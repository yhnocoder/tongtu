from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import model, processes, texlog
from ..artifacts.common import CompileReport, FixSession
from ..artifacts.precompile import PrecompileManifest, PrecompileStatus
from ..assets import asset_path
from ..manifests import describe_error, write_manifest
from ..model.config import FontsConfig, RoleTable, load_config, resolve_role
from ..model.work import StopReason
from ..workdir import Workdir

STAGE_NAME = "precompile"

ROLE = "precompile_fix"

SANDBOX_DIRNAME = "sandbox"

STAGE_DIRNAME = "precompile"

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

FONT_FILE_SUFFIXES = (".ttf", ".otf", ".ttc")

XECJK_HEAD = rb"""% ---- injected by tongtu (precompile) ----
\usepackage{xeCJK}
"""

XECJK_SANS_DETECT = rb"""\IfFontExistsTF{Hiragino Sans GB}
  {\setCJKsansfont{Hiragino Sans GB}}
  {\IfFontExistsTF{Noto Sans CJK SC}
    {\setCJKsansfont{Noto Sans CJK SC}}
    {\setCJKsansfont[Path={fonts/},BoldFont=LXGWWenKai-Medium.ttf]{LXGWWenKai-Light.ttf}}}
"""

XECJK_TAIL = rb"""\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip = 0pt plus 1pt
\linespread{1.4}
% ---- end tongtu (precompile) ----
"""


@dataclass(frozen=True)
class ResolvedFont:
    name: str
    is_file: bool


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


