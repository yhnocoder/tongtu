"""编译层共用的流水线执行辅助。

编译层经 CLI 子进程执行阶段，不直接调驱动器函数：退出码本身是验收条目之一（PDF-only
沿链退 3），而退出码只在 CLI 出口成型。执行一次四个阶段要真编译，因此按论文做成 session
级 fixture，各用例读同一次执行的结果做断言。

工作目录根由 `$TONGTU_TEST_HOME` 指定，未设时用 pytest 的临时目录。CI 里指向可缓存的
路径，使 e-print 下载结果跨作业复用（见 docs/ci/README.md 真实论文进入 CI 的前提节）。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

from tongtu import manifests
from tongtu.artifacts.fetch import FetchManifest
from tongtu.artifacts.flatten import FlattenManifest
from tongtu.artifacts.mask import BlocksFile, MaskManifest
from tongtu.artifacts.precompile import PrecompileManifest

from ..conftest import TONGTU_BIN

#: 四个已接线的阶段，按流水线顺序执行。
PIPELINE_STAGES: tuple[str, ...] = ("fetch", "flatten", "precompile", "mask")

#: 各阶段 manifest 的 model，供 `PipelineRun.manifest` 按阶段名取用。
MANIFEST_MODELS: dict[str, type[BaseModel]] = {
    "fetch": FetchManifest,
    "flatten": FlattenManifest,
    "precompile": PrecompileManifest,
    "mask": MaskManifest,
}

#: 指定工作目录根的环境变量；CI 里指向缓存路径。
TEST_HOME_ENV = "TONGTU_TEST_HOME"

#: 单个阶段的执行超时。precompile 要真编译，且可能拉起修复会话。
STAGE_TIMEOUT_SECONDS = 900

#: 掩码字符占比的合理区间（下限含、上限含）。只判 `0 < ratio < 1` 等于不设限：掩码退化到
#: 只掩住零星几段、或把整篇正文都吞掉，两种情形都仍落在那个区间里，而两者都不会有下游环节
#: 报错。实测九篇（自造三篇加真实六篇）落在 0.36 到 0.69 之间，两端各留出宽裕余量——真实
#: 论文改版会使占比小幅移动，因此取共用的宽区间而不是逐篇的期望值。
MASKED_RATIO_RANGE = (0.15, 0.90)


@dataclass(frozen=True)
class StageResult:
    """一个阶段一次执行的结果。"""

    stage: str
    returncode: int
    stdout: str
    stderr: str


@dataclass
class PipelineRun:
    """一篇论文跑完四个阶段的结果，以及读取产物的入口。

    `paper` 是短名（自造论文的目录名或真实论文的编号），用例按它分流断言；`source` 是实际
    传给 CLI 的论文参数，自造论文是源码目录的绝对路径，真实论文是 arXiv 编号。
    """

    paper: str
    source: str
    workdir: Path
    results: dict[str, StageResult] = field(default_factory=dict)

    def result(self, stage: str) -> StageResult:
        return self.results[stage]

    def first_failure(self) -> StageResult | None:
        """按流水线顺序找第一个非零退出的阶段；执行过的阶段全部成功时返回 None。"""
        for stage in PIPELINE_STAGES:
            result = self.results.get(stage)
            if result is None:
                return None  # 该阶段没跑：要么是上游先失败已在前面返回，要么本次只跑到这里
            if result.returncode != 0:
                return result
        return None

    def manifest(self, stage: str):
        """读某阶段的 manifest；读不到时让用例失败并指出首个失败的阶段。

        指出首因而不是只说「manifest 读不到」：流水线中途失败时后续阶段全都没跑，每个用例
        都会在这里失败，若消息只说本阶段的产物缺失，真正的故障点会被十几条同样的消息埋掉。
        """
        path = self.workdir / "build" / "manifests" / f"{stage}.json"
        loaded = manifests.load_manifest(path, MANIFEST_MODELS[stage])
        if loaded is not None:
            return loaded
        failure = self.first_failure()
        if failure is not None and failure.stage != stage:
            raise AssertionError(
                f"{self.paper}：{stage} 没有产物，因为 {failure.stage} 先失败了"
                f"（退 {failure.returncode}）\n{failure.stderr.strip()[:400]}"
            )
        raise AssertionError(f"{self.paper}：{stage} manifest 读不到或不合 schema（{path}）")

    def blocks(self) -> BlocksFile:
        """读 `build/blocks.json`。"""
        path = self.workdir / "build" / "blocks.json"
        loaded = manifests.load_manifest(path, BlocksFile)
        assert loaded is not None, f"{self.paper}：blocks.json 读不到或不合 schema（{path}）"
        return loaded

    def build_file(self, name: str) -> Path:
        return self.workdir / "build" / name


def run_stage(stage: str, paper: str, workdir: Path, *, force: bool = False) -> StageResult:
    """经 CLI 子进程执行一个阶段。"""
    command = [str(TONGTU_BIN), "stage", stage, paper, "--workdir", str(workdir)]
    if force:
        command.append("--force")
    completed = subprocess.run(command, capture_output=True, text=True, timeout=STAGE_TIMEOUT_SECONDS, check=False)
    return StageResult(stage=stage, returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


def run_pipeline(
    paper: str,
    source: str,
    workdir: Path,
    *,
    force_after_fetch: bool = False,
    stages: tuple[str, ...] = PIPELINE_STAGES,
) -> PipelineRun:
    """按顺序执行给定的各阶段，前一阶段非零退出即停止。

    `force_after_fetch` 用于真实论文：fetch 允许命中已缓存的下载结果，其后各阶段强制重算，
    否则代码改动后缓存中的旧结论会让阶段整体跳过，编译不再真实发生。

    `stages` 取 `PIPELINE_STAGES` 的前缀，用于只跑到某一阶段的论文——某篇论文在后续阶段
    失败并不使它在前面几个阶段的判据失效。
    """
    run = PipelineRun(paper=paper, source=source, workdir=workdir)
    for stage in stages:
        force = force_after_fetch and stage != "fetch"
        result = run_stage(stage, source, workdir, force=force)
        run.results[stage] = result
        if result.returncode != 0:
            break
    return run


def workdir_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """工作目录根：`$TONGTU_TEST_HOME` 优先，未设时用临时目录。"""
    configured = (os.environ.get(TEST_HOME_ENV) or "").strip()
    if configured:
        root = Path(configured).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        return root
    return tmp_path_factory.mktemp("tongtu-home")


def strip_comments(text: str) -> str:
    r"""逐行去掉第一个未转义 `%` 起的部分。

    口径与主文件判定、bbl 内联一致（`tongtu/stages/flatten.py`）：判「这一行有没有某个命令」
    时要排除注释里的，否则注释掉的 `\input` 会被当成展开残留。
    """
    kept = []
    for line in text.splitlines():
        index, escaped = 0, False
        cut = len(line)
        while index < len(line):
            character = line[index]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "%":
                cut = index
                break
            index += 1
        kept.append(line[:cut])
    return "\n".join(kept)


def stage_status(workdir: Path, stage: str) -> str | None:
    """读某阶段 manifest 的 `status` 字段；文件读不到或不是 JSON 时返回 None。

    只取一个字段，因此不经 model 解析：判定「是否该跳过」发生在断言之前，此时 manifest
    可能正是因为上游失败而字段不全。
    """
    path = workdir / "build" / "manifests" / f"{stage}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError):
        return None


def skip_if_download_failed(run: PipelineRun) -> None:
    """下载失败时跳过而不是判失败：那是 arXiv 可用性问题，与代码改动无关。

    解包失败、源码为空等状态不在此列——它们是拿到了下载体之后的判定结果，属于本仓库的
    行为。
    """
    fetch_result = run.results.get("fetch")
    if fetch_result is None or fetch_result.returncode == 0:
        return
    status = stage_status(run.workdir, "fetch")
    if status is None:
        pytest.skip(f"{run.paper}：fetch 未产出可读 manifest，按外部不可用处理")
    if status == "download_failed":
        pytest.skip(f"{run.paper}：e-print 下载失败（arXiv 不可用），本组不设为合并必过")
