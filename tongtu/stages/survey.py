from __future__ import annotations

import json
import re
import shutil
import time
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import tiktoken
from pydantic import ValidationError

from .. import masking
from ..artifacts.mask import BlocksFile
from ..artifacts.survey import (
    BriefFile,
    ChunkRecord,
    DecidedBy,
    DoNotTranslateEntry,
    FilteredTerm,
    Heading,
    Part,
    SurveyManifest,
    SurveyStatus,
    TermEntry,
)
from ..assets import asset_path
from ..config import config_dir
from ..console import console
from ..manifests import describe_error, write_manifest
from ..model.ask import ASK_TIMEOUT_SECONDS, AskStatus, ask
from ..model.config import RoleTable, load_config, resolve_role
from ..workdir import Workdir

STAGE_NAME = "survey"

MASKED_FILENAME = "masked.tex"

BLOCKS_FILENAME = "blocks.json"

BRIEF_FILENAME = "brief.json"

CHUNKS_DIRNAME = "chunks"

GLOSSARY_FILENAME = "glossary.json"

TERMS_LOG_FILENAME = "survey-terms.json"

SKILL_FILENAME = "SKILL.md"

ROLE = "survey_terms"

ENCODING = "utf-8"

TOKEN_ENCODING_NAME = "o200k_base"

SPLIT_ABOVE = 5000

MERGE_BELOW = 1500

WARNING_DETAIL_CHARS = 400

HEADING_COMMANDS: tuple[str, ...] = ("part", "chapter", "section", "subsection", "subsubsection", "paragraph")

APPENDIX_COMMANDS: tuple[str, ...] = ("appendix", "appendices")

APPENDIX_ENVIRONMENT = "appendices"

SPECIAL_RE = re.compile(r"[\\$]")

BEGIN_ABSTRACT_RE = re.compile(r"\\begin\s*\{" + masking.ABSTRACT_ENVIRONMENT + r"\}")

END_ABSTRACT_RE = re.compile(r"\\end\s*\{" + masking.ABSTRACT_ENVIRONMENT + r"\}")

TERMS_FIELD = "terms"

DO_NOT_TRANSLATE_FIELD = "do_not_translate"

PLURAL_SUFFIX_RE = re.compile(r"(?:es|s)$")

CJK_ASCII_BOUNDARY_RE = re.compile(r"(?<=[\u4e00-\u9fff])(?=[0-9A-Za-z])|(?<=[0-9A-Za-z])(?=[\u4e00-\u9fff])")

STYLE_FIELD = "style"

KNOWN_FIELDS: tuple[str, ...] = (TERMS_FIELD, DO_NOT_TRANSLATE_FIELD, STYLE_FIELD)

TERMS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"word": {"type": "string"}, "translation": {"type": "string"}},
                "required": ["word", "translation"],
                "additionalProperties": False,
            },
        },
        "do_not_translate": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["terms", "do_not_translate"],
    "additionalProperties": False,
}


class ChunkError(Exception):
    pass


class GlossaryError(Exception):
    pass


@dataclass(frozen=True)
class Term:
    word: str
    translation: str | None
    decided_by: DecidedBy