def run(
    paper_workdir: Workdir,
    *,
    model_override: str | None = None,
    effort: str | None = None,
    report: Callable[[str], None] | None = None,
) -> PrecompileManifest:
    paper_workdir.create()
    _reset_outputs(paper_workdir)
    manifest = _execute(paper_workdir, model_override, effort, report or (lambda action: None))
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _execute(
    paper_workdir: Workdir, model_override: str | None, effort: str | None, report: Callable[[str], None]
) -> PrecompileManifest:
    src = paper_workdir.src
    warnings: list[str] = []
    candidates = _scan_candidates(src, warnings)
    if not candidates:
        return PrecompileManifest(
            status=PrecompileStatus.MAIN_NOT_FOUND,
            warnings=warnings,
            message="no .tex file under src/ contains \\documentclass outside comments; cannot pick a main file.",
        )
    main_file = _select_main_file(candidates)
    if main_file is None:
        return PrecompileManifest(
            status=PrecompileStatus.MAIN_AMBIGUOUS,
            warnings=warnings,
            message=(
                "multiple main file candidates and the selection rules do not converge on one: "
                f"{', '.join(name for name, _ in candidates)}"
            ),
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
        warnings.append(
            f"{residual} lines still contain \\input{{ or \\include{{ outside comments "
            "after expansion; those files were not expanded"
        )

    injected, font_files = _inject_cjk(expanded, warnings, _fonts_config())
    tree = _precompile_dir(paper_workdir)
    _assemble_tree(paper_workdir, tree, injected, warnings, font_files)

    report(f"compiling {FLAT_FILENAME}")
    try:
        first = _attempt_compile(tree)
    except OSError as error:
        return _compile_failed(
            main_file,
            warnings,
            f"failed to run latexmk ({describe_error(error)}). latexmk ships with the TeX "
            "distribution; check that it is installed and in PATH.",
        )
    if first.outcome.timed_out:
        return _compile_failed(main_file, warnings, _timeout_message(first))

    fix_session: FixSession | None = None
    final = first
    if not first.passed:
        report("fix session running")
        fix_session = _fix(paper_workdir, tree, warnings, model_override, effort)
        warnings.extend(_clean_tree(tree))
        report("verifying compile")
        try:
            final = _attempt_compile(tree)
        except OSError as error:
            return _compile_failed(
                main_file,
                warnings,
                f"failed to run latexmk for the verify compile ({describe_error(error)}).",
                fix_session,
            )
        if final.outcome.timed_out:
            return _compile_failed(main_file, warnings, _timeout_message(final), fix_session)
        if not final.passed:
            return _compile_failed(
                main_file,
                warnings,
                f"after the fix session the verify compile still fails the exit checks: {_failure_message(final)}",
                fix_session,
            )

    _precompile_path(paper_workdir).write_bytes((tree / FLAT_FILENAME).read_bytes())
    compile_report = CompileReport(
        pages=final.counts.pages,
        pdf_bytes=final.pdf_bytes,
        overfull_hboxes=final.counts.overfull_hboxes,
        undefined_references=final.counts.undefined_references,
        undefined_citations=final.counts.undefined_citations,
        missing_characters=final.counts.missing_characters,
        duration_seconds=final.outcome.duration_seconds,
    )
    return PrecompileManifest(
        status=PrecompileStatus.OK,
        main_file=main_file,
        report=compile_report,
        fix_session=fix_session,
        warnings=warnings,
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
            warnings.append(f"cannot read {relative} while picking the main file: {describe_error(error)}")
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
            f"failed to run latexpand ({describe_error(error)}). latexpand ships with TeX Live; "
            "check that it is installed and in PATH."
        )
    stderr_text = completed.stderr.decode("utf-8", errors="replace")
    warnings.extend(line for line in stderr_text.splitlines() if line.strip())
    if completed.returncode != 0:
        return None, (
            f"latexpand exited with code {completed.returncode}; "
            f"stderr: {stderr_text.strip()[: processes.OUTPUT_EXCERPT_CHARS]}"
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
            f"{bbl_relative} sits next to the main file, but the expansion has {len(matches)} "
            "\\bibliography commands outside comments (inlining requires exactly one); not inlined"
        )
        return output
    index, match = matches[0]
    line = lines[index]
    lines[index] = line[: match.start()] + bbl_path.read_bytes() + line[match.end() :]
    warnings.append(f"inlined {bbl_relative} at the \\bibliography command")
    return b"".join(lines)


def _exit_check_message(output: bytes) -> str:
    if not output:
        return "latexpand produced empty output."
    missing = [marker.decode() for marker in (BEGIN_DOCUMENT_MARKER, END_DOCUMENT_MARKER) if marker not in output]
    if missing:
        return f"the expansion is missing {', '.join(missing)}; not a complete document."
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


def _inject_cjk(source: bytes, warnings: list[str], fonts: FontsConfig) -> tuple[bytes, list[Path]]:
    lines = source.splitlines(keepends=True)
    document_index = _line_index_of(lines, BEGIN_DOCUMENT_MARKER)
    preamble_end = document_index if document_index is not None else len(lines)
    packages = _preamble_packages(lines[:preamble_end])
    documentclass, class_end_index = _find_documentclass(lines, preamble_end)
    if packages & CJK_PACKAGES or documentclass in CTEX_CLASSES:
        return source, []
    if packages & CJK_LEGACY_PACKAGES:
        lines = _strip_legacy_cjk(lines, preamble_end, warnings)
        document_index = _line_index_of(lines, BEGIN_DOCUMENT_MARKER)
        preamble_end = document_index if document_index is not None else len(lines)
        _, class_end_index = _find_documentclass(lines, preamble_end)
    if class_end_index is None:
        warnings.append(
            "no \\documentclass outside comments in the expansion; "
            "the Chinese typesetting setup is injected at the top of the file"
        )
        insert_at = 0
    else:
        insert_at = class_end_index + 1
    font_files: list[Path] = []
    block = _xecjk_block(fonts, warnings, font_files)
    return b"".join(lines[:insert_at]) + block + b"".join(lines[insert_at:]), font_files


def _fonts_config() -> FontsConfig:
    config, _ = load_config()
    if config is None:
        return FontsConfig()
    return config.fonts


def _xecjk_block(fonts: FontsConfig, warnings: list[str], font_files: list[Path]) -> bytes:
    default = FontsConfig()
    main = _resolve_chain("main", fonts.main, warnings, font_files) or [ResolvedFont(default.main, True)]
    bold = _resolve_font("bold", fonts.bold, warnings, font_files) if fonts.bold else None
    bold_is_default = fonts.bold == default.bold
    if bold is not None and not bold_is_default and all(_pair_bold(bold, False, font) is None for font in main):
        warnings.append(
            "the bold font in models.toml matches no main candidate in kind "
            "(file pairs with file, font name with font name); bold is ignored"
        )
        bold = None
    sans = _resolve_chain("sans", fonts.sans, warnings, font_files) if fonts.sans else []
    mono = _resolve_chain("mono", fonts.mono, warnings, font_files) if fonts.mono else []
    parts = [XECJK_HEAD, _chain_lines(rb"\setCJKmainfont", main, bold, bold_is_default)]
    parts.append(_chain_lines(rb"\setCJKsansfont", sans, None, False) if sans else XECJK_SANS_DETECT)
    parts.append(_chain_lines(rb"\setCJKmonofont", mono or main, None, False))
    parts.append(XECJK_TAIL)
    return b"".join(parts)


def _resolve_chain(
    slot: str, value: str | list[str], warnings: list[str], font_files: list[Path]
) -> list[ResolvedFont]:
    candidates = value if isinstance(value, list) else [value]
    chain: list[ResolvedFont] = []
    for index, candidate in enumerate(candidates):
        resolved = _resolve_font(slot, candidate, warnings, font_files)
        if resolved is None:
            continue
        chain.append(resolved)
        if resolved.is_file:
            if index < len(candidates) - 1:
                warnings.append(
                    f"the {slot} candidate {candidate} in models.toml is a font file and always "
                    "available; candidates after it are never used"
                )
            break
    return chain


def _resolve_font(slot: str, value: str, warnings: list[str], font_files: list[Path]) -> ResolvedFont | None:
    if not value.lower().endswith(FONT_FILE_SUFFIXES):
        return ResolvedFont(value, False)
    path = Path(value).expanduser()
    if len(path.parts) > 1:
        if path.is_file():
            font_files.append(path)
            return ResolvedFont(path.name, True)
        warnings.append(f"the {slot} font file {value} in models.toml does not exist; skipped")
        return None
    if (FONTS_DIR / value).is_file():
        return ResolvedFont(value, True)
    warnings.append(f"the {slot} font file {value} in models.toml is not under {FONTS_DIR}; skipped")
    return None


def _pair_bold(bold: ResolvedFont | None, bold_is_default: bool, font: ResolvedFont) -> ResolvedFont | None:
    if bold is None or bold.is_file != font.is_file:
        return None
    if bold_is_default and font.name != FontsConfig().main:
        return None
    return bold


def _chain_lines(command: bytes, chain: list[ResolvedFont], bold: ResolvedFont | None, bold_is_default: bool) -> bytes:
    if len(chain) > 1 and not chain[-1].is_file:
        chain = [*chain, ResolvedFont(str(FontsConfig().main), True)]
    return _fallback_chain(command, chain, bold, bold_is_default, 0) + b"\n"


def _fallback_chain(
    command: bytes, chain: list[ResolvedFont], bold: ResolvedFont | None, bold_is_default: bool, depth: int
) -> bytes:
    font = chain[0]
    setter = _font_command(command, font, _pair_bold(bold, bold_is_default, font))
    if len(chain) == 1:
        return setter
    indent = b"  " * (depth + 1)
    rest = _fallback_chain(command, chain[1:], bold, bold_is_default, depth + 1)
    return (
        b"\\IfFontExistsTF{"
        + font.name.encode("utf-8")
        + b"}\n"
        + indent
        + b"{"
        + setter
        + b"}\n"
        + indent
        + b"{"
        + rest
        + b"}"
    )


def _font_command(command: bytes, font: ResolvedFont, bold: ResolvedFont | None) -> bytes:
    options: list[bytes] = []
    if font.is_file:
        options.append(b"Path={fonts/}")
    if bold is not None:
        options.append(b"BoldFont=" + bold.name.encode("utf-8"))
    if options:
        return command + b"[" + b",".join(options) + b"]{" + font.name.encode("utf-8") + b"}"
    return command + b"{" + font.name.encode("utf-8") + b"}"


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
        removed_names = ", ".join(sorted(name.decode() for name in removed))
        warnings.append(f"removed the pdflatex-era CJK packages {removed_names} in favor of the injected xeCJK setup")
    if stripped_envs:
        warnings.append(f"stripped {stripped_envs} CJK environment wrappers and \\CJKfamily settings")
    return rewritten


def _assemble_tree(
    paper_workdir: Workdir, tree: Path, flat: bytes, warnings: list[str], font_files: list[Path]
) -> None:
    shutil.copytree(paper_workdir.src, tree, dirs_exist_ok=True)
    tree_flat_path = tree / FLAT_FILENAME
    if tree_flat_path.exists():
        warnings.append(
            f"src/ already contains {FLAT_FILENAME}; the copy in the compile tree is overwritten by the expansion"
        )
    tree_flat_path.write_bytes(flat)
    if FONTS_DIR.is_dir():
        shutil.copytree(FONTS_DIR, tree / FONTS_DIRNAME, dirs_exist_ok=True)
    else:
        warnings.append(
            f"repository font directory {FONTS_DIR} does not exist; the injected xeCJK setup will not find the fonts"
        )
    if font_files:
        (tree / FONTS_DIRNAME).mkdir(exist_ok=True)
        for path in font_files:
            shutil.copy2(path, tree / FONTS_DIRNAME / path.name)


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
        return [f"failed to clean compile outputs before the verify compile ({describe_error(error)})"]
    if outcome.timed_out:
        return [
            f"cleaning compile outputs before the verify compile hit the {CLEAN_TIMEOUT_SECONDS}s timeout; "
            "the process group was terminated"
        ]
    if outcome.returncode != 0:
        return [f"latexmk exited with code {outcome.returncode} while cleaning before the verify compile"]
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
            f"the fix session modified {len(changed)} files besides {FLAT_FILENAME}: {', '.join(changed)}; "
            "these changes do not propagate downstream, the compile stage still assembles its tree from src/"
        )
    if outcome.stop_reason is StopReason.ERROR:
        warnings.append(
            f"the fix session ended with error ({outcome.detail}); the verdict still comes from the scripted checks"
        )
    if outcome.stop_reason is StopReason.TIMEOUT:
        warnings.append("the fix session ended with timeout; the verdict still comes from the scripted checks")
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
    return (
        f"latexmk hit the {COMPILE_TIMEOUT_SECONDS}s timeout and the process group was terminated; "
        f"log: {attempt.log_path}"
    )


