from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from . import notes
from .artifacts.common import Manifest, NoteSession
from .artifacts.fetch import FetchManifest, FetchRoute
from .manifests import load_manifest
from .workdir import Workdir, raw_log_path

STAGES: tuple[str, ...] = ("fetch", "reconstruct", "precompile", "mask", "translate", "compile")

RECONSTRUCT_STAGE = "reconstruct"


def fetched_route(workdir: Workdir) -> FetchRoute:
    fetched = load_manifest(workdir.manifest_path(STAGES[0]), FetchManifest)
    return fetched.route if fetched is not None else "tex"


def implied_complete(workdir: Workdir, stage: str) -> bool:
    if stage != RECONSTRUCT_STAGE or workdir.manifest_path(stage).exists():
        return False
    fetched = load_manifest(workdir.manifest_path(STAGES[0]), FetchManifest)
    return fetched is not None and fetched.ok and fetched.route == "tex"


def _rebuilt(workdir: Workdir) -> bool:
    return fetched_route(workdir) == "pdf"


STAGE_OUTPUTS: dict[str, tuple[Callable[[Workdir], Path], ...]] = {
    "fetch": (lambda w: w.original_pdf if _rebuilt(w) else w.src,),
    "reconstruct": (lambda w: w.src / "main.tex" if _rebuilt(w) else w.src,),
    "precompile": (lambda w: w.precompile_tex, lambda w: w.precompile_pdf),
    "mask": (lambda w: w.masked, lambda w: w.blocks),
    "translate": (lambda w: w.translated,),
    "compile": (lambda w: w.zh_tex, lambda w: w.zh_pdf),
}

STAGE_REMOVES: dict[str, tuple[Callable[[Workdir], Path | None], ...]] = {
    "fetch": (lambda w: w.src, lambda w: w.eprint, lambda w: w.original_pdf, lambda w: w.meta),
    "reconstruct": (
        lambda w: w.sandbox("reconstruct"),
        lambda w: w.reconstruct_fix_log,
        lambda w: raw_log_path(w.reconstruct_fix_log),
        lambda w: w.src if _rebuilt(w) else None,
    ),
    "precompile": (
        lambda w: w.sandbox("tex"),
        lambda w: w.precompile_tex,
        lambda w: w.precompile_pdf,
        lambda w: w.precompile_fix_log,
        lambda w: raw_log_path(w.precompile_fix_log),
    ),
    "mask": (lambda w: w.masked, lambda w: w.blocks),
    "translate": (
        lambda w: w.sandbox("translate"),
        lambda w: w.translated,
        lambda w: w.paper_glossary,
        lambda w: w.translate_log,
        lambda w: raw_log_path(w.translate_log),
    ),
    "compile": (
        lambda w: w.zh_tex,
        lambda w: w.zh_pdf,
        lambda w: w.zh_anchors,
        lambda w: w.zh_synctex,
        lambda w: w.compile_fix_log,
        lambda w: raw_log_path(w.compile_fix_log),
    ),
}

STAGE_SESSIONS: dict[str, NoteSession] = {
    "reconstruct": "reconstruct_fix",
    "precompile": "precompile_fix",
    "translate": "translate",
    "compile": "compile_fix",
}


def outputs_present(workdir: Workdir, stage: str) -> bool:
    return all(_present(output(workdir)) for output in STAGE_OUTPUTS[stage])


def _present(path: Path) -> bool:
    if path.is_dir():
        return any(path.iterdir())
    return path.is_file()


def completed(workdir: Workdir, stage: str) -> bool:
    if implied_complete(workdir, stage):
        return True
    manifest = load_manifest(workdir.manifest_path(stage), Manifest)
    return manifest is not None and manifest.ok


def first_pending(workdir: Workdir) -> str | None:
    for name in STAGES:
        if not completed(workdir, name):
            return name
    return None


def downstream(stage: str) -> tuple[str, ...]:
    return STAGES[STAGES.index(stage) :]


def clean(workdir: Workdir, stage: str) -> None:
    for entry in STAGE_REMOVES[stage]:
        _remove(entry(workdir))
    session = STAGE_SESSIONS.get(stage)
    if session is not None:
        notes.remove(workdir.notes, session)
    _remove(workdir.manifest_path(stage))


def clean_from(workdir: Workdir, stage: str) -> None:
    for name in downstream(stage):
        clean(workdir, name)


def _remove(path: Path | None) -> None:
    if path is None:
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
