"""流水线编排器：阶段序 + 阶段级增量 + `--json` 事件流（架构 §3 / §4 / §6）。

**控制流永远在脚本手里**（架构 §2 原则 3）。本模块把十个阶段按固定顺序推一遍，每个阶段
的驱动器自己在需要时拉起有界的 agent 调用（两原语之一），拿回结果后仍由脚本校验推进。
阶段图对所有论文不变——PDF-only 也只是顶层分支到降级路线，不是动态重排。

    fetch → flatten → baseline → mask → (survey) → chunk → translate → compile
                                                          → (figures) → (export)

括号里的三个阶段本期占位跳过（survey 属 M3，figures / export 属 M4），事件流里如实记
`status="skipped"`，不假装做过。

## 阶段级增量（架构 §4）

每阶段完成后在 `build/manifests/<stage>.json` 记**输入 hash 集**与**输出清单**；重跑时
输入未变即跳过（事件里 `status="cached"`），`--force` 无视。「断点续跑 = 原样重跑同一条
命令」这句话由此成立。下游阶段的输入 hash 里含上游产物的 hash，于是改一处自动失效下游，
不需要任何显式的依赖声明。

跳过的阶段仍要把状态**从盘上装回内存**（`load`），否则下游拿不到 `MaskResult`、块清单
这些对象。装载走的是已经落盘的契约文件（`masked.tex` + `blocks.json` + `chunks.json`），
这也顺带把「产物包自足、可在新环境续跑」这条（架构 §2 原则 4）在零期就走通了一遍。

## 失败语义（架构 §6）

* `fetch` 判 PDF-only、`baseline` 判 env_failed 等 → **结构化终止**：该阶段
  `stage_end.status="failed"`，`result.status="failed"`，退出码非 0，不往下走；
* `compile` 带回退块仍算**成功**：退出码 0，`result.status="ok_with_fallback"`，
  详情进 report（M4）——保证永远出 PDF 是 compile 的出口判据。

## 注入点

编译器、agent 运行时、下载器、latexpand 全部可注入：e2e 用假 latexpand / 假 latexmk +
MockAgent 在没有 TeX 的机器上跑完整条流水线（架构 §12 层 2 的成本纪律）。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Callable, Iterable, Sequence, TextIO

from . import CONTRACT_VERSION, __version__
from .agent.mock import MockAgent, identity
from .compiler import DEFAULT_TIMEOUT, Compiler
from .stages import STAGES
from .stages import baseline as baseline_stage
from .stages import chunk as chunk_stage
from .stages import compile as compile_stage
from .stages import fetch as fetch_stage
from .stages import flatten as flatten_stage
from .stages import mask as mask_stage
from .stages import translate as translate_stage
from .stages.mask import Block, Caption, MaskResult
from .workdir import Workdir, open_workdir

__all__ = [
    "BLOCKS_NAME",
    "CHUNKS_DIRNAME",
    "CHUNKS_NAME",
    "Events",
    "MANIFEST_VERSION",
    "MASKED_NAME",
    "PipelineError",
    "PipelineResult",
    "SKIPPED_STAGES",
    "STAGE_STATUSES",
    "StageOutcome",
    "ZH_CHUNKS_DIRNAME",
    "hash_tree",
    "manifest_fresh",
    "read_manifest",
    "run_pipeline",
    "run_stage",
    "sha256_file",
    "sha256_text",
]


class PipelineError(RuntimeError):
    """阶段的前置条件不满足（上游产物缺失等）。编排器转成结构化失败，不抛给用户。"""


# --------------------------------------------------------------------- 布局

#: build 区里的中间产物名（`out/` 是 export 阶段的活，M4）。
MASKED_NAME = "masked.tex"
BLOCKS_NAME = "blocks.json"
CHUNKS_DIRNAME = "chunks"
ZH_CHUNKS_DIRNAME = "zh-chunks"

#: 块清单 / 翻译记忆的文件名。后者与产物契约 `chunks.json` 同形（`docs/schemas/`），
#: export（M4）直接搬过去即可，不需要二次组装。
CHUNKS_NAME = "chunks.json"

#: 本期占位跳过的阶段 → 落地里程碑。
SKIPPED_STAGES: dict[str, str] = {
    "survey": "M3（通读 + 术语预扫，关节④）",
    "figures": "M4（EPS/PDF/位图 → PNG 预渲染）",
    "export": "M4（产物包组装 + anchors 合成 + 检验页）",
}

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
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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

    def result(self, result: "PipelineResult") -> None:
        self._emit(
            {
                "event": "result",
                "status": result.status,
                "exit_code": result.exit_code,
                "out_dir": str(result.workdir.out),
                "pdf": None if result.pdf is None else str(result.pdf),
                "report": None,  # M4 export 落 out/report.json
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
            "chunks_total": self.chunks_total,
            "fallback_chunks": self.fallback_chunks,
            "duration_ms": self.duration_ms,
            "stages": [s.to_json() for s in self.stages],
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

        # 跨阶段状态（跳过的阶段由 load 从盘上装回来）
        self.flat_text: str = ""
        self.mask_result: MaskResult | None = None
        self.plan: chunk_stage.ChunkPlan | None = None
        self.units: tuple[compile_stage.TranslatedChunk, ...] = ()
        self.chunks_total = 0
        self.fallback_chunks = 0
        self.pdf: Path | None = None
        self.outcomes: list[StageOutcome] = []

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
    def chunks_dir(self) -> Path:
        return self.workdir.build / CHUNKS_DIRNAME

    @property
    def zh_chunks_dir(self) -> Path:
        return self.workdir.build / ZH_CHUNKS_DIRNAME

    # -- agent 接线 ---------------------------------------------------------

    @property
    def session_fn(self):
        """关节 ②/⑥ 的回调（`FixupRequest` 形状）；agent 不提供则 None。"""
        adapter = getattr(self.agent, "as_session_fn", None)
        return adapter() if callable(adapter) else None

    # -- 主循环 -------------------------------------------------------------

    def run(self, only: str | None = None) -> PipelineResult:
        """跑完整阶段序；`only` 给出时只算该阶段（上游一律从盘上装载）。"""
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
            mode = "auto" if only is None else ("force" if name == only else "load")
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
            message = (
                f"{self.fallback_chunks} 块回退原文（详情见 report）"
                if self.fallback_chunks
                else ""
            )
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
        )
        self.events.result(result)
        return result

    def run_one(self, name: str, *, mode: str = "auto") -> StageOutcome:
        """跑（或装载）一个阶段。`mode`：auto = 按 manifest 判；force = 必算；load = 只装载。"""
        if name in SKIPPED_STAGES:
            self.events.stage_start(name)
            self.events.stage_end(name, "skipped")
            return StageOutcome(
                stage=name, status="skipped", detail={"reason": SKIPPED_STAGES[name]}
            )

        spec = self._specs()[name]
        total = len(self.plan) if name == "translate" and self.plan is not None else None
        self.events.stage_start(name, total=total)
        started = time.monotonic()

        try:
            inputs = spec.inputs()
            if mode == "load" or (mode == "auto" and not self.force and manifest_fresh(
                self.workdir, name, inputs
            )):
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
        except Exception as exc:  # 阶段驱动器自己炸了：结构化成失败，不把栈甩给用户
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
            "chunk": _Spec(self._chunk_inputs, self._chunk_compute, self._chunk_load),
            "translate": _Spec(
                self._translate_inputs, self._translate_compute, self._translate_load
            ),
            "compile": _Spec(self._compile_inputs, self._compile_compute, self._compile_load),
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
                error=(
                    f"{result.message}——降级流水线（fallback/）零期只标记不实现"
                    "（PHASE0 §5）"
                ),
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
        main = flatten_stage.find_main_tex(self.workdir)
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
            session=self.session_fn,
            timeout=self.timeout,
        )
        detail = result.to_json()
        if not result.ok:
            return _Work(
                ok=False,
                error=(
                    result.message
                    or "原文编译不过（环境问题，不是翻译问题）——流水线到此终止，"
                    "不产生任何 LLM 支出"
                ),
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
        result = mask_stage.mask(text)
        # 往返自检门（架构 §3.1 第 3 条）：不恒等不放行——解析缺陷在花第一分钱之前暴露。
        diff = mask_stage.roundtrip_diff(text, result=result)
        detail = {
            "blocks": len(result.blocks),
            "captions": len(result.captions),
            "environments": len(result.environments),
            "roundtrip_ok": diff is None,
            "warnings": list(result.warnings),
        }
        if diff is not None:
            detail["roundtrip_diff"] = diff
            return _Work(ok=False, error=f"掩码往返自检未通过：{diff}", detail=detail)
        self.mask_result = result
        self.masked_path.write_text(result.masked, encoding="utf-8")
        self.blocks_path.write_text(
            json.dumps(
                result.to_blocks_json(
                    source_path=f"build/{flatten_stage.FLAT_NAME}", roundtrip_ok=True
                ),
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
        manifest_path.write_text(
            json.dumps(plan.to_manifest(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
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
            "prompt_version": translate_stage.PROMPT_VERSION,
            "style_version": translate_stage.STYLE_VERSION,
            "max_retries": str(self.max_retries),
            "glossary": _files_hash(self.glossary),
        }

    def _translate_compute(self) -> _Work:
        if self.plan is None:
            return _Work(ok=False, error="没有块清单（先跑 chunk）")
        complete = getattr(self.agent, "complete", None)
        if not callable(complete):
            return _Work(ok=False, error=f"agent 运行时没有 complete 原语：{type(self.agent).__name__}")
        result = translate_stage.translate(
            self.plan,
            complete=complete,
            model=self.model,
            max_retries=self.max_retries,
            progress=self.events.chunk_progress,
        )
        detail = result.to_json()
        if not result.ok:
            return _Work(ok=False, error=result.message or "translate 失败", detail=detail)
        self.units = result.units
        self.chunks_total = len(result.chunks)
        self.fallback_chunks = len(result.fallbacks)
        self.zh_chunks_dir.mkdir(parents=True, exist_ok=True)
        for item in result.chunks:
            (self.zh_chunks_dir / f"{item.id}{chunk_stage.CHUNK_SUFFIX}").write_text(
                item.translation, encoding="utf-8"
            )
        (self.zh_chunks_dir / CHUNKS_NAME).write_text(
            json.dumps(result.to_chunks_json(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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
            session=self.session_fn,
            budget=self.budget,
            fonts=self.fonts,
            timeout=self.timeout,
        )
        detail = result.to_json()
        if not result.ok:
            return _Work(ok=False, error=result.message or "译文编译失败", detail=detail)
        self.pdf = result.pdf
        self.fallback_chunks += len(result.fallbacks)
        outputs = tuple(p for p in (result.tex, result.pdf, result.raw_tex) if p is not None)
        return _Work(outputs=outputs, detail=detail)

    def _compile_load(self) -> _Work:
        pdf = self.workdir.build / compile_stage.ZH_DIRNAME / "zh.pdf"
        if not pdf.is_file():
            return _Work(ok=False, error=f"没有 {pdf}（先跑 compile）")
        self.pdf = pdf
        manifest = read_manifest(self.workdir, "compile") or {}
        detail = manifest.get("result", {})
        self.fallback_chunks += len(detail.get("fallbacks", ()))
        return _Work(detail=detail)


# ------------------------------------------------------------------ 辅助


def _read_tex(path: Path) -> str:
    """读 TeX 文本。

    源码可能是 latin-1 等编码（flatten 刻意按字节落盘），零期一律按 UTF-8 读、非法字节
    用替代字符顶上——掩码往返自检在**解码后的文本**上仍然恒等，编码探测留到后续里程碑。
    """
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _files_hash(paths: Sequence[str]) -> str:
    """一组文件（术语表）的内容 hash；不存在的按空内容计。"""
    if not paths:
        return ""
    digest = hashlib.sha256()
    for item in paths:
        digest.update(str(item).encode("utf-8"))
        digest.update(sha256_file(Path(item)).encode("ascii"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def _plan_from_manifest(data: dict, masked: str) -> chunk_stage.ChunkPlan:
    """从 `build/chunks/chunks.json` 装回 `ChunkPlan`（跳过 chunk 阶段时走这条）。

    块正文按记录下来的 `span` 从掩码流里切——这也顺带校验了「块区间首尾相接、拼接可还原
    掩码流」这条不变式。`headings` 不还原（下游无人消费，且 `to_dict` 本就是有损的），
    段落表用同一把尺子重新切一遍（纯函数，无 IO）。
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
    `translate`（要 `build/chunks/`）、`compile`（要译块 + `blocks.json`）。`fetch` 只在
    给的是 arXiv id 或本地目录时能单跑。三个占位阶段（survey / figures / export）不可跑。
    """
    if name not in STAGES:
        raise ValueError(f"未知阶段：{name}（可选 {', '.join(STAGES)}）")
    raw, arxiv_id = _resolve_target(target)
    paper = open_workdir(arxiv_id=arxiv_id, workdir=workdir, create=True)
    events = Events(out, json_mode=json_events, arxiv_id=paper.arxiv_id)
    pipeline = Pipeline(paper, target=raw, events=events, **kwargs)
    return pipeline.run(only=name)
