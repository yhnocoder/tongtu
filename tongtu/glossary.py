from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .artifacts.translate import PaperGlossary, PaperKeptWord, PaperTerm
from .config import GLOSSARY_FILENAME, glossary_path, home_dir
from .manifests import describe_error
from .validation import (
    DO_NOT_TRANSLATE_FIELD,
    STYLE_FIELD,
    TERMS_FIELD,
    Glossary,
    GlossaryFormatError,
    hits,
    kept_word_conflicts,
    merge_glossaries,
    parse_glossary,
    searchable,
    term_key,
)
from .workdir import ENCODING

LEARNED_FILENAME = "glossary.learned.json"

LEARNED_LOCK_FILENAME = "glossary.learned.lock"

FIXED_FILENAME = "glossary.fixed.json"

SESSION_FILENAME = GLOSSARY_FILENAME

READ_ONLY_MODE = 0o444


class GlossaryError(Exception):
    pass


@dataclass(frozen=True)
class Layer:
    path: Path
    glossary: Glossary


@dataclass(frozen=True)
class Layers:
    learned: Layer | None
    user: tuple[Layer, ...]


@dataclass(frozen=True)
class Selection:
    fixed: Glossary
    learned: Glossary
    unmatched: tuple[str, ...]

    @property
    def user_count(self) -> int:
        return len(self.fixed.entries())

    @property
    def learned_count(self) -> int:
        return len(self.learned.entries())


def learned_path(env: Mapping[str, str] | None = None) -> Path:
    return home_dir(env) / LEARNED_FILENAME


def read_layers(paper_dir: Path, cli_paths: Sequence[Path], env: Mapping[str, str] | None = None) -> Layers:
    optional = [_read_layer(path, required=False) for path in (glossary_path(env), paper_dir / GLOSSARY_FILENAME)]
    required = [_read_layer(path, required=True) for path in cli_paths]
    return Layers(
        learned=_read_layer(learned_path(env), required=False),
        user=tuple(layer for layer in (*optional, *required) if layer is not None),
    )


def select(layers: Layers, masked: str) -> Selection:
    text = searchable(masked)
    user = merge_glossaries([layer.glossary for layer in layers.user])
    user_keys = {term_key(word) for word, _translation in user.entries()}
    unmatched = tuple(
        f"{_origin(layers.user, word)}: {word!r} does not occur in this paper"
        for word, _translation in user.entries()
        if not hits(word, text)
    )
    fixed = _matching(user, text)
    conflicts = kept_word_conflicts(dict(fixed.terms), fixed.do_not_translate)
    if conflicts:
        listed = "; ".join(
            f"term {word!r} -> {translation!r} ({_origin(layers.user, word)}) does not keep the "
            f"{DO_NOT_TRANSLATE_FIELD} word {kept!r} ({_origin(layers.user, kept)}) verbatim"
            for word, translation, kept in conflicts
        )
        raise GlossaryError(f"the translation of a term must keep the do-not-translate words it contains: {listed}")
    learned = Glossary() if layers.learned is None else layers.learned.glossary
    unshadowed = Glossary(
        terms=tuple(entry for entry in learned.terms if term_key(entry[0]) not in user_keys),
        do_not_translate=tuple(word for word in learned.do_not_translate if term_key(word) not in user_keys),
    )
    return Selection(fixed=fixed, learned=_matching(unshadowed, text), unmatched=unmatched)


def load_selection(
    masked: str, paper_dir: Path, cli_paths: Sequence[Path], env: Mapping[str, str] | None = None
) -> Selection:
    return select(read_layers(paper_dir, cli_paths, env), masked)


def write_session_files(session_dir: Path, selection: Selection) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    fixed = session_dir / FIXED_FILENAME
    fixed.unlink(missing_ok=True)
    fixed.write_text(render(selection.fixed), encoding=ENCODING)
    fixed.chmod(READ_ONLY_MODE)
    session = session_dir / SESSION_FILENAME
    if not session.exists():
        session.write_text(render(selection.learned), encoding=ENCODING)


def read_session_glossary(session_dir: Path) -> Glossary:
    layer = _read_layer(session_dir / SESSION_FILENAME, required=False)
    return Glossary() if layer is None else layer.glossary


def validation_glossary(selection: Selection, session: Glossary) -> Glossary:
    return merge_glossaries([session, selection.fixed])


def paper_glossary(selection: Selection, session: Glossary) -> PaperGlossary:
    fixed_keys = {term_key(word) for word, _translation in selection.fixed.entries()}
    initial = set(selection.learned.entries())
    terms = [
        PaperTerm(word=word, translation=translation, source="user") for word, translation in selection.fixed.terms
    ]
    kept = [PaperKeptWord(word=word, source="user") for word in selection.fixed.do_not_translate]
    for entry in session.entries():
        word, translation = entry
        if term_key(word) in fixed_keys:
            continue
        source: Literal["learned", "new"] = "learned" if entry in initial else "new"
        if translation is None:
            kept.append(PaperKeptWord(word=word, source=source))
        else:
            terms.append(PaperTerm(word=word, translation=translation, source=source))
    return PaperGlossary(terms=terms, do_not_translate=kept)


def grow(learned: Glossary, selection: Selection, session: Glossary) -> tuple[Glossary, int]:
    known = {term_key(word) for word, _translation in (*learned.entries(), *selection.fixed.entries())}
    added = [entry for entry in session.entries() if term_key(entry[0]) not in known]
    grown = Glossary(
        terms=(*learned.terms, *((word, translation) for word, translation in added if translation is not None)),
        do_not_translate=(*learned.do_not_translate, *(word for word, translation in added if translation is None)),
        style=learned.style,
    )
    return grown, len(added)


def publish(session_dir: Path, selection: Selection, out_path: Path, env: Mapping[str, str] | None = None) -> int:
    session = read_session_glossary(session_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(paper_glossary(selection, session).model_dump_json(indent=2) + "\n", encoding=ENCODING)
    path = learned_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / LEARNED_LOCK_FILENAME).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        layer = _read_layer(path, required=False)
        grown, added = grow(Glossary() if layer is None else layer.glossary, selection, session)
        if added:
            _replace_text(path, render(grown))
    return added


def _replace_text(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding=ENCODING) as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def render(glossary: Glossary) -> str:
    content: dict[str, object] = {
        TERMS_FIELD: dict(glossary.terms),
        DO_NOT_TRANSLATE_FIELD: list(glossary.do_not_translate),
    }
    if glossary.style is not None:
        content[STYLE_FIELD] = glossary.style
    return json.dumps(content, ensure_ascii=False, indent=2) + "\n"


def _read_layer(path: Path, *, required: bool) -> Layer | None:
    try:
        content = path.read_text(encoding=ENCODING)
    except FileNotFoundError as error:
        if required:
            raise GlossaryError(f"cannot read {path} ({describe_error(error)})") from error
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise GlossaryError(f"cannot read {path} ({describe_error(error)})") from error
    try:
        return Layer(path=path, glossary=parse_glossary(content, str(path)))
    except GlossaryFormatError as error:
        raise GlossaryError(str(error)) from error


def _matching(glossary: Glossary, text: str) -> Glossary:
    return Glossary(
        terms=tuple(entry for entry in glossary.terms if hits(entry[0], text)),
        do_not_translate=tuple(word for word in glossary.do_not_translate if hits(word, text)),
        style=glossary.style,
    )


def _origin(layers: Sequence[Layer], word: str) -> Path:
    key = term_key(word)
    return next(
        layer.path
        for layer in reversed(layers)
        if any(term_key(entry_word) == key for entry_word, _translation in layer.glossary.entries())
    )
