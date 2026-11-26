from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SENTINEL_OPEN = "⟦"
SENTINEL_CLOSE = "⟧"

BLOCK_ID_PREFIX = "BLK"
CAPTION_ID_PREFIX = "CAP"

TOKEN_RE = re.compile(rf"{SENTINEL_OPEN}({BLOCK_ID_PREFIX}|{CAPTION_ID_PREFIX})-([0-9]+){SENTINEL_CLOSE}")

BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")

ENVIRONMENT_NAME_RE = re.compile(r"[A-Za-z0-9@]+\*?")

USAGE = "usage: validate.py SOURCE_FILE TRANSLATED_FILE [--glossary FILE]..."

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
CHECK_CAPTION_LINES = "caption_lines"

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
    "autoref",
    "cref",
    "Cref",
    "cite[A-Za-z]*",
    "url",
)

NON_TEXT_COMMAND_RE = re.compile(
    r"\\(?:" + "|".join(NON_TEXT_ARGUMENT_COMMANDS) + r")(?![A-Za-z])\*?(?:\[[^\]]*\])*(?:\{[^{}]*\})*"
)

DIFFERENCE_ITEMS_MAX = 8

PARAGRAPH_PREVIEW_LENGTH = 60

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


def _structure_failures(source: str, translation: str) -> list[Failure]:
    scanned_source = scan(source)
    scanned_translation = scan(translation)
    return [
        failure
        for failure in (
            _check_placeholders(source, translation),
            check_control_sequences(scanned_source, scanned_translation),
            _check_braces_and_math(scanned_source, scanned_translation),
        )
        if failure is not None
    ]


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


def _describe_counter(counter: Counter[str]) -> str:
    items = sorted(counter.items())
    listed = ", ".join(f"{item}" + (f" x{count}" if count > 1 else "") for item, count in items[:DIFFERENCE_ITEMS_MAX])
    if len(items) > DIFFERENCE_ITEMS_MAX:
        return f"{listed} and more ({len(items)} items in total)"
    return listed


UNTRANSLATED_WORDS_MIN = 6

PROBLEM_PREVIEW_ITEMS = 4

GLOSSARY_OPTION = "--glossary"

TERMS_FIELD = "terms"

DO_NOT_TRANSLATE_FIELD = "do_not_translate"

STYLE_FIELD = "style"

GLOSSARY_FIELDS: tuple[str, ...] = (TERMS_FIELD, DO_NOT_TRANSLATE_FIELD, STYLE_FIELD)

CJK_RE = re.compile(r"[\u4e00-\u9fff]")

ENGLISH_WORD_RE = re.compile(r"(?<![A-Za-z])[a-z][A-Za-z]{2,}")

INLINE_MATH_RE = re.compile(r"(?<!\\)\$(?:\\.|[^$\\])*\$")

COMMAND_NAME_RE = re.compile(r"\\[A-Za-z@]+")

SECTION_HEADING_RE = re.compile(r"\\(?:sub){0,2}section\*?\s*(?:\[[^\]]*\])?\s*\{")

PLURAL_SUFFIX_RE = re.compile(r"(?:es|s)$")

COMPOUND_HYPHEN_BEFORE = r"(?<!(?<!-)-)"

COMPOUND_HYPHEN_AFTER = r"(?!-(?!-))"


class Category(StrEnum):
    STRUCTURE = "structure"
    FORMULA = "formula"
    HEADING = "heading"
    UNTRANSLATED = "untranslated"
    GLOSSARY = "glossary"


@dataclass(frozen=True)
class Problem:
    category: Category
    message: str


@dataclass(frozen=True)
class Report:
    problems: tuple[Problem, ...]
    paragraphs_total: int
    paragraphs_translated: int

    @property
    def ok(self) -> bool:
        return not self.problems


class GlossaryFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Glossary:
    terms: tuple[tuple[str, str], ...] = ()
    do_not_translate: tuple[str, ...] = ()
    style: str | None = None

    def entries(self) -> list[tuple[str, str | None]]:
        return [*((word, None) for word in self.do_not_translate), *self.terms]


