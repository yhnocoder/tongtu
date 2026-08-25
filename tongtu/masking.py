from __future__ import annotations

import json
import re
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SENTINEL_OPEN = "⟦"
SENTINEL_CLOSE = "⟧"

BLOCK_ID_PREFIX = "BLK"
CAPTION_ID_PREFIX = "CAP"

TOKEN_RE = re.compile(rf"{SENTINEL_OPEN}({BLOCK_ID_PREFIX}|{CAPTION_ID_PREFIX})-([0-9]+){SENTINEL_CLOSE}")

ENVIRONMENTS_TABLE_PATH = Path(__file__).resolve().parent / "data" / "environments.json"

DOCUMENT_ENVIRONMENT = "document"

ABSTRACT_ENVIRONMENT = "abstract"

METADATA_COMMANDS: frozenset[str] = frozenset({"title", "author", "date", "affiliation", "email"})

CAPTION_COMMAND = "caption"
CAPTION_OF_COMMAND = "captionof"

DECLARATION_COMMANDS: dict[str, str] = {
    "newtheorem": "newtheorem",
    "newenvironment": "newenvironment",
    "renewenvironment": "newenvironment",
}

ENVIRONMENT_NAME_RE = re.compile(r"[A-Za-z0-9@]+\*?")

LABEL_RE = re.compile(r"\\label\s*\{([^{}]*)\}")

TOP_LEVEL_SPECIAL_RE = re.compile(r"[\\%$]")

BODY_SPECIAL_RE = re.compile(r"[\\%]")

BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")

WHITESPACE_RUN_RE = re.compile(r"\s+")

COMMENT_TAIL_RE = re.compile(r"(?<!\\)%[^\n]*\n?")

PARAGRAPH_JOINER = " \\par "

DIFFERENCE_CONTEXT_CHARS = 60


class MaskError(Exception):
    pass


class EnvironmentClass(StrEnum):
    TEXT = "text"
    NON_TRANSLATABLE = "non_translatable"


class BlockCategory(StrEnum):
    MATH = "math"
    TABLE = "table"
    FIGURE = "figure"
    TIKZ = "tikz"
    CODE = "code"
    ALGORITHM = "algorithm"
    BIBLIOGRAPHY = "bibliography"
    BOX = "box"
    UNKNOWN = "unknown"
    PREAMBLE = "preamble"
    POSTAMBLE = "postamble"
    COMMENT = "comment"
    METADATA = "metadata"


class DecidedBy(StrEnum):
    NEWTHEOREM = "newtheorem"
    NEWENVIRONMENT = "newenvironment"
    TABLE = "table"
    DEFAULT = "default"


class CaptionKind(StrEnum):
    CAPTION = "caption"
    ABSTRACT = "abstract"


@dataclass(frozen=True)
class TableEntry:
    classification: EnvironmentClass
    category: BlockCategory | None


@dataclass
class EnvironmentDecision:
    classification: EnvironmentClass
    category: BlockCategory | None
    decided_by: DecidedBy
    occurrences: int = 0
    blocks: int = 0


@dataclass(frozen=True)
class Block:
    id: str
    category: BlockCategory
    environment: str
    decided_by: DecidedBy | None
    labels: tuple[str, ...]
    tex: str
    start: int
    end: int
    line: int


@dataclass(frozen=True)
class Caption:
    id: str
    block_id: str
    kind: CaptionKind
    tex: str
    masked_text: str


@dataclass(frozen=True)
class MaskOutcome:
    masked: str
    blocks: tuple[Block, ...]
    captions: tuple[Caption, ...]
    environments: dict[str, EnvironmentDecision]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class UnmaskOutcome:
    text: str
    fallbacks: tuple[str, ...]
    translated: dict[str, str]


def block_token(block_id: str) -> str:
    return f"{SENTINEL_OPEN}{block_id}{SENTINEL_CLOSE}"


