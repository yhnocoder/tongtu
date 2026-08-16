r"""precompile 阶段驱动器：在任何翻译动作之前把原文编译到通过，产出下游输入与基线数据。

precompile 只读 `src/` 与 `build/`，只写 `build/` 与 `logs/`（修复会话的 transcript）。上游
结论与两个输入 hash 都从 flatten manifest 装载，不重扫源码树，也不读 fetch manifest——
`fetch_files_sha256` flatten 已转录。

前置条件：flatten manifest 缺失或不可解析，或它的状态是 ok 但 `build/flat.tex` 不在 →
状态 `flatten_missing`；flatten 的状态不是 ok → 状态 `flatten_not_ok`，本次读到的 flatten
状态与它记录的 fetch 状态转录进 manifest。前置条件不满足同样写 precompile manifest：驱动器
不向调用方抛栈，每次执行的结论都落盘。

编译树：把 `src/` 内容全量拷进 `build/precompile/`，再把 `build/flat.tex` 的 bytes 写为
`build/precompile/flat.tex`，cwd 设在 `build/precompile/` 编译。宏包、图源与 `.bib` 按相对
路径就位，副产物全部落在 `build/precompile/`，`src/` 保持只读。`src/` 里本来就有同名
`flat.tex` 时它被覆盖，记一条 warning。

首次编译：`latexmk -xelatex -interaction=nonstopmode flat.tex` 恰好拉起一次，不重试，多 pass
收敛（含 bibtex）是 latexmk 自己的职责。引擎固定 xelatex，不按论文原始引擎探测，也不在失败
后降级——基线要与 compile 阶段同引擎才可比。latexmk 会派生 xelatex 子进程，故以新会话启动、
超时按进程组终止。首次编译通过即直接进入产物写出，不拉会话。

修复会话：首次编译失败（超时与 latexmk 不在 PATH 除外，那两种情形直接终止）拉起恰一次 agent
会话，现场是编译树本身，cwd 与可读写范围都限于树内。prompt 由 `skill/precompile/SKILL.md`
的内容加本次 flat.log 的错误行摘录拼成，读不到该文件 → `compile_failed`。会话经 agent 适配层
（`tongtu/agent/`）拉起，transcript 落 `logs/precompile-fix.jsonl`。会话的终止原因是 `error`
（运行时不可用或报错）→ `compile_failed`；`timeout` 与 `budget_exhausted` 不直接判死，照常进
复验——会话可能在超限之前已经把问题修完。

脚本终审：会话结束后先 `latexmk -C flat.tex` 清理编译产物，再跑一遍与首编相同的 latexmk，
出口判据与基线数据全部取自这一遍，agent 在会话内的自述不作数。

出口判据三条同时成立才是 ok：终审那次 latexmk 退出码 0；`flat.pdf` 存在且非空；页数大于 0。
任一不成立 → `compile_failed`，message 摘录 flat.log 中以 `!` 开头的错误行（至多五条）与 log
路径，log 不存在时摘 latexmk 的 stderr。不用 `latexmk -f` 强行编完求 PDF：带错误编出的 PDF，
页数与 overfull 计数不可信。同理，失败时不把解析出的五个计数记进 manifest，只记 command、
duration_seconds 与 pdf_bytes 供排查。

基线数据全部从 `build/precompile/flat.log` 解析：页数取 `Output written on …(N pages…` 行
（xelatex 路径经 `.xdv` 中转，解析不依赖文件扩展名，多次出现取最后一次）；`Overfull \hbox`
行计数；`LaTeX Warning: Reference` 与 `LaTeX Warning: Citation` 前缀的行计数（log 默认在
79 列折行，`undefined` 未必与前缀同行，故只按前缀匹配）；含 `Missing character` 的行计数。

产物写出：终审通过后把编译树里（可能已被会话修改的）`flat.tex` 拷出为 `build/precompile.tex`，
它是 mask 起下游全部阶段的输入，内容 hash 记进 manifest 的 `precompile_sha256`。改动传播的
边界：只承诺 flat.tex 的调整传播到下游，会话若改了树内其他文件，按与 `src/` 逐文件 hash 比对
检出，记入 `changed_files` 与一条 warning，但不传播。

重跑语义：输入 hash 是 `flat_sha256`（flat.tex 内容）与 `fetch_files_sha256`（`src/` 全量清单
的规范化 hash）两个值，只看前者不够——改一张图不动 `.tex` 时 flat.tex 不变，编译结果却会变。
已有 precompile manifest 可解析、状态 ok、两个 hash 与当前 flatten manifest 一致、
`build/precompile.tex` 与 `build/precompile/flat.pdf` 都存在 → 跳过，修复成果因此不必重复付
会话成本；失败状态不跳过；`force` 无视已有结论。每次非跳过的执行开始先整目录删除
`build/precompile/` 并删除 `build/precompile.tex`：旧 aux 文件会污染重编结果，失败时也不留上
次的产物误导下游。
"""