def run(
    paper_workdir: Workdir,
    *,
    glossary: tuple[Path, ...] = (),
    no_terms: bool = False,
    ask_model: str | None = None,
    ask_effort: str | None = None,
) -> SurveyManifest:
    paper_workdir.create()
    _reset_outputs(paper_workdir)
    manifest = _execute(paper_workdir, glossary, no_terms, ask_model, ask_effort)
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _execute(
    paper_workdir: Workdir,
    glossary_paths: Sequence[Path],
    no_terms: bool,
    ask_model: str | None,
    ask_effort: str | None,
) -> SurveyManifest:
    try:
        masked = (paper_workdir.build / MASKED_FILENAME).read_text(encoding=ENCODING)
        blocks = BlocksFile.model_validate_json((paper_workdir.build / BLOCKS_FILENAME).read_text(encoding=ENCODING))
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        return SurveyManifest(status=SurveyStatus.CHUNK_FAILED, message=describe_error(error))
    try:
        units = _read_layers(paper_workdir, glossary_paths)
    except GlossaryError as error:
        return SurveyManifest(status=SurveyStatus.GLOSSARY_INVALID, message=str(error))
    try:
        encoder = tiktoken.get_encoding(TOKEN_ENCODING_NAME)
    except Exception as error:
        return SurveyManifest(
            status=SurveyStatus.CHUNK_FAILED,
            message=(
                f"cannot load the tiktoken encoding {TOKEN_ENCODING_NAME} ({describe_error(error)}). "
                "The first use downloads the BPE file from the network; alternatively point "
                "TIKTOKEN_CACHE_DIR at a directory that already holds it."
            ),
        )
    try:
        document = _Document(masked, encoder)
        chunks = document.chunks()
        contents = [masked[chunk.start : chunk.end] for chunk in chunks]
        _verify(masked, chunks, contents)
    except (ChunkError, masking.MaskError) as error:
        return SurveyManifest(status=SurveyStatus.CHUNK_FAILED, message=describe_error(error))

    warnings: list[str] = []
    heading_tree = document.heading_tree()
    abstract = _abstract(blocks, masked)
    if abstract is None:
        warnings.append(
            "abstract not found: no abstract slot in blocks.json and no abstract environment "
            "in masked.tex; the brief carries abstract = null."
        )
    proposed, proposal_warnings = _propose(
        paper_workdir, abstract, heading_tree, masked, document.encoder, no_terms, ask_model, ask_effort
    )
    warnings.extend(proposal_warnings)

    merged, style, merge_warnings = _merge([(proposed, None), *units])
    warnings.extend(merge_warnings)
    merged, compound_warnings = _drop_broken_compounds(merged)
    warnings.extend(compound_warnings)
    decisions = [(term, _hits(term.word, masked)) for term in merged]
    kept = [term for term, hit in decisions if hit]
    filtered = [term for term, hit in decisions if not hit]
    if len(kept) + len(filtered) != len(merged):
        raise RuntimeError("the hit and miss lists do not exactly partition the merged terms; implementation bug")

    records = [
        _record(index, chunk, body, document) for index, (chunk, body) in enumerate(zip(chunks, contents, strict=True))
    ]
    warnings.extend(
        f"{record.id} has {record.tokens} tokens, over the split line {SPLIT_ABOVE}, "
        "with no finer cut point inside the unit."
        for record in records
        if record.tokens > SPLIT_ABOVE
    )
    brief = BriefFile(
        abstract=abstract,
        heading_tree=heading_tree,
        terms=[
            TermEntry(word=term.word, translation=term.translation, decided_by=term.decided_by)
            for term in kept
            if term.translation is not None
        ],
        do_not_translate=[
            DoNotTranslateEntry(word=term.word, decided_by=term.decided_by) for term in kept if term.translation is None
        ],
        style=style,
        chunks=records,
    )
    _write_outputs(paper_workdir, records, contents, brief)
    return SurveyManifest(
        status=SurveyStatus.OK,
        chunks_total=len(records),
        transparent_environments=sorted(document.transparent),
        terms_total=len(brief.terms),
        do_not_translate_total=len(brief.do_not_translate),
        filtered=[FilteredTerm(word=term.word, decided_by=term.decided_by) for term in filtered],
        warnings=warnings,
    )


def _write_outputs(
    paper_workdir: Workdir, records: Sequence[ChunkRecord], contents: Sequence[str], brief: BriefFile
) -> None:
    chunks_dir = paper_workdir.build / CHUNKS_DIRNAME
    chunks_dir.mkdir(parents=True, exist_ok=True)
    for record, body in zip(records, contents, strict=True):
        (chunks_dir / f"{record.id}.tex").write_text(body, encoding=ENCODING)
    (paper_workdir.build / BRIEF_FILENAME).write_text(brief.model_dump_json(indent=2) + "\n", encoding=ENCODING)


