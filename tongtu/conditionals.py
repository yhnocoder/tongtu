from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from .artifacts.mask import BlockCategory
from .masking import COMMENT_TAIL_RE, TableEntry, read_environment_name, skip_code_environment, skip_verb

PRIMITIVE_CONDITIONALS = frozenset(
    {
        "if",
        "ifcat",
        "ifnum",
        "ifdim",
        "ifodd",
        "ifvmode",
        "ifhmode",
        "ifmmode",
        "ifinner",
        "ifvoid",
        "ifhbox",
        "ifvbox",
        "ifx",
        "ifeof",
        "iftrue",
        "iffalse",
        "ifcase",
        "ifdefined",
        "ifcsname",
        "iffontchar",
        "ifincsname",
        "ifprimitive",
    }
)

KNOWN_NON_CONDITIONALS = frozenset({"iff", "ifthenelse"})

LET_COMMAND = "let"

NEWIF_COMMAND = "newif"

DEFINING_COMMANDS = frozenset({LET_COMMAND, "def", "edef", "gdef", "xdef", "futurelet"})

OPERAND_COMMANDS = DEFINING_COMMANDS | {"ifdefined", "ifx", "meaning", "string", "noexpand", "show", NEWIF_COMMAND}

BINARY_CONDITIONALS = frozenset({"ifx"})

CONDITIONAL_PREFIX = "if"

CONTROL_WORD_RE = re.compile(r"\\([A-Za-z@]+|.)", re.DOTALL)

SKIPPED_SPACE_RE = re.compile(r"[ \t]*(?:\n[ \t]*+(?!\n))?")

BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")

LETTER_NAME_RE = re.compile(r"[A-Za-z@]+")


class Kind(StrEnum):
    OPENER = "opener"
    ELSE = "else"
    FI = "fi"
    UNKNOWN = "unknown"
    PLAIN = "plain"


@dataclass(frozen=True)
class ControlWord:
    name: str
    start: int
    end: int
    group: int
    condition: int = 0
    role: str | None = None
    kind: Kind = Kind.PLAIN


@dataclass(frozen=True)
class Switch:
    assigned_at: int
    value: bool


@dataclass(frozen=True)
class Decision:
    word: ControlWord
    reason: str | None
    ranges: tuple[tuple[int, int], ...]


LITERAL_CONDITIONALS = {"iftrue": Switch(0, True), "iffalse": Switch(0, False)}


def strip_dead_branches(text: str, table: Mapping[str, TableEntry], warnings: list[str]) -> str:
    words, document_start = _scan(text, table)
    words = _assign_roles(text, words)
    declared = _declared_switches(words, document_start)
    words = _classify(words, declared)
    constants = _constants(words, declared, document_start)
    decisions: list[Decision] = []
    _collect_dead_ranges(text, words, 0, len(words), constants, decisions)
    warnings.extend(_report(text, decisions, constants))
    return _rebuild(text, words, [span for decision in decisions for span in decision.ranges])


def _scan(text: str, table: Mapping[str, TableEntry]) -> tuple[list[ControlWord], int]:
    words: list[ControlWord] = []
    group = 0
    document_start = len(text)
    position = 0
    while position < len(text):
        character = text[position]
        if character == "%":
            newline = text.find("\n", position)
            position = len(text) if newline < 0 else newline
        elif character == "{":
            group += 1
            position += 1
        elif character == "}":
            group -= 1
            position += 1
        elif character == "\\":
            name, after = _read_control_word(text, position)
            if name == "verb":
                position = skip_verb(text, after)
                continue
            if name in ("begin", "end"):
                environment, body_start = read_environment_name(text, after)
                if name == "begin":
                    if environment == "document" and document_start == len(text):
                        document_start = position
                    entry = (
                        None if environment is None else table.get(environment) or table.get(environment.rstrip("*"))
                    )
                    if entry is not None and entry.category is BlockCategory.CODE:
                        position = skip_code_environment(text, body_start, environment)
                        continue
                if environment not in (None, "document"):
                    group += 1 if name == "begin" else -1
            group += (name in ("begingroup", "bgroup")) - (name in ("endgroup", "egroup"))
            words.append(ControlWord(name, position, after, group))
            position = after
        else:
            position += 1
    return words, document_start