from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .. import manifests, processes, texlog, workdir
from ..agent import claude_code
from ..agent.base import STOP_REASON_ERROR, STOP_REASON_FINISHED, WorkBudget
from ..artifacts.fetch import FetchStatus
from ..artifacts.flatten import FlattenManifest, FlattenStatus
from ..artifacts.precompile import PrecompileManifest, PrecompileStatus
from ..assets import asset_path
from .flatten import FLAT_FILENAME, flat_path
from .flatten import STAGE_NAME as FLATTEN_STAGE_NAME

#: 阶段名，也是 stage manifest 的文件名主干。
STAGE_NAME = "precompile"

#: 编译树在 build/ 下的目录名，同时是 latexmk 与修复会话的 cwd。
PRECOMPILE_DIRNAME = "precompile"

#: 本阶段的输出文件名（在 build/ 下），下游阶段的输入。
PRECOMPILE_FILENAME = "precompile.tex"

#: 编译产物与日志的文件名，均由 flat.tex 的主干决定。
PDF_FILENAME = "flat.pdf"
LOG_FILENAME = "flat.log"

#: 编译驱动的可执行文件名（随 TeX 发行版分发）。
LATEXMK_EXECUTABLE = "latexmk"

#: 编译的固定选项。-xelatex 指定引擎（与 compile 阶段一致）；-interaction=nonstopmode 让
#: 报错不停在交互提示上，错误全部进 log。
LATEXMK_OPTIONS: tuple[str, ...] = ("-xelatex", "-interaction=nonstopmode")

#: 复验前清理编译产物的选项：-C 删掉 aux、log 与 PDF，让终审那次编译从干净的树开始。
LATEXMK_CLEAN_OPTIONS: tuple[str, ...] = ("-C",)

#: 单次编译的超时秒数（起步值，按真实论文的 duration_seconds 校准）。
COMPILE_TIMEOUT_SECONDS = 600

#: 清理编译产物的超时秒数：latexmk -C 只删文件，正常在一秒内返回。
CLEAN_TIMEOUT_SECONDS = 60

#: 修复会话的 prompt 资产路径；`skill/` 随仓库分发，两种布局下的定位交给 assets。
SKILL_PATH = asset_path("skill") / STAGE_NAME / "SKILL.md"

#: 修复会话 transcript 的文件名，落在工作目录的 logs/ 区。
TRACE_FILENAME = "precompile-fix.jsonl"

#: 修复会话的预算上限。轮数上限按两篇失败案例的实测轮数（11 与 15）留一倍余量定出；
#: 墙钟上限防会话卡死而非控制成本，给会话内编译大篇幅论文留出时间。
FIX_SESSION_MAX_TURNS = 30
FIX_SESSION_TIMEOUT_SECONDS = 900.0

#: prompt 里 SKILL.md 与本篇错误行摘录之间的分隔说明。
PROMPT_SEPARATOR = "\n\n---\n\n## 本篇首次编译的错误行\n\n"

#: 摘进 prompt 的 log 错误行条数上限（比 message 里的摘录宽，agent 要据此定位问题）。
PROMPT_ERROR_LINE_LIMIT = 20

#: 编译失败时摘进 message 的 log 错误行条数上限；行首前缀与提取逻辑在 texlog。
ERROR_LINE_LIMIT = 5


@dataclass(frozen=True)
class PrecompileResult:
    """驱动器的返回值：manifest、工作目录与是否命中跳过。"""

    manifest: PrecompileManifest
    workdir: workdir.Workdir
    skipped: bool


