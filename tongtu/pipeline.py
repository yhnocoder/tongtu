from __future__ import annotations

import shutil
from pathlib import Path

from .workdir import Workdir

STAGES: tuple[str, ...] = ("fetch", "precompile", "mask", "survey", "translate", "review", "compile")

STAGE_OUTPUTS: dict[str, tuple[str, ...]] = {
    "fetch": ("src",),
    "precompile": ("build/precompile.tex", "build/sandbox/precompile/flat.pdf"),
    "mask": ("build/masked.tex", "build/blocks.json"),
    "survey": ("build/brief.json", "build/chunks"),
    "translate": ("build/translated",),
    "review": ("build/reviewed",),
    "compile": ("build/zh.tex", "out/zh.pdf"),
}

STAGE_REMOVES: dict[str, tuple[str, ...]] = {
    "fetch": ("src", "build/e-print.bin"),
    "precompile": ("build/sandbox/precompile", "build/precompile.tex", "logs/precompile-fix.jsonl"),
    "mask": ("build/masked.tex", "build/blocks.json"),
    "survey": ("build/brief.json", "build/chunks", "logs/survey-terms.json"),
    "translate": ("build/translated", "logs/translate-*.json"),
    "review": ("build/sandbox/review", "build/reviewed", "logs/review.jsonl"),
    "compile": ("build/zh.tex", "build/sandbox/compile", "out/zh.pdf", "logs/compile-fix.jsonl"),
}


def outputs_present(workdir: Workdir, stage: str) -> bool:
    return all(_present(workdir.path / relative) for relative in STAGE_OUTPUTS[stage])


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


def clean_from(workdir: Workdir, stage: str) -> None:
    for name in downstream(stage):
        for pattern in STAGE_REMOVES[name]:
            for path in sorted(workdir.path.glob(pattern)):
                _remove(path)
        _remove(workdir.manifest_path(name))


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
