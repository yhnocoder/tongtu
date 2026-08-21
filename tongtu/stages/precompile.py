from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import model, processes, texlog
from ..artifacts.common import CompileReport, FixSession
from ..artifacts.precompile import PrecompileManifest, PrecompileStatus
from ..assets import asset_path
from ..manifests import describe_error, write_manifest
from ..model.config import RoleTable, load_config, resolve_role
from ..model.work import StopReason
from ..workdir import Workdir

STAGE_NAME = "precompile"

ROLE = "precompile_fix"

PRECOMPILE_DIRNAME = "precompile"

PRECOMPILE_FILENAME = "precompile.tex"

FLAT_FILENAME = "flat.tex"

PDF_FILENAME = "flat.pdf"

LOG_FILENAME = "flat.log"

TRACE_FILENAME = "precompile-fix.jsonl"

FONTS_DIRNAME = "fonts"

FONTS_DIR = asset_path(FONTS_DIRNAME)

LATEXPAND_COMMAND: tuple[str, ...] = ("latexpand", "--keep-comments", "--fatal")

LATEXMK_COMMAND: tuple[str, ...] = ("latexmk", "-xelatex", "-interaction=nonstopmode", FLAT_FILENAME)

LATEXMK_CLEAN_COMMAND: tuple[str, ...] = ("latexmk", "-C", FLAT_FILENAME)

COMPILE_TIMEOUT_SECONDS = 600

CLEAN_TIMEOUT_SECONDS = 60

ERROR_LINE_LIMIT = 5

DOCUMENT_CLASS_MARKERS: tuple[bytes, ...] = (rb"\documentclass", rb"\documentstyle")

BEGIN_DOCUMENT_MARKER = rb"\begin{document}"

END_DOCUMENT_MARKER = rb"\end{document}"

MAIN_FILE_BASENAME = "main.tex"

INPUT_MARKERS: tuple[bytes, ...] = (rb"\input{", rb"\include{")

BIBLIOGRAPHY_COMMAND_RE = re.compile(rb"\\bibliography\s*\{[^}]*\}")

COMMENT_CHAR = b"%"

BACKSLASH = b"\\"

CJK_PACKAGES = frozenset({b"xeCJK", b"ctex", b"ctexcap"})

CTEX_CLASSES = frozenset({b"ctexart", b"ctexrep", b"ctexbook", b"ctexbeamer"})

CJK_LEGACY_PACKAGES = frozenset({b"CJKutf8", b"CJK", b"CJKspace", b"CJKpunct"})

PACKAGE_RE = re.compile(rb"\\(?:usepackage|RequirePackage)\s*(\[[^\]]*\])?\s*\{([^}]*)\}")

DOCUMENTCLASS_RE = re.compile(rb"\\(?:documentclass|documentstyle)\s*(\[[^\]]*\])?\s*\{([^}]*)\}")

CJK_ENV_RE = re.compile(rb"\\begin\s*\{CJK\*?\}(?:\s*\{[^}]*\})*|\\end\s*\{CJK\*?\}|\\CJKfamily\s*\{[^}]*\}")

XECJK_BLOCK = rb"""% ---- injected by tongtu (precompile) ----
\usepackage{xeCJK}
\setCJKmainfont[
  Path = {fonts/},
  BoldFont = LXGWWenKai-Medium.ttf
]{LXGWWenKai-Light.ttf}
\IfFontExistsTF{Hiragino Sans GB}
  {\setCJKsansfont{Hiragino Sans GB}}
  {\IfFontExistsTF{Noto Sans CJK SC}
    {\setCJKsansfont{Noto Sans CJK SC}}
    {\setCJKsansfont[Path={fonts/},BoldFont=LXGWWenKai-Medium.ttf]{LXGWWenKai-Light.ttf}}}
\setCJKmonofont[Path={fonts/}]{LXGWWenKai-Light.ttf}
\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip = 0pt plus 1pt
\linespread{1.4}
% ---- end tongtu (precompile) ----
"""


@dataclass(frozen=True)
class CompileAttempt:
    outcome: processes.ProcessOutcome
    log_path: Path
    log_text: str | None
    pdf_bytes: int
    counts: texlog.LogCounts

    @property
    def passed(self) -> bool:
        return (
            not self.outcome.timed_out and self.outcome.returncode == 0 and self.pdf_bytes > 0 and self.counts.pages > 0
        )