def parse_environment_table(content: str) -> dict[str, TableEntry]:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as error:
        raise MaskError(f"environment table is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise MaskError("environment table top level must map environment names to objects")
    table: dict[str, TableEntry] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise MaskError(f"environment table entry {name} must be an object")
        try:
            classification = EnvironmentClass(entry.get("class"))
        except ValueError as error:
            raise MaskError(
                f"environment table entry {name} has a class outside the vocabulary: {entry.get('class')!r}"
            ) from error
        category_value = entry.get("category")
        if classification is EnvironmentClass.NON_TRANSLATABLE:
            try:
                category = BlockCategory(category_value)
            except ValueError as error:
                raise MaskError(
                    f"environment table entry {name} has a category outside the vocabulary: {category_value!r}"
                ) from error
        else:
            if category_value is not None:
                raise MaskError(f"environment table entry {name} is text and must not carry a category")
            category = None
        table[name] = TableEntry(classification=classification, category=category)
    return table


def mask_document(text: str, table: Mapping[str, TableEntry]) -> MaskOutcome:
    _check_sentinels(text)
    occurrences, declared = _enumerate_environments(text, table)
    environments = {name: _decide(name, declared, table, count) for name, count in sorted(occurrences.items())}
    return _MaskRun(text, environments, declared, table).run()


def unmask(masked: str, blocks: Sequence[Block], captions: Sequence[Caption]) -> UnmaskOutcome:
    filled, fallbacks, translated, stream = _restore_captions(masked, captions)
    text = _restore_blocks(stream, blocks, filled)
    residual = [ch for ch in (SENTINEL_OPEN, SENTINEL_CLOSE) if ch in text]
    if residual:
        raise MaskError(
            f"unmask output still contains sentinel characters {', '.join(residual)}; the masked text has broken placeholders"
        )
    return UnmaskOutcome(text=text, fallbacks=tuple(fallbacks), translated=translated)


def verify_roundtrip(source: str, outcome: MaskOutcome) -> None:
    restored = unmask(outcome.masked, outcome.blocks, outcome.captions).text
    if restored == source:
        return
    raise MaskError(_describe_difference(source, restored))


def _enumerate_environments(text: str, table: Mapping[str, TableEntry]) -> tuple[dict[str, int], dict[str, str]]:
    occurrences: dict[str, int] = {}
    declared: dict[str, str] = {}
    position = 0
    length = len(text)
    while position < length:
        match = TOP_LEVEL_SPECIAL_RE.search(text, position)
        if match is None:
            break
        position = match.start()
        if text[position] == "%":
            position = _skip_comment(text, position)
            continue
        if text[position] == "$":
            position += 1
            continue
        name, after_name = read_control_sequence(text, position)
        if name == "verb":
            position = skip_verb(text, after_name)
            continue
        if name == "begin":
            environment, after = read_environment_name(text, after_name)
            if environment is None:
                position = after_name
                continue
            if environment != DOCUMENT_ENVIRONMENT:
                occurrences[environment] = occurrences.get(environment, 0) + 1
            entry = _table_lookup(table, environment)
            if entry is not None and entry.category is BlockCategory.CODE:
                position = _skip_code_environment(text, after, environment)
            else:
                position = after
            continue
        if name in DECLARATION_COMMANDS:
            declared_name, after = _read_declaration_name(text, after_name)
            if declared_name is not None:
                declared.setdefault(declared_name, DECLARATION_COMMANDS[name])
            position = after
            continue
        position = after_name
    return occurrences, declared


def _decide(
    name: str, declared: Mapping[str, str], table: Mapping[str, TableEntry], occurrences: int
) -> EnvironmentDecision:
    for candidate in _lookup_names(name):
        source = declared.get(candidate)
        if source is not None:
            return EnvironmentDecision(
                classification=EnvironmentClass.TEXT,
                category=None,
                decided_by=DecidedBy(source),
                occurrences=occurrences,
            )
        entry = table.get(candidate)
        if entry is not None:
            return EnvironmentDecision(
                classification=entry.classification,
                category=entry.category,
                decided_by=DecidedBy.TABLE,
                occurrences=occurrences,
            )
    return EnvironmentDecision(
        classification=EnvironmentClass.NON_TRANSLATABLE,
        category=BlockCategory.UNKNOWN,
        decided_by=DecidedBy.DEFAULT,
        occurrences=occurrences,
    )


def _lookup_names(name: str) -> Iterator[str]:
    yield name
    if name.endswith("*"):
        yield name[:-1]


def _table_lookup(table: Mapping[str, TableEntry], name: str) -> TableEntry | None:
    for candidate in _lookup_names(name):
        entry = table.get(candidate)
        if entry is not None:
            return entry
    return None


@dataclass
class _CaptionSlot:
    start: int
    end: int
    kind: CaptionKind


class _MaskRun:
    def __init__(
        self,
        text: str,
        environments: dict[str, EnvironmentDecision],
        declared: Mapping[str, str],
        table: Mapping[str, TableEntry],
    ) -> None:
        self.text = text
        self.length = len(text)
        self.environments = environments
        self.declared = declared
        self.table = table
        self.blocks: list[Block] = []
        self.captions: list[Caption] = []
        self.warnings: list[str] = []
        self.output: list[str] = []
        self.cursor = 0
        self.line_starts = _line_starts(text)

    def run(self) -> MaskOutcome:
        position = self._mask_preamble()
        self._mask_body(position)
        self.output.append(self.text[self.cursor :])
        return MaskOutcome(
            masked="".join(self.output),
            blocks=tuple(self.blocks),
            captions=tuple(self.captions),
            environments=self.environments,
            warnings=tuple(self.warnings),
        )

    def _mask_preamble(self) -> int:
        end = self._find_begin_document()
        if end is None:
            raise MaskError("no \\begin{document} outside comments; cannot delimit the preamble")
        slots = self._preamble_abstract_slots(end)
        self._emit_block(0, end, BlockCategory.PREAMBLE, environment="", decided_by=None, slots=slots)
        return end

    def _find_begin_document(self) -> int | None:
        position = 0
        while position < self.length:
            match = TOP_LEVEL_SPECIAL_RE.search(self.text, position)
            if match is None:
                return None
            position = match.start()
            if self.text[position] == "%":
                position = _skip_comment(self.text, position)
                continue
            if self.text[position] == "$":
                position += 1
                continue
            name, after_name = read_control_sequence(self.text, position)
            if name == "verb":
                position = skip_verb(self.text, after_name)
                continue
            if name == "begin":
                environment, after = read_environment_name(self.text, after_name)
                if environment == DOCUMENT_ENVIRONMENT:
                    return after
                position = after_name if environment is None else after
                continue
            position = after_name
        return None

    def _preamble_abstract_slots(self, preamble_end: int) -> list[_CaptionSlot]:
        position = 0
        while position < preamble_end:
            match = TOP_LEVEL_SPECIAL_RE.search(self.text, position)
            if match is None or match.start() >= preamble_end:
                return []
            position = match.start()
            if self.text[position] == "%":
                position = _skip_comment(self.text, position)
                continue
            if self.text[position] == "$":
                position += 1
                continue
            name, after_name = read_control_sequence(self.text, position)
            if name == "verb":
                position = skip_verb(self.text, after_name)
                continue
            if name != "begin":
                position = after_name
                continue
            environment, after = read_environment_name(self.text, after_name)
            if environment is None:
                position = after_name
                continue
            if environment != ABSTRACT_ENVIRONMENT:
                position = after
                continue
            body_end, _ = self._scan_environment_body(after, environment, category=None, collect_captions=False)
            close_start = self._environment_close_start(body_end, environment)
            return [_CaptionSlot(start=after, end=close_start, kind=CaptionKind.ABSTRACT)]
        return []

    def _environment_close_start(self, environment_end: int, name: str) -> int:
        close = self.text.rfind("\\end", 0, environment_end)
        if close < 0:
            raise MaskError(f"failed to locate the \\end of environment {name}")
        return close

    def _mask_body(self, position: int) -> None:
        seen_end_document = False
        while position < self.length:
            match = TOP_LEVEL_SPECIAL_RE.search(self.text, position)
            if match is None:
                break
            position = match.start()
            character = self.text[position]
            if character == "%":
                position = self._mask_comment(position)
                continue
            if character == "$":
                position = self._mask_dollar(position)
                continue
            name, after_name = read_control_sequence(self.text, position)
            if name == "verb":
                position = skip_verb(self.text, after_name)
                continue
            if name == "[":
                position = self._mask_display_math(position, after_name, "\\]")
                continue
            if name == "(":
                position = skip_to_delimiter(self.text, after_name, "\\)", "\\(")
                continue
            if name == "begin":
                position = self._mask_environment(position, after_name)
                continue
            if name == "end":
                environment, after = read_environment_name(self.text, after_name)
                if environment == DOCUMENT_ENVIRONMENT:
                    self._emit_block(
                        position, self.length, BlockCategory.POSTAMBLE, environment="", decided_by=None, slots=[]
                    )
                    seen_end_document = True
                    break
                position = after_name if environment is None else after
                continue
            if name in METADATA_COMMANDS:
                position = self._mask_metadata(position, after_name)
                continue
            position = after_name
        if not seen_end_document:
            self.warnings.append("no \\end{document} met in the stream; no postamble block emitted")

    def _mask_comment(self, position: int) -> int:
        line_start = self.text.rfind("\n", 0, position) + 1
        line_end = _line_end(self.text, position)
        if line_start >= self.cursor and not self.text[line_start:position].strip():
            end = line_end
            next_start = line_end + 1
            while next_start <= self.length:
                following_end = _line_end(self.text, next_start)
                if not self.text[next_start:following_end].lstrip(" \t").startswith("%"):
                    break
                end = following_end
                next_start = following_end + 1
            self._emit_block(line_start, end, BlockCategory.COMMENT, environment="", decided_by=None, slots=[])
            return end
        self._emit_block(position, line_end, BlockCategory.COMMENT, environment="", decided_by=None, slots=[])
        return line_end

    def _mask_dollar(self, position: int) -> int:
        if self.text[position + 1 : position + 2] == "$":
            end = _find_display_dollar_close(self.text, position + 2)
            self._emit_block(position, end, BlockCategory.MATH, environment="", decided_by=None, slots=[])
            return end
        return find_inline_dollar_close(self.text, position + 1)

    def _mask_display_math(self, start: int, body_start: int, closing: str) -> int:
        end = skip_to_delimiter(self.text, body_start, closing, "\\[")
        self._emit_block(start, end, BlockCategory.MATH, environment="", decided_by=None, slots=[])
        return end

    def _mask_environment(self, start: int, after_name: int) -> int:
        environment, after = read_environment_name(self.text, after_name)
        if environment is None or environment == DOCUMENT_ENVIRONMENT:
            return after_name if environment is None else after
        decision = self._decision_for(environment)
        if decision.classification is EnvironmentClass.TEXT:
            return after
        end, slots = self._scan_environment_body(
            after, environment, decision.category, collect_captions=decision.category is not BlockCategory.CODE
        )
        self._emit_block(
            start,
            end,
            decision.category or BlockCategory.UNKNOWN,
            environment=environment,
            decided_by=decision.decided_by,
            slots=slots,
        )
        decision.blocks += 1
        return end

    def _mask_metadata(self, start: int, after_name: int) -> int:
        position = skip_optional_arguments(self.text, after_name)
        if position >= self.length or self.text[position] != "{":
            return after_name
        end = match_group(self.text, position)
        self._emit_block(start, end, BlockCategory.METADATA, environment="", decided_by=None, slots=[])
        return end

    def _scan_environment_body(
        self, body_start: int, name: str, category: BlockCategory | None, *, collect_captions: bool
    ) -> tuple[int, list[_CaptionSlot]]:
        if category is BlockCategory.CODE:
            return _skip_code_environment(self.text, body_start, name), []
        slots: list[_CaptionSlot] = []
        depth = 1
        position = body_start
        while position < self.length:
            match = BODY_SPECIAL_RE.search(self.text, position)
            if match is None:
                break
            position = match.start()
            if self.text[position] == "%":
                position = _skip_comment(self.text, position)
                continue
            command, after_name = read_control_sequence(self.text, position)
            if command == "verb":
                position = skip_verb(self.text, after_name)
                continue
            if command == "begin":
                environment, after = read_environment_name(self.text, after_name)
                if environment is None:
                    position = after_name
                    continue
                if environment == name:
                    depth += 1
                    position = after
                    continue
                nested = self._decision_for(environment)
                position = (
                    _skip_code_environment(self.text, after, environment)
                    if nested.category is BlockCategory.CODE
                    else after
                )
                continue
            if command == "end":
                environment, after = read_environment_name(self.text, after_name)
                if environment is None:
                    position = after_name
                    continue
                if environment == name:
                    depth -= 1
                    if depth == 0:
                        return after, slots
                position = after
                continue
            if collect_captions and command in (CAPTION_COMMAND, CAPTION_OF_COMMAND):
                slot, position = _read_caption_slot(self.text, command, after_name)
                if slot is not None:
                    slots.append(slot)
                continue
            position = after_name
        raise MaskError(
            f"environment {name} (opened at line {self._line_of(body_start)}) has no matching \\end before end of file"
        )

    def _decision_for(self, name: str) -> EnvironmentDecision:
        decision = self.environments.get(name)
        if decision is None:
            decision = _decide(name, self.declared, self.table, occurrences=0)
            self.environments[name] = decision
        return decision

    def _emit_block(
        self,
        start: int,
        end: int,
        category: BlockCategory,
        *,
        environment: str,
        decided_by: DecidedBy | None,
        slots: list[_CaptionSlot],
    ) -> None:
        block_id = f"{BLOCK_ID_PREFIX}-{len(self.blocks)}"
        captions = [self._emit_caption(block_id, slot) for slot in slots]
        raw = self.text[start:end]
        self.blocks.append(
            Block(
                id=block_id,
                category=category,
                environment=environment,
                decided_by=decided_by,
                labels=tuple(LABEL_RE.findall(raw)),
                tex=_apply_slots(self.text, start, end, slots, [caption.id for caption in captions]),
                start=start,
                end=end,
                line=self._line_of(start),
            )
        )
        self.output.append(self.text[self.cursor : start])
        self.output.append(block_token(block_id))
        for caption in captions:
            self.output.append(f"\n{block_token(caption.id)} {caption.masked_text}\n")
        self.cursor = end

    def _emit_caption(self, block_id: str, slot: _CaptionSlot) -> Caption:
        raw = self.text[slot.start : slot.end]
        caption = Caption(
            id=f"{CAPTION_ID_PREFIX}-{len(self.captions)}",
            block_id=block_id,
            kind=slot.kind,
            tex=raw,
            masked_text=_single_line_text(raw),
        )
        self.captions.append(caption)
        return caption

    def _line_of(self, position: int) -> int:
        return bisect_right(self.line_starts, position)


def read_control_sequence(text: str, position: int) -> tuple[str, int]:
    start = position + 1
    if start >= len(text):
        return "", start
    if text[start].isascii() and text[start].isalpha():
        end = start
        while end < len(text) and text[end].isascii() and text[end].isalpha():
            end += 1
        return text[start:end], end
    return text[start], start + 1


def read_environment_name(text: str, position: int) -> tuple[str | None, int]:
    cursor = _skip_argument_space(text, position)
    if cursor >= len(text) or text[cursor] != "{":
        return None, position
    match = ENVIRONMENT_NAME_RE.match(text, cursor + 1)
    if match is None or match.end() >= len(text) or text[match.end()] != "}":
        return None, position
    return match.group(0), match.end() + 1


def _read_declaration_name(text: str, position: int) -> tuple[str | None, int]:
    cursor = _skip_argument_space(text, position)
    if cursor < len(text) and text[cursor] == "*":
        cursor = _skip_argument_space(text, cursor + 1)
    if cursor >= len(text) or text[cursor] != "{":
        return None, position
    match = ENVIRONMENT_NAME_RE.match(text, cursor + 1)
    if match is None or match.end() >= len(text) or text[match.end()] != "}":
        return None, position
    return match.group(0), match.end() + 1


def _read_caption_slot(text: str, command: str, position: int) -> tuple[_CaptionSlot | None, int]:
    cursor = position
    if cursor < len(text) and text[cursor] == "*":
        cursor += 1
    if command == CAPTION_OF_COMMAND:
        cursor = _skip_argument_space(text, cursor)
        if cursor >= len(text) or text[cursor] != "{":
            return None, cursor
        cursor = match_group(text, cursor)
    cursor = skip_optional_arguments(text, cursor)
    if cursor >= len(text) or text[cursor] != "{":
        return None, cursor
    end = match_group(text, cursor)
    return _CaptionSlot(start=cursor + 1, end=end - 1, kind=CaptionKind.CAPTION), end


def _skip_comment(text: str, position: int) -> int:
    return _line_end(text, position)


def skip_verb(text: str, position: int) -> int:
    cursor = position
    if cursor < len(text) and text[cursor] == "*":
        cursor += 1
    if cursor >= len(text):
        return cursor
    delimiter = text[cursor]
    line_end = _line_end(text, cursor)
    close = text.find(delimiter, cursor + 1, line_end)
    return line_end if close < 0 else close + 1


def _skip_code_environment(text: str, body_start: int, name: str) -> int:
    marker = f"\\end{{{name}}}"
    close = text.find(marker, body_start)
    if close < 0:
        raise MaskError(f"environment {name} has no matching {marker} before end of file")
    return close + len(marker)


def _skip_argument_space(text: str, position: int) -> int:
    cursor = position
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    if cursor < len(text) and text[cursor] == "\n":
        cursor += 1
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1
    return cursor


def skip_optional_arguments(text: str, position: int) -> int:
    cursor = position
    while True:
        cursor = _skip_argument_space(text, cursor)
        if cursor >= len(text) or text[cursor] != "[":
            return cursor
        cursor = _match_bracket(text, cursor)


def match_group(text: str, position: int) -> int:
    return _match_delimited(text, position, "{", "}")


def _match_bracket(text: str, position: int) -> int:
    return _match_delimited(text, position, "[", "]")


def _match_delimited(text: str, position: int, opening: str, closing: str) -> int:
    depth = 0
    cursor = position
    length = len(text)
    while cursor < length:
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "%":
            cursor = _skip_comment(text, cursor)
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    raise MaskError(f"{opening}...{closing} starting at offset {position} never closes; braces are unbalanced")


def skip_to_delimiter(text: str, position: int, closing: str, opening: str) -> int:
    cursor = position
    length = len(text)
    while cursor < length:
        index = text.find("\\", cursor)
        if index < 0:
            break
        if text.startswith(closing, index):
            return index + len(closing)
        cursor = index + 2
    raise MaskError(f"{opening} starting at offset {position} has no matching {closing} before end of file")


def find_inline_dollar_close(text: str, position: int) -> int:
    cursor = position
    length = len(text)
    while cursor < length:
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "$":
            return cursor + 1
        cursor += 1
    raise MaskError(f"inline math starting at offset {position - 1} has no closing $ before end of file")


def _find_display_dollar_close(text: str, position: int) -> int:
    cursor = position
    length = len(text)
    while cursor < length:
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "$" and text[cursor + 1 : cursor + 2] == "$":
            return cursor + 2
        cursor += 1
    raise MaskError(f"display math starting at offset {position - 2} has no closing $$ before end of file")


def _line_end(text: str, position: int) -> int:
    index = text.find("\n", position)
    return len(text) if index < 0 else index


def _line_starts(text: str) -> list[int]:
    starts = [0]
    index = text.find("\n")
    while index >= 0:
        starts.append(index + 1)
        index = text.find("\n", index + 1)
    return starts


def _check_sentinels(text: str) -> None:
    present = [character for character in (SENTINEL_OPEN, SENTINEL_CLOSE) if character in text]
    if present:
        raise MaskError(
            f"source contains placeholder sentinel characters {', '.join(present)}, conflicting with the masked text format"
        )


def _apply_slots(text: str, start: int, end: int, slots: Sequence[_CaptionSlot], caption_ids: Sequence[str]) -> str:
    parts: list[str] = []
    cursor = start
    for slot, caption_id in zip(slots, caption_ids, strict=True):
        parts.append(text[cursor : slot.start])
        parts.append(block_token(caption_id))
        cursor = slot.end
    parts.append(text[cursor:end])
    return "".join(parts)


def _single_line_text(raw: str) -> str:
    paragraphs = [_normalize_whitespace(part) for part in BLANK_LINE_RE.split(raw)]
    return PARAGRAPH_JOINER.join(part for part in paragraphs if part)


def _normalize_whitespace(raw: str) -> str:
    return WHITESPACE_RUN_RE.sub(" ", COMMENT_TAIL_RE.sub("", raw)).strip()


def _describe_difference(source: str, restored: str) -> str:
    limit = min(len(source), len(restored))
    position = limit
    for index in range(limit):
        if source[index] != restored[index]:
            position = index
            break
    start = max(0, position - DIFFERENCE_CONTEXT_CHARS)
    stop = position + DIFFERENCE_CONTEXT_CHARS
    return (
        f"roundtrip check failed: first difference at offset {position} (source {len(source)} chars, "
        f"restored {len(restored)} chars); source {source[start:stop]!r}; restored {restored[start:stop]!r}"
    )


def _restore_captions(
    masked: str, captions: Sequence[Caption]
) -> tuple[dict[str, str], list[str], dict[str, str], str]:
    filled: dict[str, str] = {}
    fallbacks: list[str] = []
    translated: dict[str, str] = {}
    stream = masked
    for caption in captions:
        token = block_token(caption.id)
        occurrences = stream.count(token)
        if occurrences > 1:
            raise MaskError(
                f"{token} appears {occurrences} times in the stream; each caption token may appear at most once"
            )
        if occurrences == 0:
            filled[caption.id] = caption.tex
            fallbacks.append(caption.id)
            continue
        index = stream.index(token)
        previous_newline = stream.rfind("\n", 0, index)
        following_newline = stream.find("\n", index)
        line_start = 0 if previous_newline < 0 else previous_newline + 1
        line_stop = len(stream) if following_newline < 0 else following_newline
        segment_start = 0 if previous_newline < 0 else previous_newline
        segment_stop = len(stream) if following_newline < 0 else following_newline + 1
        line = stream[line_start:line_stop]
        text = line[line.index(token) + len(token) :]
        if text.startswith(" "):
            text = text[1:]
        if text == caption.masked_text:
            filled[caption.id] = caption.tex
        else:
            filled[caption.id] = text
            translated[caption.id] = text
        stream = stream[:segment_start] + stream[segment_stop:]
    return filled, fallbacks, translated, stream


def _restore_blocks(stream: str, blocks: Sequence[Block], filled: Mapping[str, str]) -> str:
    block_by_id = {block.id: block for block in blocks}
    used: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        token_id = f"{match.group(1)}-{match.group(2)}"
        block = block_by_id.get(token_id)
        if block is None:
            raise MaskError(f"{match.group(0)} in the stream has no entry in the blocks records")
        used[token_id] = used.get(token_id, 0) + 1
        return _fill_slots(block.tex, filled)

    text = TOKEN_RE.sub(replace, stream)
    for block in blocks:
        count = used.get(block.id, 0)
        if count != 1:
            raise MaskError(
                f"{block_token(block.id)} is used {count} times in the stream; each block token must be used exactly once"
            )
    return text


def _fill_slots(tex: str, filled: Mapping[str, str]) -> str:

    def replace(match: re.Match[str]) -> str:
        token_id = f"{match.group(1)}-{match.group(2)}"
        text = filled.get(token_id)
        if text is None:
            raise MaskError(f"{match.group(0)} inside a block has no entry in the captions records")
        return text

    return TOKEN_RE.sub(replace, tex)
