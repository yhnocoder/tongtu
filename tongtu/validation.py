from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SENTINEL_OPEN = "⟦"
SENTINEL_CLOSE = "⟧"

BLOCK_ID_PREFIX = "BLK"
CAPTION_ID_PREFIX = "CAP"

TOKEN_RE = re.compile(rf"{SENTINEL_OPEN}({BLOCK_ID_PREFIX}|{CAPTION_ID_PREFIX})-([0-9]+){SENTINEL_CLOSE}")

BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")

ENVIRONMENT_NAME_RE = re.compile(r"[A-Za-z0-9@]+\*?")

USAGE = "usage: validate.py SOURCE_FILE TRANSLATED_FILE"

EXIT_OK = 0

EXIT_FAILURE = 1


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


CHECK_PLACEHOLDERS = "placeholders"
CHECK_CONTROL_SEQUENCES = "control_sequences"
CHECK_BRACES_AND_MATH = "braces_and_math"
CHECK_PARAGRAPH_COUNT = "paragraph_count"
CHECK_NAMES: tuple[str, ...] = (
    CHECK_PLACEHOLDERS,
    CHECK_CONTROL_SEQUENCES,
    CHECK_BRACES_AND_MATH,
    CHECK_PARAGRAPH_COUNT,
)

ENVIRONMENT_DELIMITER_RE = re.compile(
    r"\\begin\s*\{" + ENVIRONMENT_NAME_RE.pattern + r"\}(?:\[[^\]]*\])*(?:\{[^{}]*\})*"
    r"|\\end\s*\{" + ENVIRONMENT_NAME_RE.pattern + r"\}"
)

CONTROL_SEQUENCE_NAME_RE = re.compile(r"\\(?:[A-Za-z]+\*?|.)", re.DOTALL)

NON_TEXT_ARGUMENT_COMMANDS: tuple[str, ...] = (
    "vspace",
    "hspace",
    "label",
    "bibliography",
    "bibliographystyle",
    "definecolor",
    "setcounter",
    "setlength",
    "input",
    "include",
    "includegraphics",
    "usepackage",
    "ref",
    "eqref",
    "cite",
    "citep",
    "citet",
)

NON_TEXT_COMMAND_RE = re.compile(
    r"\\(?:" + "|".join(NON_TEXT_ARGUMENT_COMMANDS) + r")(?![A-Za-z])\*?(?:\[[^\]]*\])*(?:\{[^{}]*\})*"
)

DIFFERENCE_ITEMS_MAX = 8

HEADING_COMMAND = (
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?"
    r"(?:\[[^\]]*\])?\s*\{(?:[^{}]|\{[^{}]*\})*\}"
)

HEADING_LINE_RE = re.compile(rf"({HEADING_COMMAND})[ \t]*\n(?:[ \t]*\n)+")

RUN_IN_HEADING_RE = re.compile(rf"(\S[ \t]*)({HEADING_COMMAND})")

OPEN_BRACE = "{"

CLOSE_BRACE = "}"

DOLLAR = "$"

PERCENT = "%"

COUNTED_CHARACTERS: tuple[str, ...] = (OPEN_BRACE, CLOSE_BRACE, DOLLAR, PERCENT)


@dataclass(frozen=True)
class Scan:
    control_sequences: tuple[str, ...]
    specials: tuple[tuple[str, int], ...]

    def count(self, character: str) -> int:
        return sum(1 for found, _position in self.specials if found == character)