def check(
    source: str,
    translation: str,
    terms: Mapping[str, str] | None = None,
    do_not_translate: Sequence[str] = (),
) -> Report:
    terms = dict(terms or {})
    source_paragraphs = paragraphs(source)
    translation_paragraphs = paragraphs(translation)
    total = paragraphs_to_translate(source)
    if len(source_paragraphs) != len(translation_paragraphs):
        problem = Problem(
            Category.STRUCTURE,
            f"paragraph counts differ: {len(source_paragraphs)} in the source, {len(translation_paragraphs)} in the "
            "translation; blank lines delimit paragraphs. Fix the paragraph count first, "
            "the paragraph-by-paragraph checks run only after that",
        )
        return Report(problems=(problem,), paragraphs_total=total, paragraphs_translated=0)
    problems = _caption_line_problems(translation)
    translated = 0
    for (_line, source_paragraph), (line, translation_paragraph) in zip(
        source_paragraphs, translation_paragraphs, strict=True
    ):
        where = f"paragraph starting at line {line} of the translation"
        problems.extend(
            Problem(Category.STRUCTURE, f"{where}, {failure.check}: {failure.message}")
            for failure in _structure_failures(source_paragraph, translation_paragraph)
        )
        problems.extend(_caption_paragraph_problems(where, source_paragraph, translation_paragraph))
        problems.extend(_paragraph_problems(where, source_paragraph, translation_paragraph, terms, do_not_translate))
        if not _needs_translation(source_paragraph):
            continue
        if CJK_RE.search(translation_paragraph):
            translated += 1
        elif _english_words(without_headings(translation_paragraph)) >= UNTRANSLATED_WORDS_MIN:
            preview = translation_paragraph[:PARAGRAPH_PREVIEW_LENGTH]
            problems.append(Problem(Category.UNTRANSLATED, f"{where} is still English: {preview!r}"))
    problems.extend(
        Problem(
            Category.GLOSSARY,
            f"glossary term {word!r} translates to {term_translation!r}, "
            f"which does not keep the do-not-translate word {kept!r} verbatim",
        )
        for word, term_translation, kept in kept_word_conflicts(terms, do_not_translate)
    )
    return Report(problems=tuple(problems), paragraphs_total=total, paragraphs_translated=translated)


def _caption_line_problems(translation: str) -> list[Problem]:
    problems: list[Problem] = []
    for number, line in enumerate(translation.split("\n"), start=1):
        tokens = list(TOKEN_RE.finditer(line))
        for match in tokens:
            if match.group(1) != CAPTION_ID_PREFIX:
                continue
            token = match.group(0)
            where = f"line {number} of the translation, {CHECK_CAPTION_LINES}"
            if match.start() != 0:
                problems.append(
                    Problem(
                        Category.STRUCTURE,
                        f"{where}: {token} must start its line; compile replaces the whole line with the caption, "
                        "so the text before it would be lost",
                    )
                )
            others = [other.group(0) for other in tokens if other is not match]
            if others:
                problems.append(
                    Problem(
                        Category.STRUCTURE,
                        f"{where}: the line of {token} also holds {', '.join(others)}; "
                        "a caption line may hold no other placeholder",
                    )
                )
    return problems


def _caption_paragraph_problems(where: str, source: str, translation: str) -> list[Problem]:
    if not any(match.group(1) == CAPTION_ID_PREFIX for match in TOKEN_RE.finditer(source)):
        return []
    source_lines = source.count("\n") + 1
    translation_lines = translation.count("\n") + 1
    if source_lines == translation_lines:
        return []
    return [
        Problem(
            Category.STRUCTURE,
            f"{where}, {CHECK_CAPTION_LINES}: a caption must be written on one line, because compile takes the whole "
            "line of the caption placeholder as the caption; this paragraph must have as many lines as the source, "
            f"which has {source_lines} lines, the translation has {translation_lines}",
        )
    ]


def _paragraph_problems(
    where: str, source: str, translation: str, terms: Mapping[str, str], do_not_translate: Sequence[str]
) -> list[Problem]:
    problems: list[Problem] = []
    source_headings = heading_texts(source)
    if source_headings != heading_texts(translation):
        problems.append(
            Problem(
                Category.HEADING,
                f"{where}: section headings must stay identical to the source, "
                f"which has {source_headings[:PROBLEM_PREVIEW_ITEMS]}",
            )
        )
    source_formulas = sorted(INLINE_MATH_RE.findall(source))
    translation_formulas = sorted(INLINE_MATH_RE.findall(translation))
    if source_formulas != translation_formulas:
        missing = [formula for formula in source_formulas if formula not in translation_formulas]
        extra = [formula for formula in translation_formulas if formula not in source_formulas]
        problems.append(
            Problem(
                Category.FORMULA,
                f"{where}: inline formulas differ from the source; missing from the translation "
                f"{missing[:PROBLEM_PREVIEW_ITEMS]}, extra in the translation {extra[:PROBLEM_PREVIEW_ITEMS]}",
            )
        )
    if not CJK_RE.search(translation):
        return problems
    source_text = searchable(source)
    translation_text = searchable(translation)
    for word, term_translation in terms.items():
        if hits(word, source_text) and term_translation not in translation_text:
            problems.append(
                Problem(
                    Category.GLOSSARY, f"{where}: the source has {word!r}, the translation lacks {term_translation!r}"
                )
            )
    for word in do_not_translate:
        if kept_word_in_source(word, source_text) and not hits(word, translation_text, exact=True):
            problems.append(
                Problem(
                    Category.GLOSSARY,
                    f"{where}: the source has the do-not-translate word {word!r}, the translation does not keep it",
                )
            )
    return problems


