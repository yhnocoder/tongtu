from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path("~/.local/share/tongtu")

HOME_ENV = "TONGTU_HOME"

AREAS: tuple[str, ...] = ("src", "build", "out", "logs")

MANIFESTS_DIRNAME = "manifests"

ENCODING = "utf-8"


class WorkdirError(ValueError):
    pass


def normalize_arxiv_id(arxiv_id: str) -> str:
    raw = (arxiv_id or "").strip()
    if not raw:
        raise WorkdirError("empty arXiv id")
    if raw.startswith(("/", "~", ".")) or "\\" in raw or ".." in raw or any(ch.isspace() for ch in raw):
        raise WorkdirError(f"not a valid arXiv id: {arxiv_id!r}")
    return raw.replace("/", "_")


def default_root(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    home = (environ.get(HOME_ENV) or "").strip()
    if home:
        return Path(home).expanduser()
    return DEFAULT_ROOT.expanduser()


def resolve(
    arxiv_id: str | None = None,
    workdir: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    if workdir is not None:
        return Path(workdir).expanduser().absolute()
    if arxiv_id is None:
        raise WorkdirError("either an arXiv id or --workdir is required")
    return (default_root(env) / normalize_arxiv_id(arxiv_id)).absolute()


@dataclass(frozen=True)
class Workdir:
    path: Path

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
    def brief(self) -> Path:
        return self.build / "brief.json"

    @property
    def chunks(self) -> Path:
        return self.build / "chunks"

    @property
    def survey_terms_log(self) -> Path:
        return self.logs / "survey-terms.json"

    @property
    def translated(self) -> Path:
        return self.build / "translated"

    @property
    def reviewed(self) -> Path:
        return self.build / "reviewed"

    @property
    def review_log(self) -> Path:
        return self.logs / "review.jsonl"

    @property
    def zh_tex(self) -> Path:
        return self.build / "zh.tex"

    @property
    def zh_pdf(self) -> Path:
        return self.out / "zh.pdf"

    @property
    def compile_fix_log(self) -> Path:
        return self.logs / "compile-fix.jsonl"

    def create(self) -> None:
        for name in AREAS:
            (self.path / name).mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