@dataclass(frozen=True)
class CompileAttempt:
    """一次编译连同它的日志解析结果：出口判据与基线数据都从这里取。"""

    outcome: processes.ProcessOutcome
    log_path: Path
    log_text: str | None
    pdf_bytes: int
    counts: texlog.LogCounts

    @property
    def passed(self) -> bool:
        """出口判据三条：退出码 0、PDF 非空、页数解析得出且大于 0。"""
        return (
            not self.outcome.timed_out and self.outcome.returncode == 0 and self.pdf_bytes > 0 and self.counts.pages > 0
        )


@dataclass(frozen=True)
class SessionRecord:
    """一次修复会话的结局，转录进 manifest 的 fix_session 与 session_ 三个字段。"""

    stop_reason: str
    model: str
    duration_seconds: float


# ------------------------------------------------------------------ 阶段驱动器


def precompile(
    workdir_name: str | None = None,
    workdir_path: Path | None = None,
    *,
    force: bool = False,
    model: str | None = None,
) -> PrecompileResult:
    """装载 flatten 结论、编译原文（必要时经修复会话修到通过），写出 precompile.tex 与 manifest。

    `workdir_name` 是工作目录名（arXiv 编号，或本地源码目录的 basename），`workdir_path`
    直接给出论文工作目录本身并覆盖前者。precompile 不访问网络，也不读源目录，两个参数只用来
    定位工作目录。`force` 无视已有结论重新执行。`model` 透传给修复会话的 agent 运行时，为
    None 时用适配层的默认模型；首次编译就通过的论文不拉会话，该参数不起作用。
    """
    paper_workdir = workdir.Workdir(workdir.resolve(workdir_name, workdir_path))
    paper_workdir.create()  # 前置条件不满足时也要写 manifest，先确保四区存在

    # 上游 flatten manifest 读不到或不可解析都转 flatten_missing，两种情形对本阶段含义相同。
    flatten_manifest = manifests.load_manifest(paper_workdir.manifest_path(FLATTEN_STAGE_NAME), FlattenManifest)
    if flatten_manifest is None:
        # 两个输入 hash 都从 flatten manifest 转录，读不到就无从做跳过判定，直接给结论。
        _reset_outputs(paper_workdir)
        return _write_result(
            paper_workdir,
            PrecompileManifest(
                status=PrecompileStatus.FLATTEN_MISSING,
                message="读不到 build/manifests/flatten.json 或它不可解析，先跑 `tongtu stage flatten`。",
            ),
        )

    if not force:
        existing = _load_skippable_manifest(paper_workdir, flatten_manifest)
        if existing is not None:
            return PrecompileResult(manifest=existing, workdir=paper_workdir, skipped=True)

    _reset_outputs(paper_workdir)
    if flatten_manifest.status is not FlattenStatus.OK:
        return _write_result(
            paper_workdir,
            _manifest_from_flatten(
                PrecompileStatus.FLATTEN_NOT_OK,
                flatten_manifest,
                message=(
                    f"flatten 的状态是 {flatten_manifest.status}，上游 fetch 判定源是 PDF 而非 LaTeX 源码，"
                    "没有可编译的原文，走 degraded path。"
                    if flatten_manifest.fetch_status == FetchStatus.PDF_ONLY
                    else f"flatten 的状态是 {flatten_manifest.status}，不是 ok，先重跑 `tongtu stage flatten`。"
                ),
            ),
        )
    if not flat_path(paper_workdir).is_file():
        return _write_result(
            paper_workdir,
            _manifest_from_flatten(
                PrecompileStatus.FLATTEN_MISSING,
                flatten_manifest,
                message=(
                    f"flatten 的状态是 ok，但 build/{FLAT_FILENAME} 不是文件，"
                    "没有可编译的原文，先跑 `tongtu stage flatten`。"
                ),
            ),
        )

    try:
        manifest = _compile(paper_workdir, flatten_manifest, model)
    except Exception as error:  # 拷贝、编译、会话与日志解析的异常类型多样，统一转状态
        manifest = _manifest_from_flatten(
            PrecompileStatus.COMPILE_FAILED, flatten_manifest, message=manifests.describe_error(error)
        )
    return _write_result(paper_workdir, manifest)