def _reset_outputs(paper_workdir: Workdir) -> None:
    (paper_workdir.build / BRIEF_FILENAME).unlink(missing_ok=True)
    shutil.rmtree(paper_workdir.build / CHUNKS_DIRNAME, ignore_errors=True)
    (paper_workdir.logs / TERMS_LOG_FILENAME).unlink(missing_ok=True)


def _record(index: int, chunk: _Chunk, body: str, document: _Document) -> ChunkRecord:
    return ChunkRecord(
        id=f"c{index:03d}",
        start=chunk.start,
        end=chunk.end,
        part=chunk.part,
        tokens=document.tokens(chunk.start, chunk.end),
        paragraphs=_paragraph_count(body),
        headings=[heading for offset, heading in document.headings if chunk.start <= offset < chunk.end],
        translatable_chars=sum(1 for character in masking.TOKEN_RE.sub("", body) if not character.isspace()),
    )


def _verify(masked: str, chunks: Sequence[_Chunk], contents: Sequence[str]) -> None:
    if not chunks:
        raise ChunkError(f"masked.tex has {len(masked)} characters but not a single chunk was produced")
    if "".join(contents) != masked:
        raise ChunkError("chunks concatenated in order do not equal masked.tex character for character; chunking bug")
    empty = [index for index, body in enumerate(contents) if _paragraph_count(body) < 1]
    if empty:
        starts = ", ".join(str(chunks[index].start) for index in empty)
        raise ChunkError(f"{len(empty)} chunks have no non-empty paragraph (start offsets {starts}); chunking bug")


def _paragraph_count(text: str) -> int:
    return sum(1 for paragraph in masking.BLANK_LINE_RE.split(text) if paragraph.strip())


def _abstract(blocks: BlocksFile, masked: str) -> str | None:
    for caption in blocks.captions:
        if caption.kind is masking.CaptionKind.ABSTRACT:
            text = caption.tex.strip()
            if text:
                return text
            break
    opening = BEGIN_ABSTRACT_RE.search(masked)
    if opening is None:
        return None
    closing = END_ABSTRACT_RE.search(masked, opening.end())
    if closing is None:
        return None
    return masked[opening.end() : closing.start()].strip() or None


def _read_layers(paper_workdir: Workdir, glossary_paths: Sequence[Path]) -> list[tuple[list[Term], str | None]]:
    layers: list[tuple[Path, DecidedBy, bool]] = [
        (config_dir() / GLOSSARY_FILENAME, DecidedBy.GLOBAL, False),
        (paper_workdir.path / GLOSSARY_FILENAME, DecidedBy.PAPER, False),
        *[(path, DecidedBy.CLI, True) for path in glossary_paths],
    ]
    units: list[tuple[list[Term], str | None]] = []
    for path, layer, required in layers:
        try:
            content = path.read_text(encoding=ENCODING)
        except FileNotFoundError as error:
            if required:
                raise GlossaryError(f"cannot read {path} ({describe_error(error)})") from error
            continue
        except (OSError, UnicodeDecodeError) as error:
            raise GlossaryError(f"cannot read {path} ({describe_error(error)})") from error
        units.append(_parse(content, str(path), layer))
    return units


