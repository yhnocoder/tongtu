"""流水线编排器：阶段序 + 阶段级增量 + `--json` 事件流（架构 §3 / §4 / §6）。

**控制流永远在脚本手里**（架构 §2 原则 3）。本模块把十个阶段按固定顺序推一遍，每个阶段
的驱动器自己在需要时拉起有界的 agent 调用（两原语之一），拿回结果后仍由脚本校验推进。
阶段图对所有论文不变——PDF-only 也只是顶层分支到降级路线，不是动态重排。

    fetch → flatten → baseline → mask → survey → chunk → translate → compile
                                                        → figures → export

末两个阶段 M4 起转正：figures 只依赖 `src/`（与翻译轨解耦，架构 §3 / 决策 9），export
把 `build/` 里的东西组装成 `out/` 产物包并做**契约自校验**——它的出口判据就是
`docs/schemas/` 全绿，不绿即失败，绝不交付一个不合契约的包。

## 阶段级增量（架构 §4）

每阶段完成后在 `build/manifests/<stage>.json` 记**输入 hash 集**与**输出清单**；重跑时
输入未变即跳过（事件里 `status="cached"`），`--force` 无视。「断点续跑 = 原样重跑同一条
命令」这句话由此成立。下游阶段的输入 hash 里含上游产物的 hash，于是改一处自动失效下游，
不需要任何显式的依赖声明。

跳过的阶段仍要把状态**从盘上装回内存**（`load`），否则下游拿不到 `MaskResult`、块清单
这些对象。装载走的是已经落盘的契约文件（`masked.tex` + `blocks.json` + `brief.json` +
`glossary.json` + `chunks.json`），这也顺带把「产物包自足、可在新环境续跑」这条
（架构 §2 原则 4）在零期就走通了一遍。

## 失败语义（架构 §6）

* `fetch` 判 PDF-only、`baseline` 判 env_failed 等 → **结构化终止**：该阶段
  `stage_end.status="failed"`，`result.status="failed"`，退出码非 0，不往下走；
* `compile` 带回退块仍算**成功**：退出码 0，`result.status="ok_with_fallback"`，
  详情进 `out/report.json`（export 落盘）——保证永远出 PDF 是 compile 的出口判据。

## 块级增量：翻译记忆（架构 §4、决策 3）

阶段级 manifest 之下还有一层块级缓存，也是唯一昂贵的那层。translate 之前从
`out/chunks.json`（权威翻译记忆，随产物包走）与 `build/zh-chunks/chunks.json`（本轮工作
副本）装载，按 cache_key 命中即免调用；翻完写回 build 侧。装载与失效住在
:mod:`tongtu.memory`，key 的公式住在 :func:`tongtu.stages.translate.cache_key`——本模块
只负责把两者接起来。`--force` 时装一个**空**记忆：无视缓存全量重跑（架构 §6）。

于是 `build/` 整体删掉也不丢任何昂贵成果：`out/chunks.json` 在，重建时全量命中。

## 六个关节都在这里接线（架构 §3、§9）

阶段驱动器只声明「这里需要一次判断」（`arbiter` / `session` / `retranslate` 回调），
**谁去问、问什么、拿什么 prompt 资产、怎么记账**归编排器：

    ① 主文件   flatten 的 arbiter → complete（无专门资产，提示词内联）
    ② 构建环境 baseline 的 session → agent.as_session_fn()（skill/repair/SKILL.md）
    ③ 环境分类 mask 的 arbiter → complete + skill/classify/SKILL.md
    ④ 通读与术语 survey 的 complete → skill/survey/SKILL.md
    ⑤ 翻译     translate 的块循环；compile 的坏段重译复用同一内环
    ⑥ 适配与修复 compile 的 session → 同 ②

每次拉起记一条 :class:`Intervention`（形状对齐 `report.schema.json` 的
`agent_interventions`），攒在 :attr:`PipelineResult.interventions`——**outcome 一律由事后
的校验脚本与编译裁决**，不信 agent 自述（架构 §9）。落盘由 export 完成。

## 注入点

编译器、agent 运行时、下载器、latexpand 全部可注入：e2e 用假 latexpand / 假 latexmk +
MockAgent 在没有 TeX 的机器上跑完整条流水线（架构 §12 层 2 的成本纪律）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterable, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import TextIO

from . import CONTRACT_VERSION, __version__, prompts, report_page
from . import glossary as glossary_module
from . import memory as memory_module
from .agent.mock import MockAgent, identity
from .compiler import DEFAULT_TIMEOUT, Compiler
from .glossary import Glossary, GlossaryError
from .memory import CHUNKS_NAME, ZH_CHUNKS_DIRNAME, Memory
from .prompts import PromptError
from .stages import STAGES
from .stages import baseline as baseline_stage
from .stages import chunk as chunk_stage
from .stages import compile as compile_stage
from .stages import export as export_stage
from .stages import fetch as fetch_stage
from .stages import figures as figures_stage
from .stages import flatten as flatten_stage
from .stages import mask as mask_stage
from .stages import survey as survey_stage
from .stages import translate as translate_stage
from .stages.mask import Block, Caption, MaskResult
from .workdir import Workdir, open_workdir

__all__ = [
    "BLOCKS_NAME",
    "BRIEF_NAME",
    "CHUNKS_DIRNAME",
    "CHUNKS_NAME",
    "Events",
    "GLOSSARY_NAME",
    "Intervention",
    "MANIFEST_VERSION",
    "MASKED_NAME",
    "OUTCOMES",
    "PipelineError",
    "PipelineResult",
    "SKIPPED_STAGES",
    "STAGE_STATUSES",
    "StageOutcome",
    "ZH_CHUNKS_DIRNAME",
    "hash_tree",
    "manifest_fresh",
    "read_manifest",
    "retranslate",
    "run_pipeline",
    "run_stage",
    "sha256_file",
    "sha256_text",
]


class PipelineError(RuntimeError):
    """阶段的前置条件不满足（上游产物缺失等）。编排器转成结构化失败，不抛给用户。"""


# --------------------------------------------------------------------- 布局

#: build 区里的中间产物名（`out/` 是 export 阶段的活）。
MASKED_NAME = "masked.tex"
BLOCKS_NAME = "blocks.json"
CHUNKS_DIRNAME = "chunks"
#: 译块目录 `ZH_CHUNKS_DIRNAME` 与翻译记忆文件名 `CHUNKS_NAME` 从 :mod:`tongtu.memory`
#: 导入（见文件头的 import）——**单一来源在那边**：它要用同一套名字定位
#: `out/chunks.json` 与 `build/zh-chunks/chunks.json`，两处各写一遍迟早会漂。

#: survey 的两份产物（形状即产物契约的 `brief.json` / `glossary.json`，export 直接搬）。
BRIEF_NAME = survey_stage.BRIEF_NAME
GLOSSARY_NAME = survey_stage.GLOSSARY_NAME

#: 占位跳过的阶段 → 落地里程碑。**M4 起为空**：figures 与 export 已转正，十个阶段全部
#: 真跑。留着这张表不是为了将来再往里塞阶段（阶段图对所有论文不变，架构 §3），而是因为
#: 「跳过」是事件流与 CLI 都认得的一个状态，删掉这条通路等于把它的语义也一并删掉。
SKIPPED_STAGES: dict[str, str] = {}

#: 阶段状态（= `events.schema.json` 的 `stage_end.status` 枚举）。
STAGE_STATUSES: tuple[str, ...] = ("ok", "cached", "skipped", "failed")

#: manifest 结构版本。结构变了就 bump——旧 manifest 一律判过期，重算而不是误读。
MANIFEST_VERSION = 1


# --------------------------------------------------------------------- hash


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """文件内容 hash；不存在返回空串（调用方据此判前置条件）。"""
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return ""


def hash_tree(root: Path) -> str:
    """目录树的内容 hash：相对路径 + 每个文件的 hash，按路径排序后再 hash。

    `src/` 是 fetch 的产物、flatten 与编译资产的输入，整棵树参与下游的输入 hash——
    换一张图、补一个 `.sty` 都应该让 baseline 与 compile 失效。
    """
    root = Path(root)
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def _data_hash(name: str) -> str:
    """打包进 wheel 的数据文件 hash（分类表 / 适配表）——改表即失效对应阶段。"""
    try:
        return sha256_text(files("tongtu").joinpath(name).read_text(encoding="utf-8"))
    except (OSError, ModuleNotFoundError):
        return ""


# ----------------------------------------------------------------- manifest


def _output_entry(workdir: Workdir, path: Path) -> dict:
    """一条输出清单记录。目录记树 hash，文件记内容 hash 与字节数。"""
    path = Path(path)
    try:
        rel = path.relative_to(workdir.path).as_posix()
    except ValueError:
        rel = str(path)
    if path.is_dir():
        return {"path": rel, "kind": "dir", "sha256": hash_tree(path)}
    return {
        "path": rel,
        "kind": "file",
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size if path.is_file() else 0,
    }


def read_manifest(workdir: Workdir, stage: str) -> dict | None:
    path = workdir.manifest_path(stage)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_manifest(
    workdir: Workdir,
    stage: str,
    *,
    inputs: dict[str, str],
    outputs: Iterable[Path],
    result: dict | None = None,
) -> Path:
    """写 `build/manifests/<stage>.json`（架构 §4 的阶段级增量）。"""
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "contract_version": CONTRACT_VERSION,
        "tongtu_version": __version__,
        "stage": stage,
        "completed_at": _now(),
        "inputs": dict(inputs),
        "outputs": [_output_entry(workdir, path) for path in outputs],
        "result": result or {},
    }
    path = workdir.manifest_path(stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def manifest_fresh(workdir: Workdir, stage: str, inputs: dict[str, str]) -> bool:
    """输入未变、且输出还在 → 可跳过。

    只查输出**存在**，不比对输出 hash：真 latexmk 出的 PDF 带时间戳，逐次不同，拿它当
    判据会让每次重跑都失效。输入 hash 才是增量模型的依据（架构 §4），输出清单是给人
    与 report 看的账。
    """
    manifest = read_manifest(workdir, stage)
    if manifest is None:
        return False
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        return False
    if manifest.get("tongtu_version") != __version__:
        return False  # 改流水线代码 → 对应阶段起的下游重算（架构 §4 表末行）
    if manifest.get("inputs") != dict(inputs):
        return False
    for entry in manifest.get("outputs", []):
        if not (workdir.path / entry.get("path", "")).exists():
            return False
    return True


# --------------------------------------------------------------------- 事件流


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Events:
    """`--json` 事件流（`docs/schemas/events.schema.json`）与人读进度行的出口。

    JSON 模式下 stdout 每行一个事件对象（JSON Lines）；人读模式下打印简洁进度行。
    两种模式的信息量刻意不对等：机器读的那份是契约，人读的那份只求好看。
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        json_mode: bool = False,
        run_id: str = "",
        arxiv_id: str | None = None,
    ) -> None:
        self.stream = sys.stdout if stream is None else stream
        self.json_mode = json_mode
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.arxiv_id = arxiv_id
        self.lines: list[dict] = []

    # -- 底层 ---------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        payload = {
            "contract_version": CONTRACT_VERSION,
            "event": event.pop("event"),
            "ts": _now(),
            "run_id": self.run_id,
        }
        if self.arxiv_id:
            payload["arxiv_id"] = self.arxiv_id
        # None 一律照发不省略：schema 里这几个字段（total / error / reason / pdf / report）
        # 本就是 nullable，省掉反而让消费方要区分「没有」与「不知道」。
        payload.update(event)
        self.lines.append(payload)
        if self.json_mode:
            print(json.dumps(payload, ensure_ascii=False), file=self.stream, flush=True)

    def _say(self, text: str) -> None:
        if not self.json_mode:
            print(text, file=self.stream, flush=True)

    def note(self, text: str) -> None:
        """人读模式的旁白。JSON 模式下**什么也不发**——事件流是契约，不塞自由文本。"""
        self._say(text)

    # -- 四类事件 -----------------------------------------------------------

    def stage_start(self, stage: str, *, total: int | None = None) -> None:
        self._emit({"event": "stage_start", "stage": stage, "total": total})
        self._say(f"→ {stage}" + (f"（{total} 块）" if total else ""))

    def stage_end(
        self,
        stage: str,
        status: str,
        *,
        duration_ms: int = 0,
        error: str | None = None,
    ) -> None:
        event: dict = {"event": "stage_end", "stage": stage, "status": status}
        if duration_ms:
            event["duration_ms"] = duration_ms
        if error is not None:
            event["error"] = error
        self._emit(event)
        mark = {"ok": "✓", "cached": "·", "skipped": "-", "failed": "✗"}.get(status, "?")
        detail = f"  {error}" if error else ""
        self._say(f"{mark} {stage} [{status}] {duration_ms} ms{detail}")

    def chunk_progress(self, progress: translate_stage.Progress, *, stage: str = "translate") -> None:
        self._emit(
            {
                "event": "chunk_progress",
                "stage": stage,
                "id": progress.id,
                "index": progress.index,
                "total": progress.total,
                "status": progress.status,
                "attempt": progress.attempt,
                "reason": progress.reason,
            }
        )
        if progress.status in ("translated", "cached", "fallback"):
            self._say(
                f"    [{progress.index + 1}/{progress.total}] {progress.id} {progress.status}"
                + (f"（{progress.reason}）" if progress.reason else "")
            )

    def result(self, result: PipelineResult) -> None:
        self._emit(
            {
                "event": "result",
                "status": result.status,
                "exit_code": result.exit_code,
                "out_dir": str(result.workdir.out),
                "pdf": None if result.pdf is None else str(result.pdf),
                "report": None if result.report is None else str(result.report),
                "chunks_total": result.chunks_total,
                "fallback_chunks": result.fallback_chunks,
                "duration_ms": result.duration_ms,
                "error": result.message or None,
            }
        )
        self._say(
            f"\n{result.status}（退出码 {result.exit_code}）"
            + (f"：{result.message}" if result.message else "")
            + (f"\nPDF：{result.pdf}" if result.pdf else "")
        )