def _compile(
    paper_workdir: workdir.Workdir, flatten_manifest: FlattenManifest, model: str | None
) -> PrecompileManifest:
    """前置条件满足之后的主流程：组装编译树、首次编译，通过即写产物，不通过转修复会话。"""
    tree = _precompile_dir(paper_workdir)
    warnings = _assemble_tree(paper_workdir, tree)
    command = [LATEXMK_EXECUTABLE, *LATEXMK_OPTIONS, FLAT_FILENAME]
    try:
        attempt = _attempt_compile(command, tree)
    except OSError as error:
        return _manifest_from_flatten(
            PrecompileStatus.COMPILE_FAILED,
            flatten_manifest,
            command=command,
            warnings=warnings,
            message=(
                f"执行 latexmk 失败（{manifests.describe_error(error)}）。latexmk 随 TeX 发行版分发，"
                "确认已安装 TeX 发行版且 latexmk 在 PATH 里。"
            ),
        )
    if attempt.outcome.timed_out:
        # 编译本身就跑不完的论文不是修复会话能处理的对象，会话内的每次编译同样会超时。
        return _timeout_manifest(flatten_manifest, command, attempt, warnings, session=None)
    if attempt.passed:
        return _success_manifest(
            paper_workdir, tree, flatten_manifest, command, attempt, warnings, session=None, changed_files=[]
        )
    return _fix_and_verify(paper_workdir, tree, flatten_manifest, command, attempt, warnings, model)


# ------------------------------------------------------------------ 编译树与编译


def _assemble_tree(paper_workdir: workdir.Workdir, tree: Path) -> list[str]:
    r"""把 `src/` 内容全量拷进编译树，再把 `build/flat.tex` 的 bytes 写为其中的 flat.tex。

    返回 warnings：`src/` 里本来就有同名 flat.tex 时它被覆盖，记一条。拷贝不过滤、不改
    字节，`.cls` / `.sty` / 图源 / `.bib` 都按原相对路径就位。
    """
    shutil.copytree(paper_workdir.src, tree, dirs_exist_ok=True)
    warnings: list[str] = []
    tree_flat_path = tree / FLAT_FILENAME
    if tree_flat_path.exists():
        warnings.append(f"src/ 里本来就有 {FLAT_FILENAME}，编译树里的这一份已被 build/{FLAT_FILENAME} 覆盖")
    tree_flat_path.write_bytes(flat_path(paper_workdir).read_bytes())
    return warnings


def _attempt_compile(command: list[str], tree: Path) -> CompileAttempt:
    """执行一次 latexmk 并读取它的产物：日志文本、PDF 字节数与五个计数一并带回。

    latexmk 会派生 xelatex 子进程，故经 processes 以新会话启动、超时按进程组终止。stdout
    与 stderr 捕获在内存里，不落盘——诊断信息以 flat.log 为准。
    """
    outcome = processes.run_in_process_group(command, tree, COMPILE_TIMEOUT_SECONDS)
    log_path = tree / LOG_FILENAME
    log_text = texlog.read_log(log_path)
    pdf_path = tree / PDF_FILENAME
    return CompileAttempt(
        outcome=outcome,
        log_path=log_path,
        log_text=log_text,
        pdf_bytes=pdf_path.stat().st_size if pdf_path.is_file() else 0,
        counts=texlog.parse_counts(log_text),
    )


# ------------------------------------------------------------------ 修复会话与脚本终审


