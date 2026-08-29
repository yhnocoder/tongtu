from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import compiling, pipeline, processes
from ..artifacts.common import FixSession
from ..artifacts.precompile import PrecompileManifest, PrecompileStatus
from ..assets import asset_path
from ..manifests import describe_error, write_manifest
from ..model.config import FontsConfig, load_config
from ..workdir import Workdir

STAGE_NAME = "precompile"

ROLE = "precompile_fix"

FLAT_FILENAME = "flat.tex"

TREE_NAME = "tex"

FONTS_DIRNAME = "fonts"

FONTS_DIR = asset_path(FONTS_DIRNAME)

LATEXPAND_COMMAND: tuple[str, ...] = ("latexpand", "--keep-comments", "--fatal")

LATEXPAND_TIMEOUT_SECONDS = 10.0

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

DEFAULT_FONTS = FontsConfig()

DEFAULT_SANS_CHAIN = ["Hiragino Sans GB", "Noto Sans CJK SC"]

FC_LIST_TIMEOUT_SECONDS = 5

XECJK_TAIL = rb"""\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip = 0pt plus 1pt
\linespread{1.4}
% ---- end tongtu (precompile) ----
"""


@dataclass(frozen=True)
class ResolvedFont:
    name: str
    is_file: bool


def run(
    paper_workdir: Workdir,
    *,
    model_override: str | None = None,
    effort: str | None = None,
    report: Callable[[str, str], None] | None = None,
) -> PrecompileManifest:
    paper_workdir.create()
    pipeline.clean(paper_workdir, STAGE_NAME)
    manifest = _execute(paper_workdir, model_override, effort, report or (lambda status, summary: None))
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _execute(
    paper_workdir: Workdir, model_override: str | None, effort: str | None, report: Callable[[str, str], None]
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
    tree = paper_workdir.sandbox(TREE_NAME)
    _assemble_tree(paper_workdir, tree, injected, warnings, font_files)

    final, fix_session, failure = compiling.compile_with_fix(
        ROLE,
        tree,
        FLAT_FILENAME,
        paper_workdir.precompile_fix_log,
        warnings,
        model_override,
        effort,
        report,
    )
    if final is None or failure:
        return _compile_failed(main_file, warnings, failure, fix_session)

    paper_workdir.precompile_tex.write_bytes((tree / FLAT_FILENAME).read_bytes())
    warnings.extend(compiling.clean_tree(tree, FLAT_FILENAME))
    return PrecompileManifest(
        status=PrecompileStatus.OK,
        main_file=main_file,
        report=final.report,
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
        outcome = processes.run_in_process_group(command, src, LATEXPAND_TIMEOUT_SECONDS)
    except OSError as error:
        return None, (
            f"failed to run latexpand ({describe_error(error)}). latexpand ships with TeX Live; "
            "check that it is installed and in PATH."
        )
    stderr_text = outcome.stderr_text
    warnings.extend(line for line in stderr_text.splitlines() if line.strip())
    if outcome.timed_out:
        return None, (
            f"latexpand did not finish within {LATEXPAND_TIMEOUT_SECONDS}s and its process group was terminated; "
            "the common cause is a self-reference in the source, "
            "for example main.tex containing \\input{main}."
        )
    if outcome.returncode != 0:
        return None, (
            f"latexpand exited with code {outcome.returncode}; "
            f"stderr: {stderr_text.strip()[: processes.OUTPUT_EXCERPT_CHARS]}"
        )
    return outcome.stdout, ""


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
    main = _resolve_chain("main", fonts.main, warnings, font_files) or [ResolvedFont(str(DEFAULT_FONTS.main), True)]
    bold = _resolve_font("bold", fonts.bold, warnings, font_files) if fonts.bold else None
    bold_is_default = fonts.bold == DEFAULT_FONTS.bold
    if bold is not None and not bold_is_default and all(_pair_bold(bold, False, font) is None for font in main):
        warnings.append(
            "the bold font in models.toml matches no main candidate in kind "
            "(file pairs with file, font name with font name); bold is ignored"
        )
        bold = None
    default_file = ResolvedFont(str(DEFAULT_FONTS.main), True)
    sans = _resolve_chain("sans", fonts.sans or DEFAULT_SANS_CHAIN, warnings, font_files) or [default_file]
    mono = _resolve_chain("mono", fonts.mono, warnings, font_files) if fonts.mono else []
    parts = [XECJK_HEAD, _chain_lines(rb"\setCJKmainfont", main, bold, bold_is_default)]
    parts.append(_chain_lines(rb"\setCJKsansfont", sans, ResolvedFont(str(DEFAULT_FONTS.bold), True), True))
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
        installed = None if resolved.is_file else _font_installed(resolved.name)
        if installed is False:
            continue
        chain.append(resolved)
        if resolved.is_file:
            if index < len(candidates) - 1:
                warnings.append(
                    f"the {slot} candidate {candidate} in models.toml is a font file and always "
                    "available; candidates after it are never used"
                )
            break
        if installed:
            break
    return chain


def _font_installed(name: str) -> bool | None:
    try:
        outcome = subprocess.run(
            ["fc-list", f":family={name}", "family"],
            capture_output=True,
            timeout=FC_LIST_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bool(outcome.stdout.strip())


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
    if bold_is_default and font.name != DEFAULT_FONTS.main:
        return None
    return bold


def _chain_lines(command: bytes, chain: list[ResolvedFont], bold: ResolvedFont | None, bold_is_default: bool) -> bytes:
    if len(chain) > 1 and not chain[-1].is_file:
        chain = [*chain, ResolvedFont(str(DEFAULT_FONTS.main), True)]
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
    warnings.extend(compiling.copy_src_tree(paper_workdir.src, tree, FLAT_FILENAME))
    (tree / FLAT_FILENAME).write_bytes(flat)
    if FONTS_DIR.is_dir():
        repo_fonts = sorted(path for path in FONTS_DIR.iterdir() if path.is_file())
    else:
        repo_fonts = []
        warnings.append(
            f"repository font directory {FONTS_DIR} does not exist; the injected xeCJK setup will not find the fonts"
        )
    fonts_dir = tree / FONTS_DIRNAME
    fonts_dir.mkdir(exist_ok=True)
    for path in (*repo_fonts, *font_files):
        link = fonts_dir / path.name
        link.unlink(missing_ok=True)
        os.symlink(path.absolute(), link)