def _read_control_word(text: str, position: int) -> tuple[str, int]:
    match = CONTROL_WORD_RE.match(text, position)
    if match is None:
        return "", position + 1
    return match.group(1), match.end()


def _assign_roles(text: str, words: list[ControlWord]) -> list[ControlWord]:
    words = list(words)
    for index in range(1, len(words)):
        preceding = _preceding_words(text, words, index)
        previous = words[index - 1]
        role = None
        if preceding and preceding[0].name in OPERAND_COMMANDS and preceding[0].role is None:
            role = preceding[0].name
        elif (
            len(preceding) == 2
            and preceding[1].name in BINARY_CONDITIONALS | {LET_COMMAND}
            and preceding[1].role is None
        ):
            role = preceding[1].name
        elif previous.name in BINARY_CONDITIONALS and previous.role is None and len(_gap(text, words, index)) == 1:
            role = previous.name
        if role is not None:
            words[index] = replace(words[index], role=role)
    return words


def _preceding_words(text: str, words: list[ControlWord], index: int) -> list[ControlWord]:
    preceding: list[ControlWord] = []
    while index > 0 and len(preceding) < 2 and not _gap(text, words, index):
        index -= 1
        preceding.append(words[index])
    return preceding


def _gap(text: str, words: list[ControlWord], index: int) -> str:
    gap = COMMENT_TAIL_RE.sub("", text[words[index - 1].end : words[index].start])
    if LETTER_NAME_RE.fullmatch(words[index - 1].name):
        gap = gap.lstrip()
    return "" if gap.strip() == "=" else gap


def _declared_switches(words: list[ControlWord], document_start: int) -> set[str]:
    declarations: Counter[str] = Counter()
    declared: set[str] = set()
    for word in words:
        if (
            word.role == NEWIF_COMMAND
            and word.name.startswith(CONDITIONAL_PREFIX)
            and len(word.name) > len(CONDITIONAL_PREFIX)
        ):
            name = word.name[len(CONDITIONAL_PREFIX) :]
            declarations[name] += 1
            if word.start < document_start:
                declared.add(name)
    return declared - {name for name, count in declarations.items() if count > 1}


def _classify(words: list[ControlWord], declared: set[str]) -> list[ControlWord]:
    openers = PRIMITIVE_CONDITIONALS | {CONDITIONAL_PREFIX + name for name in declared}
    classified: list[ControlWord] = []
    condition = 0
    for word in words:
        kind = _kind(word.name, openers)
        classified.append(replace(word, condition=condition, kind=kind))
        if word.role is None:
            condition += (kind in (Kind.OPENER, Kind.UNKNOWN)) - (kind is Kind.FI)
    return classified


def _kind(name: str, openers: frozenset[str]) -> Kind:
    if name == "fi":
        return Kind.FI
    if name == "else":
        return Kind.ELSE
    if name in openers:
        return Kind.OPENER
    if name.startswith(CONDITIONAL_PREFIX) and name not in KNOWN_NON_CONDITIONALS:
        return Kind.UNKNOWN
    return Kind.PLAIN


def _constants(words: list[ControlWord], declared: set[str], document_start: int) -> dict[str, Switch]:
    redefined = {
        word.name
        for previous, word in zip(words, words[1:], strict=False)
        if word.role in DEFINING_COMMANDS and previous.name == word.role
    }
    constants = {name: switch for name, switch in LITERAL_CONDITIONALS.items() if name not in redefined}
    for name in declared:
        switch = _constant_assignment(words, name, document_start)
        if switch is not None and CONDITIONAL_PREFIX + name not in redefined:
            constants[CONDITIONAL_PREFIX + name] = switch
    return constants


def _constant_assignment(words: list[ControlWord], name: str, document_start: int) -> Switch | None:
    assignments = [word for word in words if word.name in (name + "true", name + "false")]
    if len(assignments) > 1 or any(
        word.role is not None or word.start >= document_start or word.group != 0 or word.condition != 0
        for word in assignments
    ):
        return None
    if not assignments:
        return Switch(0, False)
    return Switch(assignments[0].start, assignments[0].name == name + "true")