def _fix_and_verify(
    paper_workdir: workdir.Workdir,
    tree: Path,
    flatten_manifest: FlattenManifest,
    command: list[str],
    first_attempt: CompileAttempt,
    warnings: list[str],
    model: str | None,
) -> PrecompileManifest:
    """首次编译不过时的路径：拉起一次修复会话，清理编译产物后自己复验，结论取自复验那一遍。"""
    prompt = _build_prompt(first_attempt)
    if prompt is None:
        return _manifest_from_flatten(
            PrecompileStatus.COMPILE_FAILED,
            flatten_manifest,
            command=command,
            pdf_bytes=first_attempt.pdf_bytes,
            duration_seconds=first_attempt.outcome.duration_seconds,
            warnings=warnings,
            message=(
                f"首次编译未过出口判据，修复会话的 prompt 资产读不到（{SKILL_PATH}），会话没有拉起。"
                f"首次编译的失败原因：{_failure_message(first_attempt)}"
            ),
        )

    snapshot = _snapshot_tree_files(paper_workdir.src, tree)
    session_model = model or claude_code.DEFAULT_MODEL
    started = time.monotonic()
    session_outcome = claude_code.work(
        prompt=prompt,
        workdir=tree,
        model=model,
        budget=WorkBudget(max_turns=FIX_SESSION_MAX_TURNS, timeout_seconds=FIX_SESSION_TIMEOUT_SECONDS),
        trace_path=paper_workdir.logs / TRACE_FILENAME,
    )
    session = SessionRecord(
        stop_reason=session_outcome.stop_reason,
        model=session_model,
        duration_seconds=time.monotonic() - started,
    )
    if session_outcome.stop_reason == STOP_REASON_ERROR:
        return _manifest_from_flatten(
            PrecompileStatus.COMPILE_FAILED,
            flatten_manifest,
            command=command,
            pdf_bytes=first_attempt.pdf_bytes,
            duration_seconds=first_attempt.outcome.duration_seconds,
            warnings=warnings,
            message=f"首次编译未过出口判据，修复会话未能执行：{session_outcome.detail}",
            **_session_fields(session),
        )
    if session_outcome.stop_reason != STOP_REASON_FINISHED:
        # 超时与轮数耗尽都不直接判死：会话可能在超限之前已经把问题修完，交给复验裁决。
        warnings.append(f"修复会话以 {session_outcome.stop_reason} 结束，结论仍由脚本复验给出")

    warnings.extend(_clean_tree(tree))
    try:
        verify_attempt = _attempt_compile(command, tree)
    except OSError as error:
        return _manifest_from_flatten(
            PrecompileStatus.COMPILE_FAILED,
            flatten_manifest,
            command=command,
            warnings=warnings,
            message=f"修复会话结束后复验时执行 latexmk 失败（{manifests.describe_error(error)}）。",
            **_session_fields(session),
        )
    if verify_attempt.outcome.timed_out:
        return _timeout_manifest(flatten_manifest, command, verify_attempt, warnings, session=session)
    if not verify_attempt.passed:
        return _manifest_from_flatten(
            PrecompileStatus.COMPILE_FAILED,
            flatten_manifest,
            command=command,
            pdf_bytes=verify_attempt.pdf_bytes,
            duration_seconds=verify_attempt.outcome.duration_seconds,
            warnings=warnings,
            message=f"经过修复会话，复验编译仍未过出口判据：{_failure_message(verify_attempt)}",
            **_session_fields(session),
        )

    changed_files = _detect_changed_files(tree, snapshot)
    if changed_files:
        warnings.append(
            f"修复会话改动了 flat.tex 之外的 {len(changed_files)} 个文件："
            f"{'、'.join(changed_files)}；这些改动不传播到下游，compile 阶段的编译树仍从 src/ 组装"
        )
    return _success_manifest(
        paper_workdir,
        tree,
        flatten_manifest,
        command,
        verify_attempt,
        warnings,
        session=session,
        changed_files=changed_files,
    )


def _build_prompt(attempt: CompileAttempt) -> str | None:
    """拼修复会话的 prompt：SKILL.md 的内容加本次编译的错误行摘录；读不到资产返回 None。"""
    try:
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    return skill_text + PROMPT_SEPARATOR + _error_excerpt(attempt)


def _error_excerpt(attempt: CompileAttempt) -> str:
    """取本次编译 log 里以 `!` 开头的错误行（至多 PROMPT_ERROR_LINE_LIMIT 行）。

    log 读不到时改摘 latexmk 的 stderr；log 在但没有错误行时如实说明，让 agent 自己去读日志。
    """
    if attempt.log_text is None:
        stderr = attempt.outcome.stderr_text.strip()[: processes.OUTPUT_EXCERPT_CHARS]
        return f"读不到 {attempt.log_path}。latexmk 的 stderr：\n\n{stderr}\n"
    error_lines = texlog.error_lines(attempt.log_text, PROMPT_ERROR_LINE_LIMIT)
    if not error_lines:
        return (
            f"{attempt.log_path} 里没有以 {texlog.ERROR_LINE_PREFIX} 开头的错误行，"
            f"完整日志在编译树的 {LOG_FILENAME}。\n"
        )
    excerpt = "\n".join(error_lines)
    return f"（至多 {PROMPT_ERROR_LINE_LIMIT} 行，完整日志在编译树的 {LOG_FILENAME}）\n\n```\n{excerpt}\n```\n"