def run(paper_workdir: Workdir, *, model_override: str | None = None, effort: str | None = None) -> PrecompileManifest:
    paper_workdir.create()
    _reset_outputs(paper_workdir)
    manifest = _execute(paper_workdir, model_override, effort)
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _execute(paper_workdir: Workdir, model_override: str | None, effort: str | None) -> PrecompileManifest:
    src = paper_workdir.src
    warnings: list[str] = []
    candidates = _scan_candidates(src, warnings)
    if not candidates:
        return PrecompileManifest(
            status=PrecompileStatus.MAIN_NOT_FOUND,
            warnings=warnings,
            message="src/ 的 .tex 文件里没有一个在注释外含 \\documentclass，判定不出主文件。",
        )
    main_file = _select_main_file(candidates)
    if main_file is None:
        return PrecompileManifest(
            status=PrecompileStatus.MAIN_AMBIGUOUS,
            warnings=warnings,
            message=f"主文件候选不唯一，判定规则收敛不到单个结果，候选：{'、'.join(name for name, _ in candidates)}",
        )

    expanded, failure = _expand(src, main_file, warnings)
    if expanded is None:
        return PrecompileManifest(
            status=PrecompileStatus.EXPAND_FAILED, main_file=main_file, warnings=warnings, message=failure
        )
    expanded = _inline_bbl(expanded, src, main_file, warnings)
    exit_check = _exit_check_message(expanded)
    if exit_check:
        return PrecompileManifest(
            status=PrecompileStatus.EXPAND_FAILED, main_file=main_file, warnings=warnings, message=exit_check
        )
    residual = _count_residual_input_lines(expanded)
    if residual:
        warnings.append(f"展开后仍有 {residual} 行在注释外含 \\input{{ 或 \\include{{，这些文件没有被展开")

    injected = _inject_cjk(expanded, warnings)
    tree = _precompile_dir(paper_workdir)
    _assemble_tree(paper_workdir, tree, injected, warnings)

    try:
        first = _attempt_compile(tree)
    except OSError as error:
        return _compile_failed(
            main_file,
            warnings,
            f"执行 latexmk 失败（{describe_error(error)}）。latexmk 随 TeX 发行版分发，确认已安装且在 PATH 里。",
        )
    if first.outcome.timed_out:
        return _compile_failed(main_file, warnings, _timeout_message(first))

    fix_session: FixSession | None = None
    final = first
    if not first.passed:
        fix_session = _fix(paper_workdir, tree, warnings, model_override, effort)
        warnings.extend(_clean_tree(tree))
        try:
            final = _attempt_compile(tree)
        except OSError as error:
            return _compile_failed(
                main_file, warnings, f"终审编译时执行 latexmk 失败（{describe_error(error)}）。", fix_session
            )
        if final.outcome.timed_out:
            return _compile_failed(main_file, warnings, _timeout_message(final), fix_session)
        if not final.passed:
            return _compile_failed(
                main_file, warnings, f"经过修复会话，终审编译未过出口判据：{_failure_message(final)}", fix_session
            )

    _precompile_path(paper_workdir).write_bytes((tree / FLAT_FILENAME).read_bytes())
    report = CompileReport(
        pages=final.counts.pages,
        pdf_bytes=final.pdf_bytes,
        overfull_hboxes=final.counts.overfull_hboxes,
        undefined_references=final.counts.undefined_references,
        undefined_citations=final.counts.undefined_citations,
        missing_characters=final.counts.missing_characters,
        duration_seconds=final.outcome.duration_seconds,
    )
    return PrecompileManifest(
        status=PrecompileStatus.OK, main_file=main_file, report=report, fix_session=fix_session, warnings=warnings
    )


def _compile_failed(
    main_file: str, warnings: list[str], message: str, fix_session: FixSession | None = None
) -> PrecompileManifest:
    return PrecompileManifest(
        status=PrecompileStatus.COMPILE_FAILED,
        main_file=main_file,
        fix_session=fix_session,
        warnings=warnings,
        message=message,
    )