# --------------------------------------------------------------------- 结果


@dataclass(frozen=True)
class StageOutcome:
    """一个阶段跑完的账。`status` 取值见 :data:`STAGE_STATUSES`。"""

    stage: str
    status: str
    duration_ms: int = 0
    error: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status != "failed"

    def to_json(self) -> dict:
        data: dict = {"stage": self.stage, "status": self.status, "duration_ms": self.duration_ms}
        if self.error:
            data["error"] = self.error
        if self.detail:
            data["detail"] = self.detail
        return data


#: 干预结论（= `report.schema.json` 的 `agent_interventions[].outcome` 枚举）。
RESOLVED = "resolved"
UNRESOLVED = "unresolved"
FELL_BACK = "fallback"

OUTCOMES: tuple[str, ...] = (RESOLVED, UNRESOLVED, FELL_BACK)


@dataclass
class Intervention:
    """一次 agent 关节干预的记录，字段与 `report.schema.json` 的 `agent_interventions[]`
    一一对应（export 直接把它们摆进 report.json）。

    **可变**是有意的：`outcome` 在拉起的当下填不出来——裁决权在事后的校验脚本与编译
    （架构 §9），故先记 `unresolved`，等阶段结果回来再改判。agent 自述的「我修好了」
    在这一层没有任何效力。

    `promotable` 是促升规则（架构 §2 原则 3）的抓手：反复出现的同类干预应当被固化成
    确定性代码 / 分类表 / 适配表条目，而不是让编排器积累一次性 hack。
    """

    joint: str
    stage: str = ""
    primitive: str = "complete"  # complete / session
    trigger: str = ""
    outcome: str = UNRESOLVED
    action: str = ""
    model_id: str = ""
    prompt_version: str = ""
    duration_ms: int = 0
    transcript_path: str = ""
    promotable: bool | None = None

    def to_json(self) -> dict:
        data: dict = {
            "joint": self.joint,
            "primitive": self.primitive,
            "outcome": self.outcome,
        }
        for name in ("stage", "trigger", "action", "model_id", "prompt_version"):
            value = getattr(self, name)
            if value:
                data[name] = value
        if self.duration_ms:
            data["duration_ms"] = self.duration_ms
        if self.transcript_path:
            data["transcript_path"] = self.transcript_path
        if self.promotable is not None:
            data["promotable"] = self.promotable
        return data


@dataclass(frozen=True)
class PipelineResult:
    """一次 `tongtu run` 的结果。`exit_code` 即进程退出码（架构 §6）。"""

    status: str  # ok / ok_with_fallback / failed
    exit_code: int
    workdir: Workdir
    stages: tuple[StageOutcome, ...] = ()
    pdf: Path | None = None
    chunks_total: int = 0
    fallback_chunks: int = 0
    duration_ms: int = 0
    message: str = ""
    interventions: tuple[Intervention, ...] = ()
    """六关节的干预记录（由 export 落进 report.json 的 `agent_interventions`）。"""

    cache_hits: int = 0
    cache_misses: int = 0
    """翻译记忆的命中 / 未命中块数（架构 §4 块级缓存）。"""

    report: Path | None = None
    """`out/report.json`（export 产出）。未跑到 export 则为 None。"""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def stage(self, name: str) -> StageOutcome | None:
        return next((s for s in self.stages if s.stage == name), None)

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "workdir": str(self.workdir.path),
            "pdf": None if self.pdf is None else str(self.pdf),
            "report": None if self.report is None else str(self.report),
            "chunks_total": self.chunks_total,
            "fallback_chunks": self.fallback_chunks,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "duration_ms": self.duration_ms,
            "stages": [s.to_json() for s in self.stages],
            "agent_interventions": [i.to_json() for i in self.interventions],
            "message": self.message,
        }


# --------------------------------------------------------------------- 驱动器


@dataclass
class _Work:
    """一个阶段跑完 / 装载完的内部返回值。"""

    ok: bool = True
    error: str | None = None
    outputs: tuple[Path, ...] = ()
    detail: dict = field(default_factory=dict)


@dataclass
class _Spec:
    inputs: Callable[[], dict[str, str]]
    compute: Callable[[], _Work]
    load: Callable[[], _Work]


