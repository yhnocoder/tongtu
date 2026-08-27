from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from .workdir import Workdir

STAGES: tuple[str, ...] = ("fetch", "precompile", "mask", "survey", "translate", "review", "compile")

STAGE_OUTPUTS: dict[str, tuple[Callable[[Workdir], Path], ...]] = {
    "fetch": (lambda w: w.src,),
    "precompile": (lambda w: w.precompile_tex, lambda w: w.fonts),
    "mask": (lambda w: w.masked, lambda w: w.blocks),
    "survey": (lambda w: w.brief, lambda w: w.chunks),
    "translate": (lambda w: w.translated,),
    "review": (lambda w: w.reviewed,),
    "compile": (lambda w: w.zh_tex, lambda w: w.zh_pdf),
}

STAGE_REMOVES: dict[str, tuple[Callable[[Workdir], Path] | str, ...]] = {
    "fetch": (lambda w: w.src, lambda w: w.eprint),
    "precompile": (
        lambda w: w.sandbox("precompile"),
        lambda w: w.precompile_tex,
        lambda w: w.fonts,
        lambda w: w.precompile_fix_log,
    ),
    "mask": (lambda w: w.masked, lambda w: w.blocks),
    "survey": (lambda w: w.brief, lambda w: w.chunks, lambda w: w.survey_terms_log),
    "translate": (lambda w: w.translated, "logs/translate-*.json"),
    "review": (lambda w: w.sandbox("review"), lambda w: w.reviewed, lambda w: w.review_log),
    "compile": (lambda w: w.zh_tex, lambda w: w.sandbox("compile"), lambda w: w.zh_pdf, lambda w: w.compile_fix_log),
}


def outputs_present(workdir: Workdir, stage: str) -> bool:
    return all(_present(output(workdir)) for output in STAGE_OUTPUTS[stage])


def _present(path: Path) -> bool:
    if path.is_dir():
        return any(path.iterdir())
    return path.is_file()


def first_pending(workdir: Workdir) -> str | None:
    for name in STAGES:
        if not outputs_present(workdir, name):
            return name
    return None


def downstream(stage: str) -> tuple[str, ...]:
    return STAGES[STAGES.index(stage) :]


def clean(workdir: Workdir, stage: str) -> None:
    for entry in STAGE_REMOVES[stage]:
        if isinstance(entry, str):
            for path in sorted(workdir.path.glob(entry)):
                _remove(path)
        else:
            _remove(entry(workdir))
    _remove(workdir.manifest_path(stage))


def clean_from(workdir: Workdir, stage: str) -> None:
    for name in downstream(stage):
        clean(workdir, name)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