def _clean_tree(tree: Path) -> list[str]:
    """复验前用 `latexmk -C` 清掉上一轮的 aux、log 与 PDF；清理失败只记 warning，不拦复验。"""
    command = [LATEXMK_EXECUTABLE, *LATEXMK_CLEAN_OPTIONS, FLAT_FILENAME]
    try:
        outcome = processes.run_in_process_group(command, tree, CLEAN_TIMEOUT_SECONDS)
    except OSError as error:
        return [f"复验前清理编译产物失败（{manifests.describe_error(error)}）"]
    if outcome.timed_out:
        return [f"复验前清理编译产物超过 {CLEAN_TIMEOUT_SECONDS} 秒超时上限，已按进程组终止"]
    if outcome.returncode != 0:
        return [f"复验前清理编译产物的 latexmk 退出码 {outcome.returncode}"]
    return []


def _snapshot_tree_files(src: Path, tree: Path) -> dict[str, str]:
    """会话之前给编译树里「在 `src/` 也存在的文件」记 sha256，flat.tex 除外。

    快照在组装编译树之后、会话之前取，此时这些文件与 `src/` 的对应文件逐字节相同，故会话
    之后拿它比对即是与 `src/` 比对。flat.tex 不在快照里：它相对 `build/flat.tex` 的差异是
    预期改动，本来就要传播到下游。
    """
    snapshot: dict[str, str] = {}
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(src).as_posix()
        if relative == FLAT_FILENAME:
            continue
        tree_path = tree / relative
        if tree_path.is_file():
            snapshot[relative] = _file_sha256(tree_path)
    return snapshot


def _detect_changed_files(tree: Path, snapshot: dict[str, str]) -> list[str]:
    """会话之后逐个比对快照里的文件，内容变了或文件没了都记入改动清单（src/ 相对路径）。"""
    changed: list[str] = []
    for relative, digest in snapshot.items():
        tree_path = tree / relative
        if not tree_path.is_file() or _file_sha256(tree_path) != digest:
            changed.append(relative)
    return changed


def _file_sha256(path: Path) -> str:
    """文件内容的 sha256，经 `hashlib.file_digest` 分块读取，不把整个文件读进内存。"""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


# ------------------------------------------------------------------ 出口判据


def _failure_message(attempt: CompileAttempt) -> str:
    """编译未过出口判据时的说明：不成立的判据 + log 错误行摘录（log 不在则摘 stderr）。"""
    reasons: list[str] = []
    if attempt.outcome.returncode != 0:
        reasons.append(f"latexmk 退出码 {attempt.outcome.returncode}")
    if attempt.pdf_bytes == 0:
        reasons.append(f"{PDF_FILENAME} 不存在或为空")
    if attempt.counts.pages <= 0:
        reasons.append(f"{LOG_FILENAME} 里解析不出页数")
    if attempt.log_text is None:
        stderr = attempt.outcome.stderr_text.strip()[: processes.OUTPUT_EXCERPT_CHARS]
        detail = f"读不到 {attempt.log_path}；latexmk 的 stderr：{stderr}"
    else:
        error_lines = texlog.error_lines(attempt.log_text, ERROR_LINE_LIMIT)
        if error_lines:
            excerpt = " | ".join(error_lines)
            detail = f"log 的错误行（至多 {ERROR_LINE_LIMIT} 条）：{excerpt}；完整日志：{attempt.log_path}"
        else:
            detail = f"log 里没有以 {texlog.ERROR_LINE_PREFIX} 开头的错误行；完整日志：{attempt.log_path}"
    return f"{'；'.join(reasons)}。{detail}"


# ------------------------------------------------------------------ 产物写出与 manifest 组装


def _success_manifest(
    paper_workdir: workdir.Workdir,
    tree: Path,
    flatten_manifest: FlattenManifest,
    command: list[str],
    attempt: CompileAttempt,
    warnings: list[str],
    *,
    session: SessionRecord | None,
    changed_files: list[str],
) -> PrecompileManifest:
    """出口判据全部成立时：把树内 flat.tex 拷出为 build/precompile.tex，组装 ok manifest。"""
    content = (tree / FLAT_FILENAME).read_bytes()
    _precompile_path(paper_workdir).write_bytes(content)
    return _manifest_from_flatten(
        PrecompileStatus.OK,
        flatten_manifest,
        precompile_sha256=hashlib.sha256(content).hexdigest(),
        precompile_bytes=len(content),
        command=command,
        pages=attempt.counts.pages,
        pdf_bytes=attempt.pdf_bytes,
        overfull_hboxes=attempt.counts.overfull_hboxes,
        undefined_references=attempt.counts.undefined_references,
        undefined_citations=attempt.counts.undefined_citations,
        missing_characters=attempt.counts.missing_characters,
        duration_seconds=attempt.outcome.duration_seconds,
        changed_files=changed_files,
        warnings=warnings,
        **_session_fields(session),
    )


