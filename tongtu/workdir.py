from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import papers_dir

AREAS: tuple[str, ...] = ("src", "build", "out", "logs")

MANIFESTS_DIRNAME = "manifests"

RUN_MARKER_FILENAME = "running.json"

LAST_RUN_FILENAME = "last_run.json"

ENCODING = "utf-8"

LOG_SUFFIX = ".jsonl"

RAW_LOG_SUFFIX = ".raw.jsonl"


class WorkdirError(ValueError):
    pass


def normalize_arxiv_id(arxiv_id: str) -> str:
    raw = (arxiv_id or "").strip()
    if not raw:
        raise WorkdirError("empty arXiv id")
    if raw.startswith(("/", "~", ".")) or "\\" in raw or ".." in raw or any(ch.isspace() for ch in raw):
        raise WorkdirError(f"not a valid arXiv id: {arxiv_id!r}")
    return raw.replace("/", "_")


def resolve(
    arxiv_id: str | None = None,
    workdir: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    if workdir is not None:
        return Path(workdir).expanduser().absolute()
    if arxiv_id is None:
        raise WorkdirError("either an arXiv id or --workdir is required")
    return (papers_dir(env) / normalize_arxiv_id(arxiv_id)).absolute()


def raw_log_path(log_path: Path) -> Path:
    return log_path.with_name(log_path.name.removesuffix(LOG_SUFFIX) + RAW_LOG_SUFFIX)


@dataclass(frozen=True)
class Workdir:
    path: Path

    @property
    def run_marker(self) -> Path:
        return self.path / RUN_MARKER_FILENAME

    @property
    def last_run(self) -> Path:
        return self.path / LAST_RUN_FILENAME

    @property
    def src(self) -> Path:
        return self.path / "src"

    @property
    def build(self) -> Path:
        return self.path / "build"

    @property
    def out(self) -> Path:
        return self.path / "out"

    @property
    def logs(self) -> Path:
        return self.path / "logs"

    @property
    def manifests(self) -> Path:
        return self.build / MANIFESTS_DIRNAME

    def manifest_path(self, stage: str) -> Path:
        return self.manifests / f"{stage}.json"

    def sandbox(self, stage: str) -> Path:
        return self.build / "sandbox" / stage

    @property
    def eprint(self) -> Path:
        return self.build / "e-print.bin"

    @property
    def original_pdf(self) -> Path:
        return self.build / "original.pdf"

    @property
    def reconstruct_formula_log(self) -> Path:
        return self.logs / "reconstruct-formula.jsonl"

    @property
    def reconstruct_table_log(self) -> Path:
        return self.logs / "reconstruct-table.jsonl"

    @property
    def reconstruct_fix_log(self) -> Path:
        return self.logs / "reconstruct-fix.jsonl"

    @property
    def precompile_tex(self) -> Path:
        return self.build / "precompile.tex"

    @property
    def precompile_pdf(self) -> Path:
        return self.build / "precompile.pdf"

    @property
    def precompile_fix_log(self) -> Path:
        return self.logs / "precompile-fix.jsonl"

    @property
    def masked(self) -> Path:
        return self.build / "masked.tex"

    @property
    def blocks(self) -> Path:
        return self.build / "blocks.json"

    @property
    def translated(self) -> Path:
        return self.build / "translated.tex"

    @property
    def translate_log(self) -> Path:
        return self.logs / "translate.jsonl"

    @property
    def zh_tex(self) -> Path:
        return self.build / "zh.tex"

    @property
    def zh_pdf(self) -> Path:
        return self.out / f"{self.path.name}.zh.pdf"

    @property
    def zh_anchors(self) -> Path:
        return self.out / f"{self.path.name}.zh.anchors.json"

    @property
    def meta(self) -> Path:
        return self.out / f"{self.path.name}.meta.json"

    @property
    def zh_synctex(self) -> Path:
        return self.build / "zh.synctex.gz"

    @property
    def paper_glossary(self) -> Path:
        return self.out / f"{self.path.name}.glossary.json"

    @property
    def notes(self) -> Path:
        return self.out / f"{self.path.name}.notes.json"

    @property
    def compile_fix_log(self) -> Path:
        return self.logs / "compile-fix.jsonl"

    def create(self) -> None:
        for name in AREAS:
            (self.path / name).mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