def _scan_candidates(src: Path, warnings: list[str]) -> list[tuple[str, bool]]:
    candidates: list[tuple[str, bool]] = []
    tex_paths = sorted(
        (path for path in src.rglob("*.tex") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.relative_to(src).as_posix(),
    )
    for path in tex_paths:
        relative = path.relative_to(src).as_posix()
        try:
            content = path.read_bytes()
        except OSError as error:
            warnings.append(f"主文件判定时读不到 {relative}：{describe_error(error)}")
            continue
        has_document_class = False
        has_begin_document = False
        for line in content.splitlines():
            code = _code_before_comment(line)
            has_document_class = has_document_class or any(marker in code for marker in DOCUMENT_CLASS_MARKERS)
            has_begin_document = has_begin_document or BEGIN_DOCUMENT_MARKER in code
        if has_document_class:
            candidates.append((relative, has_begin_document))
    return candidates


def _select_main_file(candidates: list[tuple[str, bool]]) -> str | None:
    if len(candidates) == 1:
        return candidates[0][0]
    remaining = [name for name, _ in candidates]
    with_document = [name for name, has_begin in candidates if has_begin]
    if len(with_document) == 1:
        return with_document[0]
    if with_document:
        remaining = with_document
    named_main = [name for name in remaining if PurePosixPath(name).name == MAIN_FILE_BASENAME]
    if len(named_main) == 1:
        return named_main[0]
    return None


def _expand(src: Path, main_file: str, warnings: list[str]) -> tuple[bytes | None, str]:
    command = [*LATEXPAND_COMMAND, main_file]
    try:
        completed = subprocess.run(command, cwd=src, capture_output=True, check=False)
    except OSError as error:
        return None, (
            f"执行 latexpand 失败（{describe_error(error)}）。latexpand 随 TeX Live 分发，确认已安装且在 PATH 里。"
        )
    stderr_text = completed.stderr.decode("utf-8", errors="replace")
    warnings.extend(line for line in stderr_text.splitlines() if line.strip())
    if completed.returncode != 0:
        return None, (
            f"latexpand 退出码 {completed.returncode}；stderr：{stderr_text.strip()[: processes.OUTPUT_EXCERPT_CHARS]}"
        )
    return completed.stdout, ""


def _inline_bbl(output: bytes, src: Path, main_file: str, warnings: list[str]) -> bytes:
    bbl_relative = str(PurePosixPath(main_file).with_suffix(".bbl"))
    bbl_path = src / bbl_relative
    if not bbl_path.is_file():
        return output
    lines = output.splitlines(keepends=True)
    matches: list[tuple[int, re.Match[bytes]]] = []
    for index, line in enumerate(lines):
        code = _code_before_comment(line)
        matches.extend((index, match) for match in BIBLIOGRAPHY_COMMAND_RE.finditer(code))
    if len(matches) != 1:
        warnings.append(
            f"主文件同目录有 {bbl_relative}，但展开结果里注释外的 \\bibliography 命令有 {len(matches)} 处"
            "（内联要求恰一处），未内联"
        )
        return output
    index, match = matches[0]
    line = lines[index]
    lines[index] = line[: match.start()] + bbl_path.read_bytes() + line[match.end() :]
    warnings.append(f"已把 {bbl_relative} 内联进 \\bibliography 命令处")
    return b"".join(lines)


def _exit_check_message(output: bytes) -> str:
    if not output:
        return "latexpand 的输出为空。"
    missing = [marker.decode() for marker in (BEGIN_DOCUMENT_MARKER, END_DOCUMENT_MARKER) if marker not in output]
    if missing:
        return f"展开结果缺少 {'、'.join(missing)}，不是一份完整的文档。"
    return ""


def _count_residual_input_lines(output: bytes) -> int:
    return sum(
        1 for line in output.splitlines() if any(marker in _code_before_comment(line) for marker in INPUT_MARKERS)
    )


def _code_before_comment(line: bytes) -> bytes:
    index = line.find(COMMENT_CHAR)
    while index != -1:
        if index == 0 or line[index - 1 : index] != BACKSLASH:
            return line[:index]
        index = line.find(COMMENT_CHAR, index + 1)
    return line


def _inject_cjk(source: bytes, warnings: list[str]) -> bytes:
    lines = source.splitlines(keepends=True)
    document_index = _line_index_of(lines, BEGIN_DOCUMENT_MARKER)
    preamble_end = document_index if document_index is not None else len(lines)
    packages = _preamble_packages(lines[:preamble_end])
    documentclass, class_end_index = _find_documentclass(lines, preamble_end)
    if packages & CJK_PACKAGES or documentclass in CTEX_CLASSES:
        return source
    if packages & CJK_LEGACY_PACKAGES:
        lines = _strip_legacy_cjk(lines, preamble_end, warnings)
        document_index = _line_index_of(lines, BEGIN_DOCUMENT_MARKER)
        preamble_end = document_index if document_index is not None else len(lines)
        _, class_end_index = _find_documentclass(lines, preamble_end)
    if class_end_index is None:
        warnings.append("展开结果里找不到注释外的 \\documentclass，中文排版设置注入到文件开头")
        insert_at = 0
    else:
        insert_at = class_end_index + 1
    block = XECJK_BLOCK if XECJK_BLOCK.endswith(b"\n") else XECJK_BLOCK + b"\n"
    return b"".join(lines[:insert_at]) + block + b"".join(lines[insert_at:])


def _line_index_of(lines: list[bytes], marker: bytes) -> int | None:
    for index, line in enumerate(lines):
        if marker in _code_before_comment(line):
            return index
    return None


def _preamble_packages(lines: list[bytes]) -> set[bytes]:
    packages: set[bytes] = set()
    for line in lines:
        for match in PACKAGE_RE.finditer(_code_before_comment(line)):
            packages.update(name.strip() for name in match.group(2).split(b",") if name.strip())
    return packages


def _find_documentclass(lines: list[bytes], preamble_end: int) -> tuple[bytes | None, int | None]:
    for index, line in enumerate(lines[:preamble_end]):
        code = _code_before_comment(line)
        match = DOCUMENTCLASS_RE.search(code)
        if match is not None:
            return match.group(2).strip(), index
        if any(marker in code for marker in DOCUMENT_CLASS_MARKERS):
            return None, _brace_balance_end(lines, index, preamble_end)
    return None, None


def _brace_balance_end(lines: list[bytes], start: int, preamble_end: int) -> int:
    depth = 0
    seen_open = False
    for index in range(start, preamble_end):
        code = _code_before_comment(lines[index])
        for byte in code:
            if byte == ord("{"):
                depth += 1
                seen_open = True
            elif byte == ord("}"):
                depth -= 1
        if seen_open and depth <= 0:
            return index
    return start


def _strip_legacy_cjk(lines: list[bytes], preamble_end: int, warnings: list[str]) -> list[bytes]:
    removed: set[bytes] = set()

    def rewrite_packages(match: re.Match[bytes]) -> bytes:
        names = [name.strip() for name in match.group(2).split(b",") if name.strip()]
        kept = [name for name in names if name not in CJK_LEGACY_PACKAGES]
        removed.update(name for name in names if name in CJK_LEGACY_PACKAGES)
        if not kept:
            return b""
        options = match.group(1) or b""
        return b"\\usepackage" + options + b"{" + b",".join(kept) + b"}"

    stripped_envs = 0
    rewritten: list[bytes] = []
    for index, line in enumerate(lines):
        code = _code_before_comment(line)
        comment = line[len(code) :]
        if index < preamble_end:
            code = PACKAGE_RE.sub(rewrite_packages, code)
        code, count = CJK_ENV_RE.subn(b"", code)
        stripped_envs += count
        rewritten.append(code + comment)
    if removed:
        removed_names = "、".join(sorted(name.decode() for name in removed))
        warnings.append(f"已移除 pdflatex 的中文机制宏包 {removed_names}，改用注入的 xeCJK 配置")
    if stripped_envs:
        warnings.append(f"已剥除 {stripped_envs} 处 CJK 环境包裹与 \\CJKfamily 设置")
    return rewritten


def _assemble_tree(paper_workdir: Workdir, tree: Path, flat: bytes, warnings: list[str]) -> None:
    shutil.copytree(paper_workdir.src, tree, dirs_exist_ok=True)
    tree_flat_path = tree / FLAT_FILENAME
    if tree_flat_path.exists():
        warnings.append(f"src/ 里本来就有 {FLAT_FILENAME}，编译树里的这一份已被展开结果覆盖")
    tree_flat_path.write_bytes(flat)
    if FONTS_DIR.is_dir():
        shutil.copytree(FONTS_DIR, tree / FONTS_DIRNAME, dirs_exist_ok=True)
    else:
        warnings.append(f"仓库字体目录 {FONTS_DIR} 不存在，注入的 xeCJK 配置将找不到字体")


def _attempt_compile(tree: Path) -> CompileAttempt:
    outcome = processes.run_in_process_group(list(LATEXMK_COMMAND), tree, COMPILE_TIMEOUT_SECONDS)
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


def _clean_tree(tree: Path) -> list[str]:
    try:
        outcome = processes.run_in_process_group(list(LATEXMK_CLEAN_COMMAND), tree, CLEAN_TIMEOUT_SECONDS)
    except OSError as error:
        return [f"终审前清理编译产物失败（{describe_error(error)}）"]
    if outcome.timed_out:
        return [f"终审前清理编译产物超过 {CLEAN_TIMEOUT_SECONDS} 秒超时上限，已按进程组终止"]
    if outcome.returncode != 0:
        return [f"终审前清理编译产物的 latexmk 退出码 {outcome.returncode}"]
    return []


def _fix(
    paper_workdir: Workdir, tree: Path, warnings: list[str], model_override: str | None, effort: str | None
) -> FixSession:
    snapshot = _snapshot_tree_files(paper_workdir.src, tree)
    started = time.monotonic()
    outcome = model.work(
        ROLE, tree, trace_path=paper_workdir.logs / TRACE_FILENAME, model=model_override, effort=effort
    )
    session = FixSession(
        stop_reason=str(outcome.stop_reason),
        model=_session_model(model_override, effort),
        duration_seconds=time.monotonic() - started,
    )
    changed = _detect_changed_files(tree, snapshot)
    if changed:
        warnings.append(
            f"修复会话改动了 {FLAT_FILENAME} 之外的 {len(changed)} 个文件：{'、'.join(changed)}；"
            "这些改动不传播到下游，compile 阶段的编译树仍从 src/ 组装"
        )
    if outcome.stop_reason is StopReason.ERROR:
        warnings.append(f"修复会话以 error 结束（{outcome.detail}），结论仍由脚本终审给出")
    if outcome.stop_reason is StopReason.TIMEOUT:
        warnings.append("修复会话以 timeout 结束，结论仍由脚本终审给出")
    return session


def _session_model(model_override: str | None, effort: str | None) -> str:
    config, _ = load_config()
    if config is not None:
        resolved, _ = resolve_role(config, ROLE, RoleTable.RUNTIME, model_override, effort)
        if resolved is not None:
            return f"{resolved.runtime}/{resolved.model}"
    return model_override or ""


def _snapshot_tree_files(src: Path, tree: Path) -> dict[str, str]:
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
    changed: list[str] = []
    for relative, digest in snapshot.items():
        tree_path = tree / relative
        if not tree_path.is_file() or _file_sha256(tree_path) != digest:
            changed.append(relative)
    return changed


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _timeout_message(attempt: CompileAttempt) -> str:
    return f"latexmk 执行超过 {COMPILE_TIMEOUT_SECONDS} 秒超时上限，已按进程组终止；log：{attempt.log_path}"


def _failure_message(attempt: CompileAttempt) -> str:
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


def _precompile_dir(paper_workdir: Workdir) -> Path:
    return paper_workdir.build / PRECOMPILE_DIRNAME


def _precompile_path(paper_workdir: Workdir) -> Path:
    return paper_workdir.build / PRECOMPILE_FILENAME


def _reset_outputs(paper_workdir: Workdir) -> None:
    shutil.rmtree(_precompile_dir(paper_workdir), ignore_errors=True)
    _precompile_path(paper_workdir).unlink(missing_ok=True)
    (paper_workdir.logs / TRACE_FILENAME).unlink(missing_ok=True)
