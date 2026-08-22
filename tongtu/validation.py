from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from . import masking

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
    r"\\begin\s*\{" + masking.ENVIRONMENT_NAME_RE.pattern + r"\}(?:\[[^\]]*\])*(?:\{[^{}]*\})*"
    r"|\\end\s*\{" + masking.ENVIRONMENT_NAME_RE.pattern + r"\}"
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


COUNTED_CHARACTERS: tuple[str, ...] = ("{", "}", "$", "%")


@dataclass(frozen=True)
class Scan:
    control_sequences: tuple[str, ...]
    counts: dict[str, int]


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
            _check_control_sequences(scanned_source, scanned_translation),
            _check_braces_and_math(scanned_source, scanned_translation),
            _check_paragraph_count(source, translation),
        )
        if failure is not None
    ]
    return ValidationResult(failures=tuple(failures))


def _check_placeholders(source: str, translation: str) -> Failure | None:
    expected = Counter(match.group(0) for match in masking.TOKEN_RE.finditer(source))
    actual = Counter(match.group(0) for match in masking.TOKEN_RE.finditer(translation))
    if expected != actual:
        return Failure(check=CHECK_PLACEHOLDERS, message=_describe_multiset(expected, actual))
    complete = sum(actual.values())
    opens = translation.count(masking.SENTINEL_OPEN)
    closes = translation.count(masking.SENTINEL_CLOSE)
    if opens != complete or closes != complete:
        return Failure(
            check=CHECK_PLACEHOLDERS,
            message=(
                f"译文有 {complete} 个完整 placeholder，却出现 {opens} 个 {masking.SENTINEL_OPEN} 与 "
                f"{closes} 个 {masking.SENTINEL_CLOSE}：有残缺的 placeholder 碎片，"
                f"{masking.SENTINEL_OPEN} 与 {masking.SENTINEL_CLOSE} 只允许出现在完整 placeholder 里"
            ),
        )
    return None


def _check_control_sequences(source: Scan, translation: Scan) -> Failure | None:
    expected = Counter(source.control_sequences)
    actual = Counter(translation.control_sequences)
    if expected == actual:
        return None
    return Failure(check=CHECK_CONTROL_SEQUENCES, message=_describe_multiset(expected, actual))


def _check_braces_and_math(source: Scan, translation: Scan) -> Failure | None:
    expected = source.counts
    actual = translation.counts
    differing = [name for name in COUNTED_CHARACTERS if expected[name] != actual[name]]
    if not differing:
        return None
    listed = "；".join(f"未转义的 {name} 原文 {expected[name]} 个、译文 {actual[name]} 个" for name in differing)
    return Failure(check=CHECK_BRACES_AND_MATH, message=listed)


def _check_paragraph_count(source: str, translation: str) -> Failure | None:
    expected = translatable_paragraphs(source)
    actual = translatable_paragraphs(translation)
    if expected == actual:
        return None
    return Failure(
        check=CHECK_PARAGRAPH_COUNT,
        message=(
            f"含可译文本的段落数：原文 {expected} 段、译文 {actual} 段。空行是段落边界，不合并、不拆分、不跳过、不新增"
        ),
    )


def scan(text: str) -> Scan:
    sequences: list[str] = []
    counts = dict.fromkeys(COUNTED_CHARACTERS, 0)
    position = 0
    length = len(text)
    while position < length:
        character = text[position]
        if character == "\\":
            name, after = masking.read_control_sequence(text, position)
            if text[after : after + 1] == "*" and name.isalpha():
                name, after = name + "*", after + 1
            sequences.append(name)
            position = after
            continue
        if character in counts:
            counts[character] += 1
        position += 1
    return Scan(control_sequences=tuple(sequences), counts=counts)


def translatable_paragraphs(text: str) -> int:
    return sum(
        1
        for paragraph in masking.BLANK_LINE_RE.split(text)
        if paragraph.strip() and _has_translatable_text(paragraph.strip())
    )


def _has_translatable_text(paragraph: str) -> bool:
    stripped = masking.TOKEN_RE.sub("", paragraph)
    stripped = ENVIRONMENT_DELIMITER_RE.sub("", stripped)
    stripped = NON_TEXT_COMMAND_RE.sub("", stripped)
    stripped = CONTROL_SEQUENCE_NAME_RE.sub("", stripped)
    return bool(stripped.strip())


def _describe_multiset(expected: Counter[str], actual: Counter[str]) -> str:
    missing = expected - actual
    extra = actual - expected
    parts = []
    if missing:
        parts.append(f"译文缺 {_describe_counter(missing)}")
    if extra:
        parts.append(f"译文多出 {_describe_counter(extra)}")
    return "；".join(parts)


def _describe_counter(counter: Counter[str]) -> str:
    items = sorted(counter.items())
    listed = "、".join(f"{item}" + (f"×{count}" if count > 1 else "") for item, count in items[:DIFFERENCE_ITEMS_MAX])
    if len(items) > DIFFERENCE_ITEMS_MAX:
        return f"{listed} 等 {len(items)} 项"
    return listed
