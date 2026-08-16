r"""flatten 阶段驱动器：把 `src/` 的多文件源码展开成单文件 `build/flat.tex`。

flatten 只读 `src/` 与 `build/manifests/fetch.json`，只写 `build/`。上游结论从 fetch
manifest 装载，不重扫源码树：fetch manifest 的 `files` 是输入 hash 的来源，`tex_files`
是主文件候选的枚举来源。

前置条件：fetch manifest 缺失或不可解析 → 状态 `fetch_missing`；fetch 的状态不是 ok →
状态 `fetch_not_ok`，本次读到的 fetch 状态转录进 manifest 的 `fetch_status`。前置条件不
满足同样写 flatten manifest：驱动器不向调用方抛栈，每次执行的结论都落盘。

注释判定的规则（主文件判定、bbl 内联、残留检查共用）：一行里第一个「前一字节不是反斜杠
的 %」起是注释，只看它之前的部分。

主文件判定：候选是 `tex_files` 里在注释外出现 \documentclass 或 \documentstyle 的文件，
读不到的文件按没有该候选处理并记一条 warning。规则按顺序收敛：候选恰一个即主文件；多于
一个则只留在注释外含 \begin{document} 的候选（筛成空集则退回筛选前的集合）；仍多于一个则
取基名为 main.tex 的唯一候选；仍收敛不到唯一结果 → 状态 `main_ambiguous`，message 列出
全部候选；候选为空 → 状态 `main_not_found`。

展开：在 `src/` 下执行 `latexpand --keep-comments --fatal <主文件相对路径>`，stdout 按
bytes 捕获。latexpand 不在 PATH 或退出码非 0 → 状态 `expand_failed`，stderr 摘入
message；stderr 的非空行逐行记入 warnings。

bbl 内联在 latexpand 成功之后、写盘之前对输出 bytes 做：主文件同目录存在同主干 `.bbl`
时，逐行在注释外找 \bibliography{...} 命令（花括号参数不跨行），恰一处则用 `.bbl` 的
bytes 整体替换该命令，该行命令前后的其余文本保留；零处或多处不内联并记一条 warning。
没有同主干 `.bbl` 时不内联也不记 warning。字节级操作，不做编码转换。

出口检查：输出非空且含 \begin{document} 与 \end{document}（bytes 包含检查），不满足 →
状态 `expand_failed`；通过才写 `build/flat.tex` 并记 `flat_sha256` 与 `flat_bytes`。展开
后仍在注释外含 \input{ 或 \include{ 的行只记 warning，不判失败。

重跑语义：输入 hash 是 fetch manifest 的 `files` 清单的规范化 hash（按路径排序，每行
「路径 + 制表符 + sha256 + 换行」，UTF-8 编码后取 sha256）。已有 flatten manifest 可解析、
状态 ok、`fetch_files_sha256` 与本次算出的一致、`build/flat.tex` 存在 → 跳过；失败状态不
跳过；`force` 无视已有结论。每次非跳过的执行开始先删除已有 `build/flat.tex`，失败时不留
上次的产物误导下游。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from .. import workdir
from ..artifacts.fetch import FetchManifest, FetchStatus
from ..artifacts.flatten import FlattenManifest, FlattenStatus
from .fetch import STAGE_NAME as FETCH_STAGE_NAME

#: 阶段名，也是 stage manifest 的文件名主干。
STAGE_NAME = "flatten"

#: 展开结果在 build/ 下的文件名。
FLAT_FILENAME = "flat.tex"

#: 展开工具的可执行文件名（随 TeX Live 分发）。
LATEXPAND_EXECUTABLE = "latexpand"

#: 展开工具的固定选项。--keep-comments 保留注释与 \end{document} 之后的内容（默认会剥掉，
#: 是有损变换）；--fatal 让 \input 指向的文件找不到时立刻失败，不留残缺产物。
LATEXPAND_OPTIONS: tuple[str, ...] = ("--keep-comments", "--fatal")

#: 主文件候选的判定标记：出现其一即为候选（\documentstyle 是 LaTeX 2.09 的写法）。
DOCUMENT_CLASS_MARKERS: tuple[bytes, ...] = (rb"\documentclass", rb"\documentstyle")

#: 正文区起止标记，用于候选筛选与出口检查。
BEGIN_DOCUMENT_MARKER = rb"\begin{document}"
END_DOCUMENT_MARKER = rb"\end{document}"

#: 候选仍不唯一时优先采用的基名。
MAIN_FILE_BASENAME = "main.tex"

#: 残留检查的标记：展开后注释外仍出现其一，说明有文件没被展开。
INPUT_MARKERS: tuple[bytes, ...] = (rb"\input{", rb"\include{")

#: bbl 内联的替换目标。写成正则以便一次拿到整条命令的起止位置；\bibliographystyle 的
#: 反斜杠命令名之后不是花括号，不会被它匹配。
BIBLIOGRAPHY_COMMAND_RE = re.compile(rb"\\bibliography\s*\{[^}]*\}")

#: 注释起始字符与它的转义前缀。
COMMENT_CHAR = b"%"
BACKSLASH = b"\\"

#: latexpand 失败时摘进 message 的 stderr 字符数上限；完整 stderr 逐行记入 warnings。
STDERR_MESSAGE_CHARS = 500


@dataclass(frozen=True)
class FlattenResult:
    """驱动器的返回值：manifest、工作目录与是否命中跳过。"""

    manifest: FlattenManifest
    workdir: workdir.Workdir
    skipped: bool


# ------------------------------------------------------------------ 阶段驱动器


def flatten(
    workdir_name: str | None = None,
    workdir_path: Path | None = None,
    *,
    force: bool = False,
) -> FlattenResult:
    """装载 fetch 结论、判定主文件、展开并内联 bbl，写出 flat.tex 与 manifest。

    `workdir_name` 是工作目录名（arXiv 编号，或本地源码目录的 basename），`workdir_path`
    直接给出论文工作目录本身并覆盖前者。flatten 不访问网络，也不读源目录，两个参数只用来
    定位工作目录。`force` 无视已有结论重新执行。
    """
    paper_workdir = workdir.Workdir(workdir.resolve(workdir_name, workdir_path))
    paper_workdir.create()  # 前置条件不满足时也要写 manifest，先确保四区存在

    fetch_manifest = _load_fetch_manifest(paper_workdir.manifest_path(FETCH_STAGE_NAME))
    if fetch_manifest is None:
        # 算不出输入 hash，跳过判定无从成立，直接给结论。
        _flat_path(paper_workdir).unlink(missing_ok=True)
        return _write_result(
            paper_workdir,
            FlattenManifest(
                status=FlattenStatus.FETCH_MISSING,
                message="读不到 build/manifests/fetch.json 或它不可解析，先跑 `tongtu stage fetch`。",
            ),
        )

    fetch_files_sha256 = _fetch_files_hash(fetch_manifest.files)
    if not force:
        existing = _load_skippable_manifest(paper_workdir, fetch_files_sha256)
        if existing is not None:
            return FlattenResult(manifest=existing, workdir=paper_workdir, skipped=True)

    _flat_path(paper_workdir).unlink(missing_ok=True)
    if fetch_manifest.status is not FetchStatus.OK:
        return _write_result(
            paper_workdir,
            FlattenManifest(
                status=FlattenStatus.FETCH_NOT_OK,
                fetch_files_sha256=fetch_files_sha256,
                fetch_status=str(fetch_manifest.status),
                message=(
                    f"fetch 的状态是 {fetch_manifest.status}，不是 ok，没有可展开的 LaTeX 源码树。"
                    if fetch_manifest.status is FetchStatus.PDF_ONLY
                    else f"fetch 的状态是 {fetch_manifest.status}，不是 ok，先重跑 `tongtu stage fetch`。"
                ),
            ),
        )

    try:
        manifest = _expand(paper_workdir, fetch_manifest, fetch_files_sha256)
    except Exception as error:  # 展开与内联过程的异常类型多样，统一转状态
        manifest = FlattenManifest(
            status=FlattenStatus.EXPAND_FAILED,
            fetch_files_sha256=fetch_files_sha256,
            fetch_status=str(fetch_manifest.status),
            message=_describe_error(error),
        )
    return _write_result(paper_workdir, manifest)


def _expand(paper_workdir: workdir.Workdir, fetch_manifest: FetchManifest, fetch_files_sha256: str) -> FlattenManifest:
    """前置条件满足之后的主流程：判定主文件、执行 latexpand、内联 bbl、出口检查、写 flat.tex。"""
    src = paper_workdir.src
    fetch_status = str(fetch_manifest.status)
    candidates, with_begin_document, warnings = _scan_candidates(src, fetch_manifest.tex_files)
    if not candidates:
        return FlattenManifest(
            status=FlattenStatus.MAIN_NOT_FOUND,
            fetch_files_sha256=fetch_files_sha256,
            fetch_status=fetch_status,
            warnings=warnings,
            message="fetch 记录的 .tex 文件里没有一个在注释外含 \\documentclass 或 \\documentstyle，判定不出主文件。",
        )
    main_file = _select_main_file(candidates, with_begin_document)
    if main_file is None:
        return FlattenManifest(
            status=FlattenStatus.MAIN_AMBIGUOUS,
            candidates=candidates,
            fetch_files_sha256=fetch_files_sha256,
            fetch_status=fetch_status,
            warnings=warnings,
            message=f"主文件候选不唯一，判定规则收敛不到单个结果，候选：{'、'.join(candidates)}",
        )

    command = [LATEXPAND_EXECUTABLE, *LATEXPAND_OPTIONS, main_file]
    try:
        completed = subprocess.run(command, cwd=src, capture_output=True, check=False)
    except OSError as error:
        return FlattenManifest(
            status=FlattenStatus.EXPAND_FAILED,
            main_file=main_file,
            candidates=candidates,
            fetch_files_sha256=fetch_files_sha256,
            fetch_status=fetch_status,
            command=command,
            warnings=warnings,
            message=(
                f"执行 latexpand 失败（{_describe_error(error)}）。latexpand 随 TeX Live 分发，"
                "确认已安装 TeX 发行版、latexpand 在 PATH 里，且工作目录的 src/ 存在。"
            ),
        )
    stderr_text = completed.stderr.decode("utf-8", errors="replace")
    warnings.extend(line for line in stderr_text.splitlines() if line.strip())
    if completed.returncode != 0:
        return FlattenManifest(
            status=FlattenStatus.EXPAND_FAILED,
            main_file=main_file,
            candidates=candidates,
            fetch_files_sha256=fetch_files_sha256,
            fetch_status=fetch_status,
            command=command,
            warnings=warnings,
            message=f"latexpand 退出码 {completed.returncode}；stderr：{stderr_text.strip()[:STDERR_MESSAGE_CHARS]}",
        )

    output, bbl_file, bbl_warnings = _inline_bbl(completed.stdout, src, main_file)
    warnings.extend(bbl_warnings)
    exit_check_message = _exit_check_message(output)
    if exit_check_message:
        return FlattenManifest(
            status=FlattenStatus.EXPAND_FAILED,
            main_file=main_file,
            candidates=candidates,
            fetch_files_sha256=fetch_files_sha256,
            fetch_status=fetch_status,
            bbl_file=bbl_file,
            command=command,
            warnings=warnings,
            message=exit_check_message,
        )

    residual_lines = _count_residual_input_lines(output)
    if residual_lines:
        warnings.append(f"展开后仍有 {residual_lines} 行在注释外含 \\input{{ 或 \\include{{，这些文件没有被展开")
    _flat_path(paper_workdir).write_bytes(output)
    return FlattenManifest(
        status=FlattenStatus.OK,
        main_file=main_file,
        candidates=candidates,
        fetch_files_sha256=fetch_files_sha256,
        fetch_status=fetch_status,
        bbl_file=bbl_file,
        flat_sha256=hashlib.sha256(output).hexdigest(),
        flat_bytes=len(output),
        command=command,
        warnings=warnings,
    )


# ------------------------------------------------------------------ 主文件判定


def _scan_candidates(src: Path, tex_files: list[str]) -> tuple[list[str], set[str], list[str]]:
    r"""扫描 fetch 记录的 .tex 清单，返回（候选清单、其中含 \begin{document} 的路径集合、warnings）。

    候选是在注释外出现 \documentclass 或 \documentstyle 的文件；读取失败的文件按没有该
    候选处理，并记一条 warning。
    """
    candidates: list[str] = []
    with_begin_document: set[str] = set()
    warnings: list[str] = []
    for relative in tex_files:
        try:
            content = (src / relative).read_bytes()
        except OSError as error:
            warnings.append(f"主文件判定时读不到 {relative}：{_describe_error(error)}")
            continue
        has_document_class = False
        has_begin_document = False
        for line in content.splitlines():
            code = _code_before_comment(line)
            has_document_class = has_document_class or any(marker in code for marker in DOCUMENT_CLASS_MARKERS)
            has_begin_document = has_begin_document or BEGIN_DOCUMENT_MARKER in code
        if has_document_class:
            candidates.append(relative)
            if has_begin_document:
                with_begin_document.add(relative)
    return candidates, with_begin_document, warnings


def _select_main_file(candidates: list[str], with_begin_document: set[str]) -> str | None:
    r"""按顺序收敛主文件：候选恰一个 → 含 \begin{document} 的候选 → 基名 main.tex。

    收敛不到单个结果返回 None（调用方转 main_ambiguous）。含 \begin{document} 的筛选筛成
    空集时退回筛选前的集合，再看基名。
    """
    if len(candidates) == 1:
        return candidates[0]
    remaining = candidates
    with_document = [path for path in remaining if path in with_begin_document]
    if len(with_document) == 1:
        return with_document[0]
    if with_document:
        remaining = with_document
    named_main = [path for path in remaining if PurePosixPath(path).name == MAIN_FILE_BASENAME]
    if len(named_main) == 1:
        return named_main[0]
    return None


# ------------------------------------------------------------------ bbl 内联与出口检查


def _inline_bbl(output: bytes, src: Path, main_file: str) -> tuple[bytes, str, list[str]]:
    r"""主文件同目录有同主干 .bbl 时把它内联进展开结果。

    返回（内联后的 bytes、内联的 bbl 相对路径、warnings）。逐行只在注释外找
    \bibliography{...} 命令（花括号参数不跨行）：恰一处才用 .bbl 的 bytes 整体替换该命令，
    该行命令前后的其余文本保留；零处或多处不内联并记一条 warning。没有同主干 .bbl 时原样
    返回，不记 warning。
    """
    bbl_relative = str(PurePosixPath(main_file).with_suffix(".bbl"))
    bbl_path = src / bbl_relative
    if not bbl_path.is_file():
        return output, "", []
    lines = output.splitlines(keepends=True)
    matches: list[tuple[int, re.Match[bytes]]] = []
    for index, line in enumerate(lines):
        code = _code_before_comment(line)
        matches.extend((index, match) for match in BIBLIOGRAPHY_COMMAND_RE.finditer(code))
    if len(matches) != 1:
        return (
            output,
            "",
            [
                f"主文件同目录有 {bbl_relative}，但展开结果里注释外的 \\bibliography 命令有 {len(matches)} 处"
                "（内联要求恰一处），未内联"
            ],
        )
    index, match = matches[0]
    line = lines[index]
    lines[index] = line[: match.start()] + bbl_path.read_bytes() + line[match.end() :]
    return b"".join(lines), bbl_relative, []


def _exit_check_message(output: bytes) -> str:
    r"""出口检查：输出非空且含 \begin{document} 与 \end{document}。通过返回空串，否则返回失败说明。"""
    if not output:
        return "latexpand 的输出为空。"
    missing = [marker.decode() for marker in (BEGIN_DOCUMENT_MARKER, END_DOCUMENT_MARKER) if marker not in output]
    if missing:
        return f"展开结果缺少 {'、'.join(missing)}，不是一份完整的文档。"
    return ""


def _count_residual_input_lines(output: bytes) -> int:
    r"""数展开后仍在注释外含 \input{ 或 \include{ 的行数。"""
    return sum(
        1 for line in output.splitlines() if any(marker in _code_before_comment(line) for marker in INPUT_MARKERS)
    )


def _code_before_comment(line: bytes) -> bytes:
    """取一行里注释之前的部分：第一个前一字节不是反斜杠的 % 起是注释。"""
    index = line.find(COMMENT_CHAR)
    while index != -1:
        if index == 0 or line[index - 1 : index] != BACKSLASH:
            return line[:index]
        index = line.find(COMMENT_CHAR, index + 1)
    return line


# ------------------------------------------------------------------ 输入 hash、跳过判定与落盘


def _fetch_files_hash(files: dict[str, str]) -> str:
    """算 fetch manifest 的 files 清单的规范化 hash：按路径排序，每行「路径 + 制表符 + sha256 + 换行」。"""
    listing = "".join(f"{path}\t{files[path]}\n" for path in sorted(files))
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()


def _load_fetch_manifest(path: Path) -> FetchManifest | None:
    """读上游 fetch manifest；缺失或不可解析返回 None（调用方转 fetch_missing）。"""
    try:
        return FetchManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


def _load_skippable_manifest(paper_workdir: workdir.Workdir, fetch_files_sha256: str) -> FlattenManifest | None:
    """读已有 flatten manifest；可解析、状态 ok、输入 hash 一致且 flat.tex 在，返回它，否则返回 None。"""
    try:
        manifest = FlattenManifest.model_validate_json(
            paper_workdir.manifest_path(STAGE_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError):
        return None
    if manifest.status is not FlattenStatus.OK:
        return None
    if manifest.fetch_files_sha256 != fetch_files_sha256:
        return None
    if not _flat_path(paper_workdir).is_file():
        return None
    return manifest


def _flat_path(paper_workdir: workdir.Workdir) -> Path:
    """展开结果的路径。"""
    return paper_workdir.build / FLAT_FILENAME


def _write_result(paper_workdir: workdir.Workdir, manifest: FlattenManifest) -> FlattenResult:
    """写出 manifest 并组装返回值；除跳过外的每次执行（含失败）都经此处落盘。"""
    path = paper_workdir.manifest_path(STAGE_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return FlattenResult(manifest=manifest, workdir=paper_workdir, skipped=False)


def _describe_error(error: Exception) -> str:
    """异常统一格式化成「类型名：信息」，记入 manifest 的 message。"""
    return f"{type(error).__name__}：{error}"