@dataclass(frozen=True)
class Failure:
    check: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    failures: tuple[Failure, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def validate(source: str, translation: str) -> ValidationResult:
    scanned_source = scan(source)
    scanned_translation = scan(translation)
    failures = [
        failure
        for failure in (
            _check_placeholders(source, translation),
            check_control_sequences(scanned_source, scanned_translation),
            _check_braces_and_math(scanned_source, scanned_translation),
            _check_paragraph_count(source, translation),
        )
        if failure is not None
    ]
    return ValidationResult(failures=tuple(failures))


def _check_placeholders(source: str, translation: str) -> Failure | None:
    expected = Counter(match.group(0) for match in TOKEN_RE.finditer(source))
    actual = Counter(match.group(0) for match in TOKEN_RE.finditer(translation))
    if expected != actual:
        missing = expected - actual
        extra = actual - expected
        parts = []
        if missing:
            parts.append(f"translation is missing {_describe_counter(missing)}")
        if extra:
            parts.append(f"translation has extra {_describe_counter(extra)}")
        return Failure(check=CHECK_PLACEHOLDERS, message="; ".join(parts))
    complete = sum(actual.values())
    opens = translation.count(SENTINEL_OPEN)
    closes = translation.count(SENTINEL_CLOSE)
    if opens != complete or closes != complete:
        return Failure(
            check=CHECK_PLACEHOLDERS,
            message=(
                f"translation has {complete} complete placeholders but {opens} {SENTINEL_OPEN} and "
                f"{closes} {SENTINEL_CLOSE}: broken placeholder fragments are present; "
                f"{SENTINEL_OPEN} and {SENTINEL_CLOSE} may only appear inside complete placeholders"
            ),
        )
    return None


def check_control_sequences(source: Scan, translation: Scan) -> Failure | None:
    expected = Counter(source.control_sequences)
    actual = Counter(translation.control_sequences)
    if expected == actual:
        return None
    names = sorted(set(expected) | set(actual))
    differing = [name for name in names if expected[name] != actual[name]]
    listed = "; ".join(
        f"\\{name} appears {expected[name]} times in source, {actual[name]} in translation"
        for name in differing[:DIFFERENCE_ITEMS_MAX]
    )
    if len(differing) > DIFFERENCE_ITEMS_MAX:
        listed = f"{listed} and more ({len(differing)} items in total)"
    return Failure(check=CHECK_CONTROL_SEQUENCES, message=listed)


def _check_braces_and_math(source: Scan, translation: Scan) -> Failure | None:
    problems: list[str] = []
    position = _unbalanced_position(translation)
    if position is not None and _unbalanced_position(source) is None:
        problems.append(f"{OPEN_BRACE} {CLOSE_BRACE} unbalanced at character {position}")
    dollars = translation.count(DOLLAR)
    expected_dollars = source.count(DOLLAR)
    if dollars % 2:
        problems.append(f"{DOLLAR} count in translation is {dollars}, an odd number, so they cannot pair up")
    if dollars < expected_dollars:
        problems.append(f"{DOLLAR} count differs: {expected_dollars} in source, {dollars} in translation")
    percents = translation.count(PERCENT)
    expected_percents = source.count(PERCENT)
    if percents > expected_percents:
        problems.append(
            f"unescaped {PERCENT} count differs: {expected_percents} in source, {percents} in translation; "
            f"{PERCENT} starts a comment, a literal percent sign must be written \\{PERCENT}"
        )
    if not problems:
        return None
    return Failure(check=CHECK_BRACES_AND_MATH, message="; ".join(problems))


def _unbalanced_position(scanned: Scan) -> int | None:
    opened: list[int] = []
    for character, position in scanned.specials:
        if character == OPEN_BRACE:
            opened.append(position)
        elif character == CLOSE_BRACE:
            if not opened:
                return position
            opened.pop()
    return opened[0] if opened else None


def _check_paragraph_count(source: str, translation: str) -> Failure | None:
    expected = translatable_paragraphs(_attach_headings(source))
    actual = translatable_paragraphs(_attach_headings(translation))
    if expected == actual:
        return None
    return Failure(
        check=CHECK_PARAGRAPH_COUNT,
        message=(
            f"paragraphs with translatable text: {expected} in source, {actual} in translation. "
            "Blank lines delimit paragraphs; do not merge, split, skip or add any"
        ),
    )


def scan(text: str) -> Scan:
    sequences: list[str] = []
    specials: list[tuple[str, int]] = []
    position = 0
    length = len(text)
    while position < length:
        character = text[position]
        if character == "\\":
            name, after = read_control_sequence(text, position)
            if text[after : after + 1] == "*" and name.isalpha():
                name, after = name + "*", after + 1
            sequences.append(name)
            position = after
            continue
        if character in COUNTED_CHARACTERS:
            specials.append((character, position))
        position += 1
    return Scan(control_sequences=tuple(sequences), specials=tuple(specials))


def _attach_headings(text: str) -> str:
    detached = RUN_IN_HEADING_RE.sub(lambda match: f"{match.group(1)}\n\n{match.group(2)}", text)
    return HEADING_LINE_RE.sub(lambda match: match.group(1) + "\n", detached)


def translatable_paragraphs(text: str) -> int:
    return sum(
        1 for paragraph in BLANK_LINE_RE.split(text) if paragraph.strip() and _has_translatable_text(paragraph.strip())
    )


def _has_translatable_text(paragraph: str) -> bool:
    stripped = TOKEN_RE.sub("", paragraph)
    stripped = ENVIRONMENT_DELIMITER_RE.sub("", stripped)
    stripped = NON_TEXT_COMMAND_RE.sub("", stripped)
    stripped = CONTROL_SEQUENCE_NAME_RE.sub("", stripped)
    return bool(stripped.strip())


def _describe_counter(counter: Counter[str]) -> str:
    items = sorted(counter.items())
    listed = ", ".join(f"{item}" + (f" x{count}" if count > 1 else "") for item, count in items[:DIFFERENCE_ITEMS_MAX])
    if len(items) > DIFFERENCE_ITEMS_MAX:
        return f"{listed} and more ({len(items)} items in total)"
    return listed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(USAGE)
        return EXIT_FAILURE
    try:
        source = Path(argv[0]).read_text(encoding="utf-8")
        translation = Path(argv[1]).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"cannot read file: {error}")
        return EXIT_FAILURE
    result = validate(source.strip(), translation.strip())
    failures = {failure.check: failure.message for failure in result.failures}
    for layer in CHECK_NAMES:
        if layer in failures:
            print(f"  [fail] {layer}: {failures[layer]}")
        else:
            print(f"  [pass] {layer}")
    return EXIT_OK if result.ok else EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
