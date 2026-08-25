from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path("~/.local/share/tongtu")

HOME_ENV = "TONGTU_HOME"

AREAS: tuple[str, ...] = ("src", "build", "out", "logs")

MANIFESTS_DIRNAME = "manifests"


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

    def create(self) -> None:
        for name in AREAS:
            (self.path / name).mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
