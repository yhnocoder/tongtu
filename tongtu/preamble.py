from __future__ import annotations

import re
from dataclasses import dataclass

from .masking import COMMENT_TAIL_RE, MaskError, match_group

CJK_LEGACY_PACKAGES = frozenset({"CJKutf8", "CJK", "CJKspace", "CJKpunct"})

DROPPED_PACKAGES = CJK_LEGACY_PACKAGES | {"ucs", "bxcjkjatype"}

DROPPED_DRIVER_OPTIONS = frozenset({"pdftex", "dvips", "dvipdfm", "dvipdfmx"})

ADDED_OPTIONS = {"microtype": "protrusion=false"}

FONTAWESOME_PRELUDE = (
    "\\newfontfamily\\FA{FontAwesome.otf}\n"
    "\\let\\tongtuorig\\newfontfamily\n"
    "\\def\\newfontfamily#1#2{\\global\\let\\newfontfamily\\tongtuorig}\n"
)

PACKAGE_PRELUDES = {"fontawesome": FONTAWESOME_PRELUDE}

DROPPED_ASSIGNMENTS = frozenset({"pdfoutput", "pdfcompresslevel", "pdfminorversion", "pdfobjcompresslevel"})

DROPPED_COMMANDS = frozenset({"pdfinfo"})

PACKAGE_COMMANDS = frozenset({"usepackage", "RequirePackage"})

BEGIN_DOCUMENT = "\\begin{document}"

TOKEN_RE = re.compile(r"%|\\([A-Za-z@]+|.)", re.DOTALL)

OPTIONS_RE = re.compile(r"\s*\[([^\]]*)\]", re.DOTALL)

GROUP_START_RE = re.compile(r"\s*\{")

ASSIGNMENT_RE = re.compile(r"[ \t]*=?[ \t]*\d+(?:[ \t]*\\relax)?")

CJK_ENV_RE = re.compile(r"\\begin\s*\{CJK\*?\}(?:\s*\{[^}]*\})*|\\end\s*\{CJK\*?\}|\\CJKfamily\s*\{[^}]*\}")


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    replacement: str
    notes: tuple[str, ...]
    dropped: tuple[str, ...] = ()


def adapt(text: str, warnings: list[str]) -> str:
    try:
        edits = _preamble_edits(text, _preamble_end(text))
    except MaskError as error:
        warnings.append(f"{error}; pdflatex leftovers are not rewritten")
        return text
    for edit in edits:
        line = _line_number(text, edit.start)
        warnings.extend(f"{note} at line {line}" for note in edit.notes)
    for edit in reversed(edits):
        text = text[: edit.start] + edit.replacement + text[edit.end :]
    if any(name in CJK_LEGACY_PACKAGES for edit in edits for name in edit.dropped):
        text, stripped = CJK_ENV_RE.subn("", text)
        if stripped:
            warnings.append(f"stripped {stripped} CJK environment wrappers and \\CJKfamily settings")
    return text


def _preamble_end(text: str) -> int:
    position = 0
    while (match := TOKEN_RE.search(text, position)) is not None:
        if match.group() == "%":
            position = _line_end(text, match.end())
        elif text.startswith(BEGIN_DOCUMENT, match.start()):
            return match.start()
        else:
            position = match.end()
    return len(text)


def _preamble_edits(text: str, end: int) -> list[Edit]:
    edits: list[Edit] = []
    position = 0
    while (match := TOKEN_RE.search(text, position, end)) is not None:
        position = match.end()
        if match.group() == "%":
            position = _line_end(text, position)
            continue
        name = match.group(1)
        if name in PACKAGE_COMMANDS:
            edit = _package_edit(text, match.start(), match.end(), name)
        elif name in DROPPED_ASSIGNMENTS:
            edit = _assignment_edit(text, match.start(), match.end())
        elif name in DROPPED_COMMANDS:
            edit = _command_edit(text, match.start(), match.end(), name)
        else:
            continue
        if edit is not None:
            edits.append(edit)
            position = edit.end
    return edits


def _package_edit(text: str, start: int, cursor: int, command: str) -> Edit | None:
    options_match = OPTIONS_RE.match(text, cursor)
    options_text = options_match.group(1) if options_match else ""
    cursor = options_match.end() if options_match else cursor
    group_match = GROUP_START_RE.match(text, cursor)
    if group_match is None:
        return None
    end = match_group(text, group_match.end() - 1)
    options = _split(options_text)
    names = _split(text[group_match.end() : end - 1])
    kept_options = [option for option in options if option not in DROPPED_DRIVER_OPTIONS]
    kept = [name for name in names if name not in DROPPED_PACKAGES]
    notes: list[str] = []
    dropped = [name for name in names if name in DROPPED_PACKAGES]
    if dropped:
        notes.append(f"removed the pdflatex-era package(s) {', '.join(dropped)} from \\{command}")
    for option in options:
        if option in DROPPED_DRIVER_OPTIONS:
            notes.append(f"removed the driver option {option} from \\{command}{{{','.join(names)}}}")
    if not kept and not dropped:
        return None
    statements: list[str] = []
    plain = [name for name in kept if name not in ADDED_OPTIONS]
    if plain:
        statements.append(_statement(command, kept_options, plain))
    for name in kept:
        if name in PACKAGE_PRELUDES:
            statements.insert(0, PACKAGE_PRELUDES[name].rstrip("\n"))
            notes.append(f"inserted the {name} prelude before \\{command}{{{name}}}")
        if name not in ADDED_OPTIONS:
            continue
        added = ADDED_OPTIONS[name]
        key = added.partition("=")[0]
        if any(option.partition("=")[0] == key for option in kept_options):
            statements.append(_statement(command, kept_options, [name]))
        else:
            statements.append(_statement(command, [*kept_options, added], [name]))
            notes.append(f"added {added} to \\{command}{{{name}}}")
    if not notes:
        return None
    return Edit(start, end, "\n".join(statements), tuple(notes), tuple(dropped))


def _statement(command: str, options: list[str], names: list[str]) -> str:
    bracket = f"[{','.join(options)}]" if options else ""
    return f"\\{command}{bracket}{{{','.join(names)}}}"


def _split(text: str) -> list[str]:
    return [part.strip() for part in COMMENT_TAIL_RE.sub("", text).split(",") if part.strip()]


def _assignment_edit(text: str, start: int, cursor: int) -> Edit | None:
    match = ASSIGNMENT_RE.match(text, cursor)
    if match is None:
        return None
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].rstrip()[-1:].isalpha():
        return None
    return Edit(start, match.end(), "", (f"removed the pdftex assignment {text[start : match.end()].strip()}",))


def _command_edit(text: str, start: int, cursor: int, name: str) -> Edit | None:
    group_match = GROUP_START_RE.match(text, cursor)
    if group_match is None:
        return None
    end = match_group(text, group_match.end() - 1)
    return Edit(start, end, "", (f"removed the pdftex command \\{name}{{...}}",))


def _line_end(text: str, position: int) -> int:
    newline = text.find("\n", position)
    return len(text) if newline < 0 else newline


def _line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1