def _collect_dead_ranges(
    text: str,
    words: list[ControlWord],
    low: int,
    high: int,
    constants: dict[str, Switch],
    decisions: list[Decision],
) -> None:
    index = low
    while index < high:
        word = words[index]
        if word.name not in constants or word.role == NEWIF_COMMAND:
            index += 1
            continue
        preceding = _preceding_words(text, words, index)
        switch = constants[word.name]
        value = switch.value and word.start >= switch.assigned_at
        start = word.start
        if preceding and preceding[0].name == "unless" and preceding[0].role is None:
            value = not value
            start = preceding[0].start
        outcome = _keep_reason(word, preceding, switch) or _find_boundaries(text, words, index, high, value)
        if isinstance(outcome, str):
            decisions.append(Decision(word, outcome, ()))
            index += 1
            continue
        else_index, fi_index = outcome
        fi_end = _skip_space(text, words[fi_index].end)
        if value:
            live_end = fi_index if else_index is None else else_index
            ranges = ((start, _skip_space(text, word.end)), (words[live_end].start, fi_end))
            decisions.append(Decision(word, None, ranges))
            _collect_dead_ranges(text, words, index + 1, live_end, constants, decisions)
        elif else_index is not None:
            ranges = ((start, _skip_space(text, words[else_index].end)), (words[fi_index].start, fi_end))
            decisions.append(Decision(word, None, ranges))
            _collect_dead_ranges(text, words, else_index + 1, fi_index, constants, decisions)
        else:
            decisions.append(Decision(word, None, ((start, fi_end),)))
        index = fi_index + 1


def _keep_reason(word: ControlWord, preceding: list[ControlWord], switch: Switch) -> str | None:
    if word.role is not None:
        return f"operand of \\{word.role}"
    if preceding and preceding[0].name == "expandafter":
        return "follows \\expandafter"
    if word.start < switch.assigned_at and word.group > 0:
        return "precedes assignment inside a group"
    return None


def _find_boundaries(
    text: str, words: list[ControlWord], index: int, high: int, value: bool
) -> tuple[int | None, int] | str:
    depth = 0
    else_index: int | None = None
    live = value
    for position in range(index + 1, high):
        word = words[position]
        if word.role is not None and (live or word.role == NEWIF_COMMAND):
            continue
        if word.kind is Kind.OPENER:
            depth += 1
        elif word.kind is Kind.FI:
            if depth == 0:
                return else_index, position
            depth -= 1
        elif word.kind is Kind.ELSE:
            if depth == 0 and else_index is None:
                else_index = position
                live = not value
        elif word.kind is Kind.UNKNOWN:
            return f"\\{word.name} at line {_line_number(text, word.start)} is not a known conditional"
    return "no matching \\fi"


def _report(text: str, decisions: list[Decision], constants: dict[str, Switch]) -> list[str]:
    lines = [
        f"\\{decision.word.name} at line {_line_number(text, decision.word.start)} is kept: {decision.reason}"
        for decision in decisions
        if decision.reason is not None
    ]
    removed = Counter(decision.word.name for decision in decisions if decision.reason is None)
    kept = Counter(decision.word.name for decision in decisions if decision.reason is not None)
    for name in sorted(removed | kept):
        lines.append(
            f"constant switch \\{name} is {str(constants[name].value).lower()}: "
            f"removed {removed[name]} dead branches, kept {kept[name]}"
        )
    return lines


def _rebuild(text: str, words: list[ControlWord], ranges: list[tuple[int, int]]) -> str:
    word_ends = {word.end for word in words if LETTER_NAME_RE.fullmatch(word.name)}
    pieces: list[str] = []
    cursor = 0
    piece_end = 0
    for start, end in [*sorted(ranges), (len(text), len(text))]:
        if start > cursor:
            if piece_end in word_ends and _letter(text[cursor]):
                pieces.append(" ")
            elif cursor > piece_end and BLANK_LINE_RE.match(text, cursor):
                pieces.append("%")
            pieces.append(text[cursor:start])
            piece_end = start
        cursor = max(cursor, end)
    return "".join(pieces)


def _letter(character: str) -> bool:
    return character == "@" or (character.isascii() and character.isalpha())


def _skip_space(text: str, position: int) -> int:
    return SKIPPED_SPACE_RE.match(text, position).end()


def _line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1