def _timeout_manifest(
    flatten_manifest: FlattenManifest,
    command: list[str],
    attempt: CompileAttempt,
    warnings: list[str],
    *,
    session: SessionRecord | None,
) -> PrecompileManifest:
    """编译超时的 manifest：五个计数不可信，只记 command、pdf_bytes 与耗时。"""
    return _manifest_from_flatten(
        PrecompileStatus.COMPILE_FAILED,
        flatten_manifest,
        command=command,
        pdf_bytes=attempt.pdf_bytes,
        duration_seconds=attempt.outcome.duration_seconds,
        warnings=warnings,
        message=(f"latexmk 执行超过 {COMPILE_TIMEOUT_SECONDS} 秒超时上限，已按进程组终止；log：{attempt.log_path}"),
        **_session_fields(session),
    )


def _session_fields(session: SessionRecord | None) -> dict[str, object]:
    """修复会话的记录转成 manifest 字段；没拉起会话时 fix_session 为 False，三个字段留默认值。"""
    if session is None:
        return {"fix_session": False}
    return {
        "fix_session": True,
        "session_stop_reason": session.stop_reason,
        "session_model": session.model,
        "session_duration_seconds": session.duration_seconds,
    }


def _manifest_from_flatten(
    status: PrecompileStatus, flatten_manifest: FlattenManifest, **fields: object
) -> PrecompileManifest:
    """组装 manifest：两个输入 hash 与上游两个状态一律从 flatten manifest 转录，其余字段由调用处给出。"""
    return PrecompileManifest(
        status=status,
        flat_sha256=flatten_manifest.flat_sha256,
        fetch_files_sha256=flatten_manifest.fetch_files_sha256,
        flatten_status=str(flatten_manifest.status),
        fetch_status=flatten_manifest.fetch_status,
        **fields,
    )


# ------------------------------------------------------------------ 装载、跳过判定与落盘


def _load_skippable_manifest(
    paper_workdir: workdir.Workdir, flatten_manifest: FlattenManifest
) -> PrecompileManifest | None:
    """读已有 precompile manifest；可解析、状态 ok、两个输入 hash 一致且两件产物都在，返回它，否则返回 None。"""
    manifest = manifests.load_manifest(paper_workdir.manifest_path(STAGE_NAME), PrecompileManifest)
    if manifest is None:
        return None
    if manifest.status is not PrecompileStatus.OK:
        return None
    if manifest.flat_sha256 != flatten_manifest.flat_sha256:
        return None
    if manifest.fetch_files_sha256 != flatten_manifest.fetch_files_sha256:
        return None
    if not _precompile_path(paper_workdir).is_file():
        return None
    if not _pdf_path(paper_workdir).is_file():
        return None
    return manifest


def _precompile_dir(paper_workdir: workdir.Workdir) -> Path:
    """编译树目录。"""
    return paper_workdir.build / PRECOMPILE_DIRNAME


def _precompile_path(paper_workdir: workdir.Workdir) -> Path:
    """本阶段的输出文件路径：编译通过的原文，下游阶段的输入。"""
    return paper_workdir.build / PRECOMPILE_FILENAME


def _pdf_path(paper_workdir: workdir.Workdir) -> Path:
    """编译产出的 PDF 路径。"""
    return _precompile_dir(paper_workdir) / PDF_FILENAME


def _reset_outputs(paper_workdir: workdir.Workdir) -> None:
    """整目录删除编译树并删除 precompile.tex：旧 aux 文件会污染重编结果，失败时也不留旧产物。"""
    shutil.rmtree(_precompile_dir(paper_workdir), ignore_errors=True)
    _precompile_path(paper_workdir).unlink(missing_ok=True)


def _write_result(paper_workdir: workdir.Workdir, manifest: PrecompileManifest) -> PrecompileResult:
    """写出 manifest 并组装返回值；除跳过外的每次执行（含失败）都经此处落盘。"""
    manifests.write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return PrecompileResult(manifest=manifest, workdir=paper_workdir, skipped=False)