def paragraphs(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    line = 1
    for part in re.split(f"({BLANK_LINE_RE.pattern})", text):
        if part.strip() and not BLANK_LINE_RE.fullmatch(part):
            lead = len(part) - len(part.lstrip("\n"))
            found.append((line + part[:lead].count("\n"), part.strip()))
        line += part.count("\n")
    return found


def paragraphs_to_translate(text: str) -> int:
    return sum(1 for _line, paragraph in paragraphs(text) if _needs_translation(paragraph))


def _needs_translation(paragraph: str) -> bool:
    return _english_words(without_headings(paragraph)) >= UNTRANSLATED_WORDS_MIN


def _english_words(paragraph: str) -> int:
    stripped = TOKEN_RE.sub("", paragraph)
    stripped = INLINE_MATH_RE.sub("", stripped)
    stripped = NON_TEXT_COMMAND_RE.sub("", stripped)
    stripped = CONTROL_SEQUENCE_NAME_RE.sub("", stripped)
    return len(ENGLISH_WORD_RE.findall(stripped))


def _heading_spans(paragraph: str) -> list[tuple[int, int]]:
    spans = []
    for match in SECTION_HEADING_RE.finditer(paragraph):
        depth = 1
        position = match.end()
        while position < len(paragraph) and depth:
            character = paragraph[position]
            if character == "\\":
                position += 2
                continue
            depth += (character == OPEN_BRACE) - (character == CLOSE_BRACE)
            position += 1
        spans.append((match.start(), position))
    return spans


def heading_texts(paragraph: str) -> list[str]:
    return [paragraph[start:end] for start, end in _heading_spans(paragraph)]


def without_headings(paragraph: str) -> str:
    for start, end in reversed(_heading_spans(paragraph)):
        paragraph = paragraph[:start] + " " + paragraph[end:]
    return paragraph


def searchable(text: str) -> str:
    stripped = INLINE_MATH_RE.sub(" ", without_headings(text))
    return COMMAND_NAME_RE.sub(" ", NON_TEXT_COMMAND_RE.sub(" ", stripped))


def hits(word: str, text: str, exact: bool = False, *, in_compounds: bool = True) -> bool:
    case_sensitive = exact or (any(character.isalpha() for character in word) and word == word.upper())
    body = r"\s+".join(re.escape(part) for part in word.split())
    before = "" if in_compounds else COMPOUND_HYPHEN_BEFORE
    after = "" if in_compounds else COMPOUND_HYPHEN_AFTER
    pattern = rf"(?<![0-9A-Za-z]){before}{body}(?:es|s)?(?![0-9A-Za-z]){after}"
    return re.search(pattern, text, 0 if case_sensitive else re.IGNORECASE) is not None


def kept_word_in_source(word: str, source_text: str) -> bool:
    return hits(word, source_text, exact=True, in_compounds=False)


def term_key(word: str) -> str:
    collapsed = " ".join(word.replace("-", " ").split()).casefold()
    return PLURAL_SUFFIX_RE.sub("", collapsed)


def kept_word_conflicts(terms: Mapping[str, str], do_not_translate: Sequence[str]) -> list[tuple[str, str, str]]:
    return [
        (word, translation, kept)
        for word, translation in terms.items()
        for kept in do_not_translate
        if hits(kept, word, exact=True) and not hits(kept, translation, exact=True)
    ]


def parse_glossary(content: str, origin: str) -> Glossary:
    try:
        data = json.loads(content, object_pairs_hook=lambda pairs: _unique_object(pairs, origin))
    except json.JSONDecodeError as error:
        raise GlossaryFormatError(f"{origin} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise GlossaryFormatError(f"the top level of {origin} must be an object, got {type(data).__name__}")
    unknown = [key for key in data if key not in GLOSSARY_FIELDS]
    if unknown:
        raise GlossaryFormatError(
            f"{origin} has an unknown field {unknown[0]!r}; a glossary file only accepts {', '.join(GLOSSARY_FIELDS)}"
        )
    glossary = Glossary(
        terms=_parse_terms(data.get(TERMS_FIELD), origin),
        do_not_translate=_parse_do_not_translate(data.get(DO_NOT_TRANSLATE_FIELD), origin),
        style=_parse_style(data.get(STYLE_FIELD), origin),
    )
    seen: dict[str, tuple[str, str | None]] = {}
    for entry in glossary.entries():
        previous = seen.setdefault(term_key(entry[0]), entry)
        if previous != entry:
            raise GlossaryFormatError(
                f"{origin} gives two conflicting records for the same word: "
                f"{_describe_entry(previous)} and {_describe_entry(entry)}"
            )
    return glossary


def _unique_object(pairs: list[tuple[str, object]], origin: str) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data and data[key] != value:
            raise GlossaryFormatError(f"{origin} gives the key {key!r} twice with different values")
        data[key] = value
    return data


def _parse_terms(value: object, origin: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise GlossaryFormatError(
            f"{TERMS_FIELD} in {origin} must be an object mapping words to translations, got {type(value).__name__}"
        )
    pairs: list[tuple[str, str]] = []
    for raw_word, raw_translation in value.items():
        word = raw_word.strip()
        if not word:
            raise GlossaryFormatError(f"{TERMS_FIELD} in {origin} contains an empty word")
        if not isinstance(raw_translation, str):
            raise GlossaryFormatError(
                f"the translation of {TERMS_FIELD}[{word!r}] in {origin} must be a string, "
                f"got {type(raw_translation).__name__}"
            )
        translation = raw_translation.strip()
        if not translation:
            raise GlossaryFormatError(
                f"the translation of {TERMS_FIELD}[{word!r}] in {origin} is empty; "
                f"to keep the original wording put the word into {DO_NOT_TRANSLATE_FIELD}"
            )
        pairs.append((word, translation))
    return tuple(pairs)


def _parse_do_not_translate(value: object, origin: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise GlossaryFormatError(
            f"{DO_NOT_TRANSLATE_FIELD} in {origin} must be a list of strings, got {type(value).__name__}"
        )
    words: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise GlossaryFormatError(
                f"{DO_NOT_TRANSLATE_FIELD}[{index}] in {origin} must be a string, got {type(item).__name__}"
            )
        word = item.strip()
        if not word:
            raise GlossaryFormatError(f"{DO_NOT_TRANSLATE_FIELD}[{index}] in {origin} is an empty word")
        words.append(word)
    return tuple(words)


def _parse_style(value: object, origin: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GlossaryFormatError(
            f"{STYLE_FIELD} in {origin} must be a string (extra requirements addressed to the translator), "
            f"got {type(value).__name__}"
        )
    return value.strip()


def _describe_entry(entry: tuple[str, str | None]) -> str:
    word, translation = entry
    if translation is None:
        return f"{word!r} in {DO_NOT_TRANSLATE_FIELD}"
    return f"{word!r} -> {translation!r} in {TERMS_FIELD}"


def merge_glossaries(glossaries: Sequence[Glossary]) -> Glossary:
    entries: dict[str, tuple[str, str | None]] = {}
    style: str | None = None
    for glossary in glossaries:
        for entry in glossary.entries():
            key = term_key(entry[0])
            entries.pop(key, None)
            entries[key] = entry
        if glossary.style is not None:
            style = glossary.style or None
    return Glossary(
        terms=tuple((word, translation) for word, translation in entries.values() if translation is not None),
        do_not_translate=tuple(word for word, translation in entries.values() if translation is None),
        style=style,
    )


def main(argv: list[str]) -> int:
    files: list[str] = []
    glossary_files: list[str] = []
    arguments = iter(argv)
    for argument in arguments:
        if argument != GLOSSARY_OPTION:
            files.append(argument)
            continue
        value = next(arguments, None)
        if value is None:
            print(USAGE)
            return EXIT_FAILURE
        glossary_files.append(value)
    if len(files) != 2:
        print(USAGE)
        return EXIT_FAILURE
    try:
        source = Path(files[0]).read_text(encoding="utf-8")
        translation = Path(files[1]).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"cannot read file: {error}")
        return EXIT_FAILURE
    glossaries: list[Glossary] = []
    for path in glossary_files:
        try:
            glossaries.append(parse_glossary(Path(path).read_text(encoding="utf-8"), path))
        except (OSError, UnicodeDecodeError) as error:
            print(f"cannot read glossary {path}: {error}")
            return EXIT_FAILURE
        except GlossaryFormatError as error:
            print(error)
            return EXIT_FAILURE
    glossary = merge_glossaries(glossaries)
    report = check(source, translation, dict(glossary.terms), glossary.do_not_translate)
    print(f"translated paragraphs {report.paragraphs_translated}/{report.paragraphs_total}")
    for problem in report.problems:
        print(f"[{problem.category}] {problem.message}")
    print("check passed" if report.ok else f"{len(report.problems)} problems")
    return EXIT_OK if report.ok else EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