class Pipeline:
    """按阶段序推进一次构建。可注入编译器 / agent / 下载器，便于 e2e 与调试。"""

    def __init__(
        self,
        workdir: Workdir,
        *,
        target: str | Path | None = None,
        force: bool = False,
        events: Events | None = None,
        agent: object | None = None,
        compiler: Compiler | None = None,
        fetcher: fetch_stage.Fetcher | None = None,
        latexpand: str = flatten_stage.LATEXPAND,
        glossary: Sequence[str | Path] = (),
        model: str = "",
        max_retries: int = translate_stage.DEFAULT_MAX_RETRIES,
        budget: int = compile_stage.DEFAULT_BUDGET,
        fonts: str | Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        soft_target: int = chunk_stage.SOFT_TARGET_TOKENS,
        hard_limit: int = chunk_stage.HARD_LIMIT_TOKENS,
        cache: MutableMapping[str, str] | None = None,
        renderer: figures_stage.Renderer | None = None,
        max_long_edge: int = figures_stage.DEFAULT_MAX_LONG_EDGE,
    ) -> None:
        self.workdir = workdir
        self.target = target
        self.force = force
        self.events = events or Events(json_mode=False, arxiv_id=workdir.arxiv_id)
        self.agent = agent if agent is not None else MockAgent()
        self.compiler = compiler
        self.fetcher = fetcher
        self.latexpand = latexpand
        self.glossary = tuple(str(g) for g in glossary)
        self.model = model or getattr(self.agent, "model", "")
        self.max_retries = max_retries
        self.budget = budget
        self.fonts = fonts
        self.timeout = timeout
        self.soft_target = soft_target
        self.hard_limit = hard_limit
        self.renderer = renderer
        self.max_long_edge = max_long_edge
        self.started_at = _now()

        # 跨阶段状态（跳过的阶段由 load 从盘上装回来）
        self.flat_text: str = ""
        self.mask_result: MaskResult | None = None
        self.brief: dict = {}
        self.decisions: Glossary = glossary_module.empty()
        """survey 产出的术语**决策表**（三层输入表 + agent 新决策）。"""

        self._layers: tuple[glossary_module.Layer, ...] | None = None
        self.plan: chunk_stage.ChunkPlan | None = None
        self.units: tuple[compile_stage.TranslatedChunk, ...] = ()
        self.chunks_total = 0
        self.fallback_chunks = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.pdf: Path | None = None
        self.figures: figures_stage.FiguresResult | None = None
        self.export: export_stage.ExportResult | None = None
        self.report_path: Path | None = None
        self.outcomes: list[StageOutcome] = []

        # 六关节的干预记录（形状对齐 report.schema.json；由 export 落进 report.json）
        self.interventions: list[Intervention] = []
        self._pending: dict[str, Intervention] = {}
        """还等着被事后裁决改判 outcome 的记录（键：阶段名 / 坏段标识）。"""

        self._cache: MutableMapping[str, str] | None = cache
        """块级翻译缓存。None = 尚未装载（`--force` 时装一个空的）。"""

    # -- 路径 ---------------------------------------------------------------

    @property
    def flat_path(self) -> Path:
        return self.workdir.build / flatten_stage.FLAT_NAME

    @property
    def masked_path(self) -> Path:
        return self.workdir.build / MASKED_NAME

    @property
    def blocks_path(self) -> Path:
        return self.workdir.build / BLOCKS_NAME

    @property
    def brief_path(self) -> Path:
        return self.workdir.build / BRIEF_NAME

    @property
    def glossary_path(self) -> Path:
        return self.workdir.build / GLOSSARY_NAME

    @property
    def chunks_dir(self) -> Path:
        return self.workdir.build / CHUNKS_DIRNAME

    @property
    def zh_chunks_dir(self) -> Path:
        return self.workdir.build / ZH_CHUNKS_DIRNAME

    @property
    def zh_dir(self) -> Path:
        return self.workdir.build / compile_stage.ZH_DIRNAME

    @property
    def figures_dir(self) -> Path:
        return self.workdir.build / figures_stage.FIGURES_DIRNAME

    # -- 翻译记忆 -----------------------------------------------------------

    def memory(self) -> MutableMapping[str, str]:
        """本篇的块级翻译缓存（架构 §4）。`--force` 时是空的——无视缓存全量重跑。

        只装载一次：一次运行内 translate 至多算一遍，而 retranslate 会**预先塞**一个删过
        条目的记忆进来（`Pipeline(cache=...)`），这里不该把它再覆盖回去。
        """
        if self._cache is None:
            self._cache = Memory() if self.force else memory_module.load(self.workdir)
        return self._cache

    # -- agent 接线（六关节，架构 §3/§9）------------------------------------

    @property
    def complete_fn(self):
        """关节 ①③④⑤ 的 `complete` 原语；agent 不提供则 None。"""
        fn = getattr(self.agent, "complete", None)
        return fn if callable(fn) else None

    def _record(self, **fields) -> Intervention:
        """记一条干预（默认带上模型与 prompt 资产版本，促升统计要按这两维分组看）。

        版本号取**本关节自己那个技能**的（`skill/<name>/SKILL.md` 的 frontmatter），不是
        全部技能的聚合版本：促升统计要回答的是「这条规则的哪一版反复要人干预」。关节没有
        专属资产（如关节①）或资产读不到时留空，记账不因此中断。
        """
        fields.setdefault("model_id", self.model)
        fields.setdefault("prompt_version", prompts.joint_version(fields.get("joint", "")))
        entry = Intervention(**fields)
        self.interventions.append(entry)
        return entry

    def _settle(self, key: str, outcome: str, *, action: str = "") -> None:
        """事后裁决：把挂起的那条记录改判（`outcome` 永远由脚本填，不由 agent 自述）。"""
        entry = self._pending.pop(key, None)
        if entry is None:
            return
        entry.outcome = outcome
        if action:
            entry.action = action

    def session_for(self, stage: str):
        """关节 ②/⑥ 的回调（`FixupRequest` 形状），外加一层记账；agent 不提供则 None。

        包一层的意义不在功能而在**账**：`session` 的返回值不是裁决（架构 §9），故这里先
        把干预记成 `unresolved`，由阶段驱动器随后的重新编译改判。
        """
        adapter = getattr(self.agent, "as_session_fn", None)
        inner = adapter() if callable(adapter) else None
        if inner is None:
            return None

        def run(request):
            started = time.monotonic()
            joint = str(getattr(request, "joint", "") or stage)
            try:
                outcome = inner(request)
            except Exception as exc:  # noqa: BLE001 —— 关节炸了不该拖垮编译回环
                self._record(
                    joint=joint,
                    stage=stage,
                    primitive="session",
                    trigger=_first_error(request),
                    outcome=UNRESOLVED,
                    action=f"关节调用失败（{type(exc).__name__}）：{exc}",
                    duration_ms=_ms(started),
                )
                return None
            entry = self._record(
                joint=joint,
                stage=stage,
                primitive="session",
                trigger=_first_error(request),
                outcome=UNRESOLVED,  # 裁决在随后的重新编译
                action=str(getattr(outcome, "message", "") or "")[:200],
                duration_ms=_ms(started),
                transcript_path=str(getattr(outcome, "transcript_path", "") or ""),
            )
            self._pending[stage] = entry
            return outcome

        return run

    def main_arbiter(self):
        """关节①：主文件真歧义时判一个（`skill/` 没有专门资产，提示词内联）。

        资产之所以内联：这不是「规则」而是一道选择题——候选与打分明细都在提问里，答案
        只有一行路径，写成 markdown 资产反而多一层间接。判错的代价由 baseline 编译当场
        兜住（架构 §3：编译门控就在 flatten 之后）。
        """
        complete = self.complete_fn
        if complete is None:
            return None

        def arbiter(query) -> str | None:
            started = time.monotonic()
            trigger = "主文件歧义：" + "、".join(c.relpath for c in query.tied)
            listing = "\n".join(
                f"- {c.relpath}（启发式得分 {c.score}"
                + ("，含 \\begin{document}" if c.has_document else "")
                + (f"，被 {', '.join(c.included_by)} 包含" if c.included_by else "")
                + "）"
                for c in query.candidates
            )
            prompt = (
                "这份 arXiv 源码里有多个含 \\documentclass 的 .tex 文件，启发式打分并列，"
                "需要你判定哪一个是**主文件**（latexpand 要展开的那个）。\n\n"
                f"源码根目录：{query.root}\n候选：\n{listing}\n\n"
                "判据：主文件是整篇论文的入口（通常含 \\begin{document} 与 \\title，"
                "且不被别的 .tex \\input/\\include）；模板残骸、投稿说明、单章片段都不是。\n"
                "只输出一行：候选中那个文件的相对路径，不要解释、不要代码块。"
                "拿不准就输出一个空行——脚本会按分数取第一个，由编译裁决。"
            )
            try:
                answer = complete(prompt, "", self.model or None)
            except Exception as exc:  # noqa: BLE001
                self._record(
                    joint="main_file",
                    stage="flatten",
                    primitive="complete",
                    trigger=trigger,
                    outcome=UNRESOLVED,
                    action=f"关节①调用失败（{type(exc).__name__}）：{exc}",
                    duration_ms=_ms(started),
                )
                return None
            picked = _first_line(answer)
            self._pending["main_file"] = self._record(
                joint="main_file",
                stage="flatten",
                primitive="complete",
                trigger=trigger,
                outcome=UNRESOLVED,  # 由 find_main_tex 的 `arbitrated` 改判
                action=f"答「{picked}」" if picked else "没给出答案，按分数取第一个",
                duration_ms=_ms(started),
            )
            return picked or None

        return arbiter

    def env_arbiter(self):
        """关节③：未知环境的散文 / 重环境分类（规则来自 `skill/classify/SKILL.md`）。

        拿不到规则资产就**不问**——没有规则的分类只是猜测，而猜错的代价是不对称的
        （`skill/classify/SKILL.md` 自己写着：该 heavy 判成 prose 会炸编译）。不问即保守整块
        掩码，只降覆盖率，绝不损坏。
        """
        complete = self.complete_fn
        if complete is None:
            return None

        def arbiter(query) -> str | None:
            started = time.monotonic()
            trigger = f"未知环境 {query.name}（全文出现 {query.count} 次）"
            try:
                rules = prompts.joint_prompt("env_classify")
            except PromptError as exc:
                self._record(
                    joint="env_classify",
                    stage="mask",
                    primitive="complete",
                    trigger=trigger,
                    outcome=UNRESOLVED,
                    action=f"prompt 资产不可用（{exc}）→ 保守整块掩码",
                    duration_ms=_ms(started),
                )
                return None
            prompt = (
                f"{rules}\n\n---\n\n环境名：{query.name}\n全文出现次数：{query.count}\n\n下面是它首次出现处的源码片段："
            )
            try:
                answer = complete(prompt, query.sample, self.model or None)
            except Exception as exc:  # noqa: BLE001
                self._record(
                    joint="env_classify",
                    stage="mask",
                    primitive="complete",
                    trigger=trigger,
                    outcome=UNRESOLVED,
                    action=f"关节③调用失败（{type(exc).__name__}）：{exc}→ 保守整块掩码",
                    duration_ms=_ms(started),
                )
                return None
            verdict = _verdict(answer)
            self._record(
                joint="env_classify",
                stage="mask",
                primitive="complete",
                trigger=trigger,
                outcome=RESOLVED if verdict else UNRESOLVED,
                action=(f"判为 {verdict}" if verdict else "没给出可用判定 → 保守整块掩码（category=unknown）"),
                duration_ms=_ms(started),
                # 促升规则（架构 §2）：agent 的分类结论该沉淀成 environments.json 条目
                promotable=True if verdict else None,
            )
            return verdict

        return arbiter

    def retranslate_fn(self):
        """关节⑤复用：compile 定位出的坏段重译一次（走 translate 的同一个 validate 内环）。

        出口判据仍是机械的两层——译文先过 validate，再由**重新编译**裁决救没救活；
        两层都不看 agent 怎么自述。
        """
        complete = self.complete_fn
        if complete is None:
            return None

        def run(segment) -> str | None:
            started = time.monotonic()
            label = segment.chunk_id if segment.para_index is None else f"{segment.chunk_id}#{segment.para_index}"
            text = translate_stage.retranslate_segment(
                segment.source,
                complete=complete,
                model=self.model,
                brief=survey_stage.render_brief(self.brief) if self.brief else None,
                glossary=glossary_module.term_map(self.decisions),
                detail=segment.detail,
            )
            self._pending[f"segment:{label}"] = self._record(
                joint=translate_stage.JOINT,
                stage="compile",
                primitive="complete",
                trigger=f"编译失败坏段 {label}：{segment.detail or '（日志里没有 ! 错误）'}",
                outcome=FELL_BACK,  # 默认回退原文；编译救活了再改判
                action="重译一次（validate 通过）" if text else "没能翻出可用译文",
                duration_ms=_ms(started),
            )
            return text

        return run

    # -- 主循环 -------------------------------------------------------------

    def run(self, only: str | None = None, *, since: str | None = None) -> PipelineResult:
        """跑阶段序。

        * 默认：全部阶段按 manifest 判（`auto`）；
        * `only=<阶段>`：只算该阶段，上游一律从盘上装载，之后的阶段不跑（`tongtu stage`）；
        * `since=<阶段>`：上游装载、该阶段必算、**下游照常按 manifest 判**——
          `tongtu retranslate` 走这条（失效缓存 + 重算受影响子图，架构 §4）。
        """
        started = time.monotonic()
        failed: StageOutcome | None = None
        if isinstance(self.agent, MockAgent) and self.agent.transform is identity:
            # 不许让人误以为跑出了译文：默认 agent 是恒等 mock（真运行时属 M3）。
            self.events.note(
                "注意：当前 agent 运行时是 MockAgent（恒等翻译，译文即原文）——"
                "真 agent 适配层属 M3（docs/PHASE0.md §3.3）"
            )
        for name in STAGES:
            if only is not None and STAGES.index(name) > STAGES.index(only):
                break
            mode = _mode_for(name, only=only, since=since)
            outcome = self.run_one(name, mode=mode)
            self.outcomes.append(outcome)
            if outcome.status == "failed":
                failed = outcome
                break

        duration = int((time.monotonic() - started) * 1000)
        if failed is not None:
            status, exit_code = "failed", 1
            message = failed.error or f"{failed.stage} 阶段失败"
        else:
            status = "ok_with_fallback" if self.fallback_chunks else "ok"
            exit_code = 0
            message = f"{self.fallback_chunks} 块回退原文（详情见 report）" if self.fallback_chunks else ""
        result = PipelineResult(
            status=status,
            exit_code=exit_code,
            workdir=self.workdir,
            stages=tuple(self.outcomes),
            pdf=self.pdf,
            chunks_total=self.chunks_total,
            fallback_chunks=self.fallback_chunks,
            duration_ms=duration,
            message=message,
            interventions=tuple(self.interventions),
            cache_hits=self.cache_hits,
            cache_misses=self.cache_misses,
            report=self.report_path,
        )
        self.events.result(result)
        return result

    def run_one(self, name: str, *, mode: str = "auto") -> StageOutcome:
        """跑（或装载）一个阶段。`mode`：auto = 按 manifest 判；force = 必算；load = 只装载。"""
        if name in SKIPPED_STAGES:
            self.events.stage_start(name)
            self.events.stage_end(name, "skipped")
            return StageOutcome(stage=name, status="skipped", detail={"reason": SKIPPED_STAGES[name]})

        spec = self._specs()[name]
        total = len(self.plan) if name == "translate" and self.plan is not None else None
        self.events.stage_start(name, total=total)
        started = time.monotonic()

        try:
            # 装载模式下**不算输入 hash**：装载只读盘上已有的产物，而输入 hash 可能根本
            # 算不出来（`tongtu retranslate` 没有 target，fetch 的输入无从谈起）。
            if mode == "load":
                work = spec.load()
                status = "cached" if work.ok else "failed"
            else:
                inputs = spec.inputs()
                if mode == "auto" and not self.force and manifest_fresh(self.workdir, name, inputs):
                    work = spec.load()
                    status = "cached" if work.ok else "failed"
                else:
                    work = spec.compute()
                    status = "ok" if work.ok else "failed"
                    if work.ok:
                        write_manifest(
                            self.workdir,
                            name,
                            inputs=inputs,
                            outputs=work.outputs,
                            result=work.detail,
                        )
        except PipelineError as exc:
            work, status = _Work(ok=False, error=str(exc)), "failed"
        except Exception as exc:  # 阶段驱动器自身异常：结构化成失败，不把栈回溯抛给用户
            work = _Work(ok=False, error=f"{name} 阶段异常（{type(exc).__name__}）：{exc}")
            status = "failed"

        duration = int((time.monotonic() - started) * 1000)
        self.events.stage_end(name, status, duration_ms=duration, error=work.error)
        return StageOutcome(
            stage=name,
            status=status,
            duration_ms=duration,
            error=work.error,
            detail=work.detail,
        )

    def _specs(self) -> dict[str, _Spec]:
        return {
            "fetch": _Spec(self._fetch_inputs, self._fetch_compute, self._fetch_load),
            "flatten": _Spec(self._flatten_inputs, self._flatten_compute, self._flatten_load),
            "baseline": _Spec(self._baseline_inputs, self._baseline_compute, self._baseline_load),
            "mask": _Spec(self._mask_inputs, self._mask_compute, self._mask_load),
            "survey": _Spec(self._survey_inputs, self._survey_compute, self._survey_load),
            "chunk": _Spec(self._chunk_inputs, self._chunk_compute, self._chunk_load),
            "translate": _Spec(self._translate_inputs, self._translate_compute, self._translate_load),
            "compile": _Spec(self._compile_inputs, self._compile_compute, self._compile_load),
            "figures": _Spec(self._figures_inputs, self._figures_compute, self._figures_load),
            "export": _Spec(self._export_inputs, self._export_compute, self._export_load),
        }

    # ------------------------------------------------------------- fetch

    @property
    def _local_dir(self) -> Path | None:
        """`target` 是本地源码目录吗（`tongtu run <dir>`）。"""
        if self.target is None:
            return None
        path = Path(self.target).expanduser()
        return path if path.is_dir() else None

    def _fetch_inputs(self) -> dict[str, str]:
        local = self._local_dir
        if local is not None:
            return {"kind": "local", "dir": str(local.resolve()), "tree": hash_tree(local)}
        if self.target is None:
            raise PipelineError("没有可下载的目标：给 arXiv id 或本地源码目录")
        return {"kind": "arxiv", "id": str(self.target)}

    def _fetch_compute(self) -> _Work:
        local = self._local_dir
        if local is not None:
            result = fetch_stage.ingest_local(local, self.workdir)
        else:
            result = fetch_stage.fetch(str(self.target), self.workdir, fetcher=self.fetcher)
        detail = result.to_json()
        if result.fallback:
            return _Work(
                ok=False,
                error=(f"{result.message}——降级流水线（fallback/）零期只标记不实现（PHASE0 §5）"),
                detail=detail,
            )
        if not result.ok:
            return _Work(ok=False, error=result.message or f"fetch 失败：{result.status}", detail=detail)
        return _Work(outputs=(self.workdir.src,), detail=detail)

    def _fetch_load(self) -> _Work:
        if not self.workdir.src.is_dir() or not any(self.workdir.src.rglob("*.tex")):
            return _Work(ok=False, error=f"src/ 里没有源码：{self.workdir.src}（先跑 fetch）")
        manifest = read_manifest(self.workdir, "fetch") or {}
        return _Work(detail=manifest.get("result", {}))

    # ----------------------------------------------------------- flatten

    def _flatten_inputs(self) -> dict[str, str]:
        tree = hash_tree(self.workdir.src)
        if not tree:
            raise PipelineError(f"源码树为空：{self.workdir.src}（先跑 fetch）")
        return {"src": tree, "latexpand": self.latexpand}

    def _flatten_compute(self) -> _Work:
        # 关节①：只有真歧义（最高分并列）时驱动器才会回调，一篇论文至多问一次。
        main = flatten_stage.find_main_tex(self.workdir, arbiter=self.main_arbiter())
        if main.arbitrated and main.main is not None:
            self._settle("main_file", RESOLVED, action=f"判定主文件为 {main.main.name}")
        else:
            self._settle("main_file", UNRESOLVED)
        if not main.ok or main.main is None:
            return _Work(ok=False, error=main.message or "未找到主文件", detail=main.to_json())
        result = flatten_stage.flatten(self.workdir, main.main, latexpand=self.latexpand)
        detail = {"main": main.to_json(), "flatten": result.to_json()}
        if not result.ok or result.flat is None:
            return _Work(ok=False, error=result.message or f"flatten 失败：{result.status}", detail=detail)
        self.flat_text = _read_tex(result.flat)
        return _Work(outputs=(result.flat,), detail=detail)

    def _flatten_load(self) -> _Work:
        if not self.flat_path.is_file():
            return _Work(ok=False, error=f"没有 {self.flat_path}（先跑 flatten）")
        self.flat_text = _read_tex(self.flat_path)
        manifest = read_manifest(self.workdir, "flatten") or {}
        return _Work(detail=manifest.get("result", {}))

    # ---------------------------------------------------------- baseline

    def _baseline_inputs(self) -> dict[str, str]:
        flat = sha256_file(self.flat_path)
        if not flat:
            raise PipelineError(f"没有 {self.flat_path}（先跑 flatten）")
        return {"flat": flat, "src": hash_tree(self.workdir.src)}

    def _baseline_compute(self) -> _Work:
        result = baseline_stage.baseline(
            self.workdir,
            compiler=self.compiler,
            session=self.session_for("baseline"),  # 关节②
            timeout=self.timeout,
        )
        # 裁决在会话之后的那次重新编译，不在会话自述（架构 §9）。
        self._settle(
            "baseline",
            RESOLVED if result.ok else UNRESOLVED,
            action="修复会话之后原文编译通过" if result.ok else "修复会话之后仍编不过",
        )
        detail = result.to_json()
        if not result.ok:
            return _Work(
                ok=False,
                error=(result.message or "原文编译不过（环境问题，不是翻译问题）——流水线到此终止，不产生任何 LLM 支出"),
                detail=detail,
            )
        outputs = (result.pdf,) if result.pdf is not None else ()
        return _Work(outputs=outputs, detail=detail)

    def _baseline_load(self) -> _Work:
        manifest = read_manifest(self.workdir, "baseline")
        if manifest is None:
            return _Work(ok=False, error="没有 baseline manifest（先跑 baseline）")
        return _Work(detail=manifest.get("result", {}))

    # -------------------------------------------------------------- mask

    def _mask_inputs(self) -> dict[str, str]:
        flat = sha256_file(self.flat_path)
        if not flat:
            raise PipelineError(f"没有 {self.flat_path}（先跑 flatten）")
        return {"flat": flat, "env_table": _data_hash("data/environments.json")}

    def _mask_compute(self) -> _Work:
        text = self.flat_text or _read_tex(self.flat_path)
        # 关节③：分类表与文档自带声明都没结论的环境名才会问到 agent；分类结论进
        # blocks.json 的 `environments[].decided_by="agent"`（数据在产物里，不只在报告里）。
        result = mask_stage.mask(text, arbiter=self.env_arbiter())
        # 往返自检门（架构 §3.1 第 3 条）：不恒等不放行——解析缺陷在花第一分钱之前暴露。
        diff = mask_stage.roundtrip_diff(text, result=result)
        decided_by_agent = [e.name for e in result.environments if e.decided_by == "agent"]
        detail = {
            "blocks": len(result.blocks),
            "captions": len(result.captions),
            "environments": len(result.environments),
            "roundtrip_ok": diff is None,
            "warnings": list(result.warnings),
        }
        if decided_by_agent:
            detail["classified_by_agent"] = decided_by_agent
        if diff is not None:
            detail["roundtrip_diff"] = diff
            return _Work(ok=False, error=f"掩码往返自检未通过：{diff}", detail=detail)
        self.mask_result = result
        self.masked_path.write_text(result.masked, encoding="utf-8")
        self.blocks_path.write_text(
            json.dumps(
                result.to_blocks_json(source_path=f"build/{flatten_stage.FLAT_NAME}", roundtrip_ok=True),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return _Work(outputs=(self.masked_path, self.blocks_path), detail=detail)

    def _mask_load(self) -> _Work:
        if not (self.masked_path.is_file() and self.blocks_path.is_file()):
            return _Work(ok=False, error=f"没有 {self.masked_path} / {self.blocks_path}（先跑 mask）")
        data = json.loads(self.blocks_path.read_text(encoding="utf-8"))
        # environments 只进 report，不参与下游计算，装载时不还原（有意的有损装载）。
        self.mask_result = MaskResult(
            masked=self.masked_path.read_text(encoding="utf-8"),
            blocks=tuple(Block.from_json(b) for b in data.get("blocks", ())),
            captions=tuple(Caption.from_json(c) for c in data.get("captions", ())),
            environments=(),
            source_chars=data.get("source", {}).get("chars", 0),
            source_sha256=data.get("source", {}).get("sha256", ""),
        )
        manifest = read_manifest(self.workdir, "mask") or {}
        return _Work(detail=manifest.get("result", {}))

    # ------------------------------------------------------------ survey

    def glossary_layers(self) -> tuple[glossary_module.Layer, ...]:
        """三层输入表（全局 XDG → 论文目录 → `--glossary`），本次运行内只读一次。

        术语表读不出来 / 不合 schema 是**用户输入错误**，结构化成阶段失败并说清是哪个
        文件——不静默吞掉（吞掉的后果是译文里悄悄少了一堆术语约束）。
        """
        if self._layers is None:
            try:
                self._layers = glossary_module.load_layers(workdir=self.workdir, cli=self.glossary)
            except GlossaryError as exc:
                raise PipelineError(str(exc)) from exc
        return self._layers

    def _survey_inputs(self) -> dict[str, str]:
        if self.mask_result is None:
            raise PipelineError("没有掩码流（先跑 mask）")
        layers = self.glossary_layers()
        return {
            "masked": sha256_text(self.mask_result.masked),
            "blocks": sha256_file(self.blocks_path),
            # 术语表按**内容**参与（换个路径、同样的内容不该重跑通读）
            "glossary": sha256_text(
                "\x1e".join(f"{layer.layer}\t{glossary_module.content_hash(layer.glossary)}" for layer in layers)
            ),
            "agent": type(self.agent).__name__,
            "model": self.model,
            "prompt_version": survey_stage.prompt_version(),
            "prompt": _prompt_hash(),
        }

    def _survey_compute(self) -> _Work:
        assert self.mask_result is not None
        merged = glossary_module.merge(self.glossary_layers())
        result = survey_stage.survey(
            self.mask_result.masked,
            self.mask_result,
            complete=getattr(self.agent, "complete", None),
            glossary=merged,
            model=self.model,
            arxiv_id=self.workdir.arxiv_id,
        )
        detail = result.to_json()
        if result.attempts:  # 关节④ 被拉起过才记账（没 agent 时直接走确定性骨架）
            self._record(
                joint=survey_stage.JOINT,
                stage="survey",
                primitive="complete",
                trigger="全文通读 → 纲要与术语预扫",
                outcome=FELL_BACK if result.degraded else RESOLVED,
                action=(
                    "输出不可用，brief 降级为确定性骨架"
                    if result.degraded
                    else f"新增术语 {result.terms_added} 条、不译 {result.do_not_translate_added} 条"
                ),
            )
        if not result.ok:
            return _Work(ok=False, error=result.message or "survey 失败", detail=detail)
        self.brief = result.brief
        self.decisions = result.glossary
        _write_json(self.brief_path, result.brief)
        _write_json(self.glossary_path, result.glossary.to_json())
        for warning in result.warnings:
            self.events.note(f"    survey：{warning}")
        return _Work(outputs=(self.brief_path, self.glossary_path), detail=detail)

    def _survey_load(self) -> _Work:
        if not (self.brief_path.is_file() and self.glossary_path.is_file()):
            return _Work(ok=False, error=f"没有 {self.brief_path} / {self.glossary_path}（先跑 survey）")
        self.brief = json.loads(self.brief_path.read_text(encoding="utf-8"))
        self.decisions = Glossary.from_json(json.loads(self.glossary_path.read_text(encoding="utf-8")))
        manifest = read_manifest(self.workdir, "survey") or {}
        return _Work(detail=manifest.get("result", {}))

    # ------------------------------------------------------------- chunk

    def _chunk_inputs(self) -> dict[str, str]:
        if self.mask_result is None:
            raise PipelineError("没有掩码流（先跑 mask）")
        return {
            "masked": sha256_text(self.mask_result.masked),
            "soft_target": str(self.soft_target),
            "hard_limit": str(self.hard_limit),
            "estimator": chunk_stage.ESTIMATOR_VERSION,
        }

    def _chunk_compute(self) -> _Work:
        assert self.mask_result is not None
        plan = chunk_stage.chunk_masked(
            self.mask_result.masked,
            soft_target=self.soft_target,
            hard_limit=self.hard_limit,
        )
        if not plan.chunks:
            return _Work(ok=False, error="分块结果为空（掩码流里没有可翻译的段落）")
        self.plan = plan
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        for name, body in plan.chunk_files().items():
            (self.chunks_dir / name).write_text(body, encoding="utf-8")
        manifest_path = self.chunks_dir / CHUNKS_NAME
        manifest_path.write_text(json.dumps(plan.to_manifest(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return _Work(
            outputs=(self.chunks_dir,),
            detail={
                "chunk_count": len(plan),
                "paragraph_count": len(plan.paragraphs),
                "tokens": sum(c.tokens for c in plan.chunks),
            },
        )

    def _chunk_load(self) -> _Work:
        path = self.chunks_dir / CHUNKS_NAME
        if self.mask_result is None:
            return _Work(ok=False, error="没有掩码流（先跑 mask）")
        if not path.is_file():
            return _Work(ok=False, error=f"没有 {path}（先跑 chunk）")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.plan = _plan_from_manifest(data, self.mask_result.masked)
        manifest = read_manifest(self.workdir, "chunk") or {}
        return _Work(detail=manifest.get("result", {}))

    # --------------------------------------------------------- translate

    def _translate_inputs(self) -> dict[str, str]:
        path = self.chunks_dir / CHUNKS_NAME
        plan_hash = sha256_file(path)
        if not plan_hash:
            raise PipelineError(f"没有 {path}（先跑 chunk）")
        return {
            "chunks": plan_hash,
            "agent": type(self.agent).__name__,
            "model": self.model,
            "prompt_version": translate_stage.prompt_version(),
            # 文风规则版本号来自术语表第三段（架构 §8）：bump 即全量重翻。
            "style_version": self.decisions.style_version,
            "max_retries": str(self.max_retries),
            # 术语**决策表**的内容（不含 merged_from 与决策时间戳）与 brief 的内容 hash：
            # 两者都是 survey 的产物，也都直接进块级缓存 key（架构 §4）。
            "glossary": glossary_module.content_hash(self.decisions),
            "brief": survey_stage.brief_hash(self.brief) if self.brief else "",
        }

    def _translate_compute(self) -> _Work:
        if self.plan is None:
            return _Work(ok=False, error="没有块清单（先跑 chunk）")
        complete = self.complete_fn  # 关节⑤
        if complete is None:
            return _Work(ok=False, error=f"agent 运行时没有 complete 原语：{type(self.agent).__name__}")
        cache = self.memory()
        sources = getattr(cache, "sources", ())
        loaded = len(cache)  # 翻完之后 cache 会长大，装载条数得在这之前记
        if loaded:
            self.events.note(f"    翻译记忆：装载 {loaded} 条（{'、'.join(sources) or '注入'}）")
        result = translate_stage.translate(
            self.plan,
            complete=complete,
            model=self.model,
            brief=survey_stage.render_brief(self.brief) if self.brief else None,
            brief_hash=survey_stage.brief_hash(self.brief) if self.brief else "",
            glossary=glossary_module.term_map(self.decisions),
            style_version=self.decisions.style_version,
            cache=cache,
            max_retries=self.max_retries,
            progress=self.events.chunk_progress,
        )
        detail = {**result.to_json(), "memory": {"loaded": loaded, "sources": list(sources)}}
        if not result.ok:
            return _Work(ok=False, error=result.message or "translate 失败", detail=detail)
        self.units = result.units
        self.chunks_total = len(result.chunks)
        self.fallback_chunks = len(result.fallbacks)
        self.cache_hits = result.cache_hits
        self.cache_misses = result.cache_misses
        self.zh_chunks_dir.mkdir(parents=True, exist_ok=True)
        for item in result.chunks:
            (self.zh_chunks_dir / f"{item.id}{chunk_stage.CHUNK_SUFFIX}").write_text(item.translation, encoding="utf-8")
        # 翻译记忆写回 build 侧（权威副本随产物包走，export 搬进 `out/`）。
        memory_module.write_chunks(self.zh_chunks_dir / CHUNKS_NAME, result.to_chunks_json())
        return _Work(outputs=(self.zh_chunks_dir,), detail=detail)

    def _translate_load(self) -> _Work:
        path = self.zh_chunks_dir / CHUNKS_NAME
        if not path.is_file():
            return _Work(ok=False, error=f"没有 {path}（先跑 translate）")
        data = json.loads(path.read_text(encoding="utf-8"))
        units: list[compile_stage.TranslatedChunk] = []
        fallbacks = 0
        for entry in data.get("chunks", ()):
            section_path = entry.get("section_path") or []
            units.append(
                compile_stage.TranslatedChunk(
                    id=entry["id"],
                    source=entry.get("src", ""),
                    translation=entry.get("translation", ""),
                    section=section_path[-1] if section_path else None,
                )
            )
            if entry.get("status") == translate_stage.FALLBACK:
                fallbacks += 1
        if not units:
            return _Work(ok=False, error=f"{path} 里没有译块（先跑 translate）")
        self.units = tuple(units)
        self.chunks_total = len(units)
        self.fallback_chunks = fallbacks
        manifest = read_manifest(self.workdir, "translate") or {}
        return _Work(detail=manifest.get("result", {}))

    # ----------------------------------------------------------- compile

    def _compile_inputs(self) -> dict[str, str]:
        if not self.units:
            raise PipelineError("没有译块（先跑 translate）")
        blocks = sha256_file(self.blocks_path)
        if not blocks:
            raise PipelineError(f"没有 {self.blocks_path}（先跑 mask）")
        return {
            "zh_stream": sha256_text("".join(u.translation for u in self.units)),
            "blocks": blocks,
            "src": hash_tree(self.workdir.src),
            "adaptation": _data_hash("data/documentclass.json"),
            "budget": str(self.budget),
        }

    def _compile_compute(self) -> _Work:
        if self.mask_result is None:
            return _Work(ok=False, error="没有块清单（先跑 mask）")
        result = compile_stage.compile_zh(
            self.workdir,
            list(self.units),
            self.mask_result,
            compiler=self.compiler,
            retranslate=self.retranslate_fn(),  # 关节⑤复用（坏段重译一次）
            session=self.session_for("compile"),  # 关节⑥
            budget=self.budget,
            fonts=self.fonts,
            timeout=self.timeout,
        )
        # 事后裁决：救活的坏段改判 resolved，其余留在 fallback；会话看重新编译的结果。
        for label in result.retranslated:
            self._settle(f"segment:{label}", RESOLVED, action="重译后编译通过（坏段救活）")
        self._pending = {k: v for k, v in self._pending.items() if not k.startswith("segment:")}
        self._settle(
            "compile",
            RESOLVED if result.ok else UNRESOLVED,
            action="修复会话之后译文编译通过" if result.ok else "修复会话之后仍编不过",
        )
        detail = result.to_json()
        if not result.ok:
            return _Work(ok=False, error=result.message or "译文编译失败", detail=detail)
        self.pdf = result.pdf
        self.fallback_chunks += len(result.fallbacks)
        outputs = tuple(p for p in (result.tex, result.pdf, result.raw_tex, result.spans_path) if p is not None)
        return _Work(outputs=outputs, detail=detail)

    def _compile_load(self) -> _Work:
        pdf = self.zh_dir / "zh.pdf"
        if not pdf.is_file():
            return _Work(ok=False, error=f"没有 {pdf}（先跑 compile）")
        self.pdf = pdf
        manifest = read_manifest(self.workdir, "compile") or {}
        detail = manifest.get("result", {})
        self.fallback_chunks += len(detail.get("fallbacks", ()))
        return _Work(detail=detail)

    # ----------------------------------------------------------- figures

    def _figures_inputs(self) -> dict[str, str]:
        """figures 只依赖 `src/` 与掩码侧产物——**译文一律不进输入 hash**。

        这正是架构 §3 / 决策 9 那句「与翻译轨并行」的可执行含义：重译一块、换个模型、
        改术语表都不会让任何一张图重渲染。阶段序里它排在 compile 之后只是因为零期不做
        并行执行（阶段图对所有论文不变，位置固定在 `STAGES` 里）。
        """
        blocks = sha256_file(self.blocks_path)
        if not blocks:
            raise PipelineError(f"没有 {self.blocks_path}（先跑 mask）")
        return {
            "src": hash_tree(self.workdir.src),
            "blocks": blocks,
            "chunks": sha256_file(self.chunks_dir / CHUNKS_NAME),
            "max_long_edge": str(self.max_long_edge),
        }

    def _figures_compute(self) -> _Work:
        if self.mask_result is None:
            return _Work(ok=False, error="没有块清单（先跑 mask）")
        result = figures_stage.figures(
            self.workdir,
            self.mask_result,
            masked=self.mask_result.masked,
            plan=self.plan,
            renderer=self.renderer,
            max_long_edge=self.max_long_edge,
            force=self.force,
        )
        self.figures = result
        for warning in result.warnings:
            self.events.note(f"    figures：{warning}")
        detail = result.to_json()
        if not result.ok:
            return _Work(ok=False, error=result.message or "figures 失败", detail=detail)
        outputs = (result.out_dir,) if result.out_dir is not None else ()
        return _Work(outputs=outputs, detail=detail)

    def _figures_load(self) -> _Work:
        path = self.figures_dir / figures_stage.FIGURES_JSON
        if not path.is_file():
            return _Work(ok=False, error=f"没有 {path}（先跑 figures）")
        manifest = read_manifest(self.workdir, "figures") or {}
        return _Work(detail=manifest.get("result", {}))

    # ------------------------------------------------------------ export

    def _export_inputs(self) -> dict[str, str]:
        """输入 = 要装进包里的每一份东西 + 检验页模板资产。

        **刻意不含**运行过程本身（阶段耗时、干预记录）：那些每跑一次都不同，塞进 hash 等于
        取消这一阶段的缓存。于是「原样重跑」时 `out/` 一个字节都不动，report.json 描述的
        始终是**产出这批产物的那次构建**——这正是增量构建模型该有的语义。
        """
        pdf = sha256_file(self.zh_dir / export_stage.ZH_PDF)
        if not pdf:
            raise PipelineError(f"没有 {self.zh_dir / export_stage.ZH_PDF}（先跑 compile）")
        return {
            "zh_tex": sha256_file(self.zh_dir / compile_stage.ZH_TEX),
            "pdf": pdf,
            "synctex": sha256_file(self.zh_dir / export_stage.SYNCTEX_NAME),
            # 块区间进 anchors，改了它 anchors.json 就该重算（文件缺席即空串）。
            "spans": sha256_file(self.workdir.build / compile_stage.SPANS_NAME),
            "blocks": sha256_file(self.blocks_path),
            "chunks": sha256_file(self.zh_chunks_dir / CHUNKS_NAME),
            "brief": sha256_file(self.brief_path),
            "glossary": sha256_file(self.glossary_path),
            "figures": sha256_file(self.figures_dir / figures_stage.FIGURES_JSON),
            "page_assets": report_page.assets_hash(),
        }

    def _export_compute(self) -> _Work:
        result = export_stage.export(
            self.workdir,
            report=self.report_body(),
            title=self.workdir.arxiv_id or "",
        )
        self.export = result
        for warning in result.warnings:
            self.events.note(f"    export：{warning}")
        detail = result.to_json()
        if not result.ok:
            return _Work(
                ok=False,
                error=result.message or "产物包不通过契约自校验",
                detail=detail,
            )
        # 交付路径从此指向产物包：build/ 随时可丢，out/ 才是给人与文枢的那一份。
        self.pdf = result.out_dir / export_stage.ZH_PDF
        self.report_path = result.report_path
        return _Work(outputs=result.outputs(), detail=detail)

    def _export_load(self) -> _Work:
        report = self.workdir.out / export_stage.REPORT_NAME
        pdf = self.workdir.out / export_stage.ZH_PDF
        if not (report.is_file() and pdf.is_file()):
            return _Work(ok=False, error=f"产物包不完整：{self.workdir.out}（先跑 export）")
        self.pdf = pdf
        self.report_path = report
        manifest = read_manifest(self.workdir, "export") or {}
        return _Work(detail=manifest.get("result", {}))

    # -------------------------------------------------- report.json 主体

    def _detail(self, stage: str) -> dict:
        outcome = next((s for s in self.outcomes if s.stage == stage), None)
        return outcome.detail if outcome is not None and outcome.detail else {}

    def report_body(self) -> dict:
        """把这一次运行摊成 `report.schema.json` 的形状（`artifacts` 由 export 补）。

        数据来源是各阶段的 `detail`——**同一份 detail 既进 manifest 也进 report**，故
        「命中缓存的阶段」照样有账可报（`_x_load` 把 manifest 里的 result 读回来）。

        `stages` 里没有 export 自己：一个阶段不评判自己的成败（那正是「我检查过了」的
        变体，架构 §2 原则 1）。export 的成败在事件流、manifest 与退出码里，由 schema
        校验裁决。
        """
        translate = self._detail("translate")
        compiled = self._detail("compile")
        masked = self._detail("mask")
        baseline = self._detail("baseline")
        fetched = self._detail("fetch")

        validation: dict = {
            "chunks_total": int(translate.get("chunk_count", self.chunks_total)),
            "translated": int(translate.get("translated", 0)),
            "cached": int(translate.get("cache_hits", self.cache_hits)),
            "fallback": int(translate.get("fallback", 0)),
            "retries": max(0, int(translate.get("attempts", 0)) - int(translate.get("chunk_count", 0))),
        }
        if translate.get("failures_by_check"):
            validation["failures_by_check"] = dict(translate["failures_by_check"])
        if "roundtrip_ok" in masked:
            validation["mask_roundtrip_ok"] = bool(masked["roundtrip_ok"])

        compile_section: dict = {
            "passed": bool(compiled.get("passed", self.pdf is not None)),
            "engine": str(compiled.get("engine", "")),
            "passes": int(compiled.get("passes", 0)),
        }
        if "passed" in baseline:
            compile_section["baseline_passed"] = bool(baseline["passed"])
        if compiled.get("inject"):
            compile_section["inject"] = dict(compiled["inject"])
        warnings = [{"kind": "compile", "message": str(text)} for text in compiled.get("warnings", ())]
        if warnings:
            compile_section["warnings"] = warnings
        if compiled.get("log_path"):
            compile_section["log_path"] = f"logs/{compiled['log_path']}"

        paper: dict = {"arxiv_id": self.workdir.arxiv_id or ""}
        title = ((self.brief or {}).get("paper") or {}).get("title")
        if title:
            paper["title"] = str(title)
        if fetched.get("kind"):
            paper["source"] = str(fetched["kind"])

        status = "ok_with_fallback" if self.fallback_chunks else "ok"
        body: dict = {
            "contract_version": CONTRACT_VERSION,
            "tongtu_version": __version__,
            "paper": paper,
            "status": status,
            "started_at": self.started_at,
            "finished_at": _now(),
            "stages": [
                {
                    "name": outcome.stage,
                    "status": outcome.status,
                    "duration_ms": max(0, outcome.duration_ms),
                    **({"message": outcome.error} if outcome.error else {}),
                }
                for outcome in self.outcomes
            ],
            "validation": validation,
            "compile": compile_section,
            "agent_interventions": [i.to_json() for i in self.interventions],
        }
        fallbacks = self._fallbacks(compiled)
        if fallbacks:
            body["fallbacks"] = fallbacks
        return body

    def _fallbacks(self, compiled: dict) -> list[dict]:
        """回退清单：compile 的坏段 + translate 重试用尽的块（后者从翻译记忆里读）。

        两条来源不重叠：compile 记的是「编译不过的段」，translate 记的是「校验过不了的
        块」。同一个块两样都占时以 compile 的记录为准（它更具体，带段落号）。
        """
        entries = [dict(f) for f in compiled.get("fallbacks", ()) if isinstance(f, dict)]
        seen = {f.get("chunk_id") for f in entries}
        record = memory_module.read_chunks(self.zh_chunks_dir / CHUNKS_NAME)
        for item in memory_module.entries(record):
            if item.get("status") != translate_stage.FALLBACK:
                continue
            chunk_id = str(item.get("id") or "")
            if not chunk_id or chunk_id in seen:
                continue
            entry: dict = {
                "chunk_id": chunk_id,
                "reason": str(item.get("fallback_reason") or "other"),
            }
            paragraphs = item.get("fallback_paragraphs")
            if isinstance(paragraphs, (list, tuple)) and paragraphs:
                entry["paragraphs"] = [int(p) for p in paragraphs]
            entries.append(entry)
            seen.add(chunk_id)
        return entries


# ------------------------------------------------------------------ 辅助


def _mode_for(name: str, *, only: str | None, since: str | None) -> str:
    """某个阶段这一轮该怎么跑：`auto`（按 manifest 判）/ `force`（必算）/ `load`（只装载）。"""
    if only is not None:
        return "force" if name == only else "load"
    if since is not None:
        index, start = STAGES.index(name), STAGES.index(since)
        if index < start:
            return "load"
        return "force" if index == start else "auto"
    return "auto"


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _first_line(text: object) -> str:
    """取答案的第一行非空文本（关节①要的就是一行路径）。"""
    if not isinstance(text, str):
        return ""
    for line in text.splitlines():
        stripped = line.strip().strip("`").strip()
        if stripped:
            return stripped
    return ""


#: 关节③认得的两个答案。别的一律当「不知道」——`skill/classify/SKILL.md` 明说 unknown 是有用
#: 的答案，而不是失败。
_VERDICTS = ("prose", "heavy")

_WORD_RE = re.compile(r"[a-zA-Z]+")


def _verdict(answer: object) -> str | None:
    """把关节③的回答收敛成 `prose` / `heavy` / None（保守默认）。

    **只认「整个回答就是那一个词」**（`skill/classify/SKILL.md` 要求的输出格式，允许围栏、标点
    与空白）。「prose 还是 heavy？」这种含糊话读成 prose 是不划算的：判错方向的代价是
    不对称的——该 heavy 的判成 prose 会把公式送去翻译、炸编译，反过来只是少翻一段。
    """
    if not isinstance(answer, str):
        return None
    words = _WORD_RE.findall(answer.strip().lower())
    return words[0] if len(words) == 1 and words[0] in _VERDICTS else None


def _first_error(request: object) -> str:
    """`FixupRequest` 里的第一条编译错误（干预记录的 trigger）。"""
    detail = getattr(request, "first_error", None)
    return f"编译失败：{detail}" if detail else "编译失败（日志里没有 ! 错误）"


def _read_tex(path: Path) -> str:
    """读 TeX 文本。

    源码可能是 latin-1 等编码（flatten 刻意按字节落盘），零期一律按 UTF-8 读、非法字节
    用替代字符顶上——掩码往返自检在**解码后的文本**上仍然恒等，编码探测留到后续里程碑。
    """
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _write_json(path: Path, payload: dict) -> Path:
    """写一份 JSON 产物（UTF-8、缩进 2、末尾换行——与其余产物落盘风格一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _prompt_hash() -> str:
    """通读 prompt 资产的内容 hash——改 `skill/survey/SKILL.md` 即失效 survey（架构 §4）。

    `prompt_version` 是人工 bump 的，忘了 bump 也不该让缓存说谎；内容 hash 是兜底。
    """
    try:
        return sha256_text(survey_stage.load_prompt())
    except PromptError:
        return ""


def _plan_from_manifest(data: dict, masked: str) -> chunk_stage.ChunkPlan:
    """从 `build/chunks/chunks.json` 装回 `ChunkPlan`（跳过 chunk 阶段时走这条）。

    块正文按记录下来的 `span` 从掩码流里切——这也顺带校验了「块区间首尾相接、拼接可还原
    掩码流」这条不变式。`headings` 不还原（下游无人消费，且 `to_dict` 本就是有损的），
    段落表用同一个纯函数重新切一遍（无 IO）。
    """
    paragraphs = chunk_stage.split_paragraphs(masked)
    chunks: list[chunk_stage.Chunk] = []
    for entry in data.get("chunks", ()):
        start, end = entry["span"]
        chunks.append(
            chunk_stage.Chunk(
                id=entry["id"],
                index=entry["index"],
                section_path=tuple(entry.get("section_path", ())),
                section_titles=tuple(entry.get("section_titles", ())),
                headings=(),
                para_start=entry["para_start"],
                para_end=entry["para_end"],
                paragraph_count=entry["paragraph_count"],
                tokens=entry["tokens"],
                is_appendix=entry["is_appendix"],
                is_front_matter=entry["is_front_matter"],
                part=entry["part"],
                part_count=entry["part_count"],
                span=(start, end),
                text=masked[start:end],
                prev_tail_para=entry.get("prev_tail_para"),
                next_head_para=entry.get("next_head_para"),
            )
        )
    return chunk_stage.ChunkPlan(
        source=masked,
        paragraphs=paragraphs,
        chunks=tuple(chunks),
        soft_target=data.get("soft_target_tokens", chunk_stage.SOFT_TARGET_TOKENS),
        hard_limit=data.get("hard_limit_tokens", chunk_stage.HARD_LIMIT_TOKENS),
        tail_min=data.get("tail_min_tokens", 0),
    )


# ------------------------------------------------------------------ 顶层入口


def _resolve_target(target: str | Path) -> tuple[str, str | None]:
    """`<arxiv-id | dir>` → (原样目标, 工作目录用的 id)。

    本地目录取目录名当 id（`tongtu run tests/fixtures/papers/article` → `article`），
    这样默认工作目录仍落在 `~/.local/share/tongtu/<id>/`，仓库里绝不长出论文目录。
    """
    text = str(target)
    path = Path(text).expanduser()
    if path.is_dir():
        return text, path.resolve().name
    return text, text


def run_pipeline(
    target: str | Path,
    *,
    workdir: str | Path | None = None,
    force: bool = False,
    json_events: bool = False,
    out: TextIO | None = None,
    glossary: Sequence[str | Path] = (),
    **kwargs,
) -> PipelineResult:
    """`tongtu run <arxiv-id | dir>`：跑完整流水线。其余关键字参数直通 :class:`Pipeline`。"""
    raw, arxiv_id = _resolve_target(target)
    paper = open_workdir(arxiv_id=arxiv_id, workdir=workdir, create=True)
    events = Events(out, json_mode=json_events, arxiv_id=paper.arxiv_id)
    pipeline = Pipeline(
        paper,
        target=raw,
        force=force,
        events=events,
        glossary=glossary,
        **kwargs,
    )
    return pipeline.run()


def run_stage(
    name: str,
    target: str | Path,
    *,
    workdir: str | Path | None = None,
    json_events: bool = False,
    out: TextIO | None = None,
    **kwargs,
) -> PipelineResult:
    """`tongtu stage <name> <id>`：只算一个阶段，上游一律从盘上装载。

    可单跑的阶段 = 上游产物已经在工作目录里的任何阶段：`flatten`（要 `src/`）、
    `baseline` / `mask`（要 `build/flat.tex`）、`chunk`（要 `masked.tex` + `blocks.json`）、
    `survey`（要 `masked.tex` + `blocks.json`）、`translate`（要 `build/chunks/`）、
    `compile`（要译块 + `blocks.json`）。`fetch` 只在给的是 arXiv id 或本地目录时能单跑。
    `figures`（要 `src/` + `blocks.json`）与 `export`（要 compile 与 figures 的产物）
    同样可单跑。
    """
    if name not in STAGES:
        raise ValueError(f"未知阶段：{name}（可选 {', '.join(STAGES)}）")
    raw, arxiv_id = _resolve_target(target)
    paper = open_workdir(arxiv_id=arxiv_id, workdir=workdir, create=True)
    events = Events(out, json_mode=json_events, arxiv_id=paper.arxiv_id)
    pipeline = Pipeline(paper, target=raw, events=events, **kwargs)
    return pipeline.run(only=name)


# --------------------------------------------------------------- retranslate


def _failed(workdir: Workdir, events: Events, message: str) -> PipelineResult:
    """一次没跑起来的运行：结构化失败 + 事件流里照样有 result 行（消费方不必特判）。"""
    result = PipelineResult(status="failed", exit_code=1, workdir=workdir, message=message)
    events.result(result)
    return result


def retranslate(
    target: str | Path,
    *,
    workdir: str | Path | None = None,
    chunks: Sequence[str] = (),
    term: str = "",
    all_chunks: bool = False,
    json_events: bool = False,
    out: TextIO | None = None,
    glossary: Sequence[str | Path] = (),
    **kwargs,
) -> PipelineResult:
    """`tongtu retranslate <id>`：块级失效重算（架构 §4 返工触发表、§6）。

    三种失效范围恰好对应架构 §4 那张表的三行：

    * `chunks=["c012", "c045"]` —— 点名重翻（编译回环之外的人工返工）；
    * `term="tensor"` —— **命中该术语的块**（编辑某术语条目 → 增量重翻 → 重编译）；
    * `all_chunks=True` —— 全部块（改文风 / 升级 prompt / 换模型时的显式全量重翻）。

    实现就是架构 §2 原则 2 的字面意思：**删掉对应的缓存条目，再重算受影响子图**——
    translate 必算（缓存里还在的块直接命中，等于只重翻被失效的那些），compile 及其下游
    按 manifest 判（译文没变则整段跳过）。没有任何「回跳到某阶段」的控制流。

    上游阶段一律**从盘上装载**，不重算：retranslate 只给 id，没有下载目标，也不该因为
    一次重翻去碰 fetch / baseline。想让上游也重算，那是 `tongtu run` 的活。

    退出码语义同 `run`：0 = 出包（含有回退块），非 0 = 未能出包。
    未知的块 id 是**用法错误**，抛 `ValueError`（CLI 转成退出码 2）。
    """
    if not (chunks or term or all_chunks):
        raise ValueError("retranslate 要指定失效范围：--chunks / --term / --all 三选一")
    raw, arxiv_id = _resolve_target(target)
    paper = open_workdir(arxiv_id=arxiv_id, workdir=workdir, create=False)
    events = Events(out, json_mode=json_events, arxiv_id=paper.arxiv_id)
    if not paper.exists():
        return _failed(paper, events, f"工作目录不存在：{paper.path}（先跑 tongtu run）")

    out_path, build_path = memory_module.memory_paths(paper)
    record = memory_module.read_chunks(build_path) or memory_module.read_chunks(out_path)
    if not memory_module.entries(record):
        return _failed(
            paper,
            events,
            f"没有可失效的翻译记忆（找过 {build_path} 与 {out_path}）——先跑 tongtu run",
        )

    memory = memory_module.load(paper)
    if all_chunks:
        keys, scope = set(memory), "全部块"
    elif term:
        keys = memory_module.keys_for_term(record, term)
        scope = f"命中术语 {term!r} 的块"
        if not keys:
            return _failed(paper, events, f"没有块命中术语 {term!r}，无需重翻")
    else:
        keys, missing = memory_module.keys_for_chunks(record, chunks)
        if missing:
            known = ", ".join(memory_module.chunk_ids(record)[:20])
            raise ValueError(f"翻译记忆里没有这些块：{', '.join(missing)}（已有：{known}）")
        scope = f"块 {', '.join(chunks)}"

    dropped = memory.forget(keys)
    # 权威记忆也要抹掉，否则下一次 `tongtu run` 会把失效掉的译文原样装回来。
    dropped_out = memory_module.drop_entries(out_path, keys)
    events.note(
        f"失效 {scope}：翻译记忆删掉 {dropped} 条"
        + (f"（其中 {dropped_out} 条来自 {out_path.name}）" if dropped_out else "")
        + f"，剩 {len(memory)} 条可命中"
    )

    pipeline = Pipeline(paper, target=raw, events=events, glossary=glossary, cache=memory, **kwargs)
    return pipeline.run(since="translate")