def _failure_message(attempt: CompileAttempt) -> str:
    reasons: list[str] = []
    if attempt.outcome.returncode != 0:
        reasons.append(f"latexmk exited with code {attempt.outcome.returncode}")
    if attempt.pdf_bytes == 0:
        reasons.append(f"{PDF_FILENAME} is missing or empty")
    if attempt.counts.pages <= 0:
        reasons.append(f"no page count can be parsed from {LOG_FILENAME}")
    if attempt.log_text is None:
        stderr = attempt.outcome.stderr_text.strip()[: processes.OUTPUT_EXCERPT_CHARS]
        detail = f"cannot read {attempt.log_path}; latexmk stderr: {stderr}"
    else:
        error_lines = texlog.error_lines(attempt.log_text, ERROR_LINE_LIMIT)
        if error_lines:
            excerpt = " | ".join(error_lines)
            detail = f"error lines from the log (at most {ERROR_LINE_LIMIT}): {excerpt}; full log: {attempt.log_path}"
        else:
            detail = f"no lines starting with {texlog.ERROR_LINE_PREFIX} in the log; full log: {attempt.log_path}"
    return f"{'; '.join(reasons)}. {detail}"


def _precompile_dir(paper_workdir: Workdir) -> Path:
    return paper_workdir.build / SANDBOX_DIRNAME / STAGE_DIRNAME


def _precompile_path(paper_workdir: Workdir) -> Path:
    return paper_workdir.build / PRECOMPILE_FILENAME


def _reset_outputs(paper_workdir: Workdir) -> None:
    shutil.rmtree(_precompile_dir(paper_workdir), ignore_errors=True)
    _precompile_path(paper_workdir).unlink(missing_ok=True)
    (paper_workdir.logs / TRACE_FILENAME).unlink(missing_ok=True)