def _parse(content: str, origin: str, layer: DecidedBy) -> tuple[list[Term], str | None]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise GlossaryError(f"{origin} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise GlossaryError(f"the top level of {origin} must be an object, got {type(data).__name__}")
    unknown = [key for key in data if key not in KNOWN_FIELDS]
    if unknown:
        raise GlossaryError(
            f"{origin} has an unknown field {unknown[0]!r}; a glossary file only accepts {', '.join(KNOWN_FIELDS)}"
        )
    terms: list[Term] = []
    seen: dict[str, Term] = {}
    for word in _parse_do_not_translate(data.get(DO_NOT_TRANSLATE_FIELD), origin):
        _add(terms, seen, Term(word=word, translation=None, decided_by=layer), origin)
    for word, translation in _parse_terms(data.get(TERMS_FIELD), origin):
        _add(terms, seen, Term(word=word, translation=translation, decided_by=layer), origin)
    return terms, _parse_style(data.get(STYLE_FIELD), origin)


def _parse_do_not_translate(value: object, origin: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise GlossaryError(
            f"{DO_NOT_TRANSLATE_FIELD} in {origin} must be a list of strings, got {type(value).__name__}"
        )
    words: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise GlossaryError(
                f"{DO_NOT_TRANSLATE_FIELD}[{index}] in {origin} must be a string, got {type(item).__name__}"
            )
        word = item.strip()
        if not word:
            raise GlossaryError(f"{DO_NOT_TRANSLATE_FIELD}[{index}] in {origin} is an empty word")
        words.append(word)
    return words


def _parse_terms(value: object, origin: str) -> list[tuple[str, str]]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise GlossaryError(
            f"{TERMS_FIELD} in {origin} must be an object mapping words to translations, got {type(value).__name__}"
        )
    pairs: list[tuple[str, str]] = []
    for raw_word, raw_translation in value.items():
        word = raw_word.strip()
        if not word:
            raise GlossaryError(f"{TERMS_FIELD} in {origin} contains an empty word")
        if not isinstance(raw_translation, str):
            raise GlossaryError(
                f"the translation of {TERMS_FIELD}[{word!r}] in {origin} must be a string, "
                f"got {type(raw_translation).__name__}"
            )
        translation = raw_translation.strip()
        if not translation:
            raise GlossaryError(
                f"the translation of {TERMS_FIELD}[{word!r}] in {origin} is empty; "
                f"to keep the original wording put the word into {DO_NOT_TRANSLATE_FIELD}"
            )
        pairs.append((word, translation))
    return pairs


def _parse_style(value: object, origin: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GlossaryError(
            f"{STYLE_FIELD} in {origin} must be a string (extra requirements addressed to the translator), "
            f"got {type(value).__name__}"
        )
    return value.strip()


def _add(terms: list[Term], seen: dict[str, Term], term: Term, origin: str) -> None:
    key = term.word.casefold()
    previous = seen.get(key)
    if previous is None:
        seen[key] = term
        terms.append(term)
        return
    if previous == term:
        return
    raise GlossaryError(
        f"{origin} gives two conflicting records for {term.word!r}"
        f" ({_describe_term(previous)}, {_describe_term(term)}); no precedence is applied within one file"
    )


def _describe_term(term: Term) -> str:
    if term.translation is None:
        return f"{term.word!r} in {DO_NOT_TRANSLATE_FIELD}"
    return f"{term.word!r} -> {term.translation!r} in {TERMS_FIELD}"


def _merge(units: Sequence[tuple[list[Term], str | None]]) -> tuple[list[Term], str | None, list[str]]:
    entries: dict[str, list[Term]] = {}
    warnings: list[str] = []
    style: str | None = None
    for terms, unit_style in units:
        layer: dict[str, list[Term]] = {}
        for term in terms:
            key = _term_key(term.word)
            bucket = layer.get(key)
            if bucket is None:
                layer[key] = [term]
                continue
            previous = bucket[-1]
            same = f"terms {previous.word!r} and {term.word!r} normalize to the same entry"
            if _from_user(term):
                bucket.append(term)
                warnings.append(
                    f"{same}, both from the {term.decided_by} layer: "
                    f"{_describe_term(previous)}, {_describe_term(term)}; both are kept, edit your glossary."
                )
                continue
            kept, dropped = (previous, term) if previous.translation is None else (term, previous)
            bucket[-1] = kept
            warnings.append(
                f"{same}, both from the {DecidedBy.SURVEY} layer; keeping {_describe_term(kept)}, "
                f"dropping {_describe_term(dropped)}."
            )
        entries.update(layer)
        if unit_style is not None:
            style = unit_style or None
    merged = [term for bucket in entries.values() for term in bucket]
    return sorted(merged, key=lambda term: (term.word.casefold(), term.word)), style, warnings


def _term_key(word: str) -> str:
    collapsed = " ".join(word.replace("-", " ").split()).casefold()
    return PLURAL_SUFFIX_RE.sub("", collapsed)


def _from_user(term: Term) -> bool:
    return term.decided_by is not DecidedBy.SURVEY


def _drop_broken_compounds(terms: Sequence[Term]) -> tuple[list[Term], list[str]]:
    protected = [(index, term) for index, term in enumerate(terms) if term.translation is None]
    dropped: set[int] = set()
    warnings: list[str] = []
    for index, term in enumerate(terms):
        if term.translation is None:
            continue
        for protected_index, word in protected:
            if protected_index in dropped or not _hits(word.word, term.word) or word.word in term.translation:
                continue
            if _term_key(word.word) == _term_key(term.word):
                continue
            conflict = (
                f"term {term.word!r} ({term.decided_by} layer) translates to {term.translation!r}, "
                f"which does not keep the {DO_NOT_TRANSLATE_FIELD} word {word.word!r} "
                f"({word.decided_by} layer) verbatim"
            )
            if _from_user(term) and _from_user(word):
                warnings.append(f"{conflict}; both come from your glossary, both are kept, edit your glossary.")
                continue
            if _from_user(word) or not _from_user(term):
                dropped.add(index)
                warnings.append(f"{conflict}; dropping this term, keeping the {DO_NOT_TRANSLATE_FIELD} word.")
                break
            dropped.add(protected_index)
            warnings.append(f"{conflict}; dropping this {DO_NOT_TRANSLATE_FIELD} word, keeping the term.")
    return [term for index, term in enumerate(terms) if index not in dropped], warnings


def _hits(word: str, text: str) -> bool:
    flags = re.NOFLAG if _abbreviation(word) else re.IGNORECASE
    body = r"\s+".join(re.escape(part) for part in word.split())
    return re.search(rf"(?<![0-9A-Za-z]){body}(?:es|s)?(?![0-9A-Za-z])", text, flags) is not None


def _abbreviation(word: str) -> bool:
    return any(character.isalpha() for character in word) and word == word.upper()


def _propose(
    paper_workdir: Workdir,
    abstract: str | None,
    heading_tree: Sequence[Heading],
    masked: str,
    encoder: tiktoken.Encoding,
    no_terms: bool,
    ask_model: str | None,
    ask_effort: str | None,
) -> tuple[list[Term], list[str]]:
    if no_terms:
        return [], []
    config, detail = load_config()
    if config is None:
        return [], [f"cannot read the model config; term proposal continues as empty ({detail[:WARNING_DETAIL_CHARS]})"]
    if ROLE not in config.roles:
        return [], []
    payload = _payload(abstract, heading_tree, masked)
    resolved, _detail = resolve_role(config, ROLE, RoleTable.PROVIDER, ask_model, ask_effort)
    if resolved is not None:
        thousands = len(encoder.encode(payload, disallowed_special=())) / 1000
        console.print(
            f"  {ROLE}: {resolved.provider}/{resolved.model}, "
            f"~{thousands:.1f}k tokens in, timeout {ASK_TIMEOUT_SECONDS}s"
        )
    started = time.monotonic()
    outcome = ask(
        role=ROLE,
        system=(asset_path("skill") / ROLE / SKILL_FILENAME).read_text(encoding=ENCODING),
        messages=[("user", payload)],
        schema=TERMS_SCHEMA,
        log_path=paper_workdir.logs / TERMS_LOG_FILENAME,
        model=ask_model,
        effort=ask_effort,
    )
    console.print(f"  {ROLE} returned {outcome.status} in {time.monotonic() - started:.1f}s")
    if outcome.status is AskStatus.ERROR:
        return [], [f"the term proposal call failed; continuing as empty ({outcome.detail[:WARNING_DETAIL_CHARS]})"]
    try:
        return _proposed_terms(outcome.text), []
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as error:
        return [], [
            f"the term proposal reply does not match the schema; continuing as empty ({describe_error(error)[:WARNING_DETAIL_CHARS]})"
        ]


def _proposed_terms(text: str) -> list[Term]:
    data = json.loads(text)
    proposed = [
        Term(
            word=item["word"].strip(),
            translation=CJK_ASCII_BOUNDARY_RE.sub(" ", item["translation"].strip()),
            decided_by=DecidedBy.SURVEY,
        )
        for item in data["terms"]
    ]
    proposed += [
        Term(word=word.strip(), translation=None, decided_by=DecidedBy.SURVEY) for word in data["do_not_translate"]
    ]
    return [term for term in proposed if term.word and term.translation != ""]


def _payload(abstract: str | None, heading_tree: Sequence[Heading], masked: str) -> str:
    tree = [f"{'  ' * (heading.depth - 1)}{heading.command}：{heading.argument}" for heading in heading_tree]
    return "\n".join(
        [
            "# 摘要",
            abstract or "（无摘要）",
            "",
            "# 标题树",
            *(tree or ["（无标题）"]),
            "",
            "# 全文（已掩码）",
            masked,
        ]
    )


@dataclass(frozen=True)
class _Chunk:
    start: int
    end: int
    part: Part


@dataclass(frozen=True)
class _Heading:
    start: int
    command: str
    argument: str
    depth: int


@dataclass(frozen=True)
class _Environment:
    name: str
    body_start: int
    body_end: int


@dataclass(frozen=True)
class _Scan:
    headings: tuple[_Heading, ...]
    environments: tuple[_Environment, ...]
    appendix_marks: tuple[int, ...]
    paragraph_starts: tuple[int, ...]


def _scan(text: str) -> _Scan:
    headings: list[_Heading] = []
    environments: list[_Environment] = []
    appendix_marks: list[int] = []
    stack: list[tuple[str, int]] = []
    position = 0
    while True:
        match = SPECIAL_RE.search(text, position)
        if match is None:
            break
        position = match.start()
        if text[position] == "$":
            position = masking.find_inline_dollar_close(text, position + 1)
            continue
        name, after_name = masking.read_control_sequence(text, position)
        if name == "verb":
            position = masking.skip_verb(text, after_name)
        elif name == "(":
            position = masking.skip_to_delimiter(text, after_name, "\\)", "\\(")
        elif name == "begin":
            environment, after = masking.read_environment_name(text, after_name)
            if environment is None:
                position = after_name
                continue
            if environment == APPENDIX_ENVIRONMENT:
                appendix_marks.append(position)
            stack.append((environment, after))
            position = after
        elif name == "end":
            environment, after = masking.read_environment_name(text, after_name)
            if environment is None:
                position = after_name
                continue
            if not stack:
                raise ChunkError(f"\\end{{{environment}}} at offset {position} has no matching \\begin")
            opened, body_start = stack.pop()
            if opened != environment:
                raise ChunkError(
                    f"\\end{{{environment}}} at offset {position} does not match the open \\begin{{{opened}}}"
                )
            environments.append(_Environment(name=environment, body_start=body_start, body_end=position))
            position = after
        elif name in HEADING_COMMANDS:
            heading, position = _read_heading(text, name, position, after_name, len(stack))
            headings.append(heading)
        elif name in APPENDIX_COMMANDS:
            appendix_marks.append(position)
            position = after_name
        else:
            position = after_name
    if stack:
        raise ChunkError(f"environments still open at end of file: {', '.join(name for name, _ in stack)}")
    return _Scan(
        headings=tuple(headings),
        environments=tuple(environments),
        appendix_marks=tuple(sorted(appendix_marks)),
        paragraph_starts=tuple(match.end() for match in masking.BLANK_LINE_RE.finditer(text)),
    )


def _read_heading(text: str, command: str, start: int, after_name: int, depth: int) -> tuple[_Heading, int]:
    cursor = after_name
    if text[cursor : cursor + 1] == "*":
        cursor += 1
    cursor = masking.skip_optional_arguments(text, cursor)
    if text[cursor : cursor + 1] != "{":
        return _Heading(start=start, command=command, argument="", depth=depth), cursor
    end = masking.match_group(text, cursor)
    return _Heading(start=start, command=command, argument=text[cursor + 1 : end - 1], depth=depth), end


class _Depth:
    def __init__(self, environments: Iterable[_Environment], transparent: frozenset[str]) -> None:
        points: list[tuple[int, int]] = []
        for environment in environments:
            if environment.name in transparent:
                continue
            points.append((environment.body_start, 1))
            points.append((environment.body_end, -1))
        points.sort()
        self._offsets = [offset for offset, _ in points]
        self._depths: list[int] = []
        total = 0
        for _, delta in points:
            total += delta
            self._depths.append(total)

    def at(self, offset: int) -> int:
        index = bisect_right(self._offsets, offset)
        return self._depths[index - 1] if index else 0


class _Document:
    def __init__(self, text: str, encoder: tiktoken.Encoding) -> None:
        self.text = text
        self.encoder = encoder
        self._token_counts: dict[tuple[int, int], int] = {}
        self.scan = _scan(text)
        self.command = _preferred_command(self.scan.headings)
        self.transparent = _transparent_environments(self.scan, self.command)
        self.depth = _Depth(self.scan.environments, self.transparent)
        self.headings = self._headings()

    def tokens(self, start: int, end: int) -> int:
        found = self._token_counts.get((start, end))
        if found is None:
            found = len(self.encoder.encode(self.text[start:end], disallowed_special=()))
            self._token_counts[(start, end)] = found
        return found

    def heading_tree(self) -> list[Heading]:
        return [heading for _offset, heading in self.headings]

    def _headings(self) -> list[tuple[int, Heading]]:
        top = [heading for heading in self.scan.headings if self.depth.at(heading.start) == 0]
        if not top:
            return []
        shallowest = min(HEADING_COMMANDS.index(heading.command) for heading in top)
        return [
            (
                heading.start,
                Heading(
                    command=heading.command,
                    argument=heading.argument,
                    depth=HEADING_COMMANDS.index(heading.command) - shallowest + 1,
                ),
            )
            for heading in top
        ]

    def chunks(self) -> list[_Chunk]:
        appendix_start = self._appendix_start() if self.command is not None else None
        pieces: list[_Chunk] = []
        for part, start, end in self._regions(appendix_start):
            for unit in self._region_units(start, end):
                pieces.extend(_Chunk(piece[0], piece[1], part) for piece in self._expand(unit, self.command))
        return self._merge_small(pieces)

    def _appendix_start(self) -> int | None:
        for offset in self.scan.appendix_marks:
            if self.depth.at(offset) == 0:
                return offset
        return None

    def _command_cuts(self) -> list[int]:
        return [
            heading.start
            for heading in self.scan.headings
            if heading.command == self.command and self.depth.at(heading.start) == 0
        ]

    def _regions(self, appendix_start: int | None) -> list[tuple[Part, int, int]]:
        length = len(self.text)
        if self.command is None:
            return [(Part.BODY, 0, length)] if length else []
        cuts = self._command_cuts()
        appendix = length if appendix_start is None else appendix_start
        body_start = min([*cuts, appendix])
        bounds = [(Part.FRONT, 0, body_start), (Part.BODY, body_start, appendix), (Part.APPENDIX, appendix, length)]
        return _merge_blank_regions(self.text, [(part, start, end) for part, start, end in bounds if start < end])

    def _region_units(self, start: int, end: int) -> list[tuple[int, int]]:
        if self.command is None:
            return _merge_blank(self.text, _split_at((start, end), self._paragraph_cuts(start, end)))
        return _split_at((start, end), [cut for cut in self._command_cuts() if start < cut < end])

    def _paragraph_cuts(self, start: int, end: int) -> list[int]:
        return [offset for offset in self.scan.paragraph_starts if start < offset < end and self.depth.at(offset) == 0]

    def _expand(self, unit: tuple[int, int], command: str | None) -> list[tuple[int, int]]:
        start, end = unit
        if self.tokens(start, end) <= SPLIT_ABOVE:
            return [unit]
        deeper_commands = HEADING_COMMANDS[HEADING_COMMANDS.index(command) + 1 :] if command is not None else ()
        for deeper in deeper_commands:
            cuts = [
                heading.start
                for heading in self.scan.headings
                if heading.command == deeper and start < heading.start < end and self.depth.at(heading.start) == 0
            ]
            if cuts:
                return [piece for sub in _split_at(unit, cuts) for piece in self._expand(sub, deeper)]
        cuts = self._paragraph_cuts(start, end)
        return _merge_blank(self.text, _split_at(unit, cuts)) if cuts else [unit]

    def _merge_small(self, chunks: Sequence[_Chunk]) -> list[_Chunk]:
        merged: list[_Chunk] = []
        for chunk in chunks:
            if (
                merged
                and self.tokens(merged[-1].start, merged[-1].end) < MERGE_BELOW
                and self._joinable(merged[-1], chunk)
            ):
                merged[-1] = _join(merged[-1], chunk)
            else:
                merged.append(chunk)
        index = len(merged) - 1
        while index >= 1:
            if self.tokens(merged[index].start, merged[index].end) < MERGE_BELOW and self._joinable(
                merged[index - 1], merged[index]
            ):
                merged[index - 1] = _join(merged[index - 1], merged[index])
                del merged[index]
            index -= 1
        return merged

    def _joinable(self, first: _Chunk, second: _Chunk) -> bool:
        return first.part is second.part and self.tokens(first.start, second.end) <= SPLIT_ABOVE


def _join(first: _Chunk, second: _Chunk) -> _Chunk:
    return _Chunk(start=first.start, end=second.end, part=first.part)


def _preferred_command(headings: Sequence[_Heading]) -> str | None:
    if not headings:
        return None
    top = [heading for heading in headings if heading.depth == 0]
    return min((heading.command for heading in top or headings), key=HEADING_COMMANDS.index)


def _transparent_environments(scan: _Scan, command: str | None) -> frozenset[str]:
    if command is None:
        return frozenset()
    starts = [heading.start for heading in scan.headings if heading.command == command]
    return frozenset(
        environment.name
        for environment in scan.environments
        if bisect_left(starts, environment.body_start) < bisect_left(starts, environment.body_end)
    )


def _split_at(unit: tuple[int, int], cuts: Sequence[int]) -> list[tuple[int, int]]:
    edges = [unit[0], *cuts, unit[1]]
    return [(edges[index], edges[index + 1]) for index in range(len(edges) - 1)]


def _merge_blank(text: str, units: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for unit in units:
        if merged and not text[unit[0] : unit[1]].strip():
            merged[-1] = (merged[-1][0], unit[1])
        else:
            merged.append(unit)
    if len(merged) > 1 and not text[merged[0][0] : merged[0][1]].strip():
        merged[1] = (merged[0][0], merged[1][1])
        del merged[0]
    return merged


def _merge_blank_regions(text: str, bounds: Sequence[tuple[Part, int, int]]) -> list[tuple[Part, int, int]]:
    merged: list[tuple[Part, int, int]] = []
    pending: int | None = None
    for part, start, end in bounds:
        if pending is not None:
            start, pending = pending, None
        if text[start:end].strip():
            merged.append((part, start, end))
        else:
            pending = start
    if pending is None:
        return merged
    if merged:
        part, start, _end = merged[-1]
        merged[-1] = (part, start, bounds[-1][2])
        return merged
    return [(bounds[0][0], pending, bounds[-1][2])]
