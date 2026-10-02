from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

HOME_ENV = "TONGTU_HOME"

DEFAULT_HOME = Path("~/.tongtu")

DEV_HOME = Path("~/.tongtu-dev")

CONFIG_FILENAME = "config.toml"

GLOSSARY_FILENAME = "glossary.json"

PAPERS_DIRNAME = "papers"

LEGACY_DIRS: tuple[Path, ...] = (Path("~/.config/tongtu"), Path("~/.local/share/tongtu"))


def home_dir(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    home = (environ.get(HOME_ENV) or "").strip()
    if home:
        return Path(home).expanduser()
    return DEFAULT_HOME.expanduser()


def config_path(env: Mapping[str, str] | None = None) -> Path:
    return home_dir(env) / CONFIG_FILENAME


def glossary_path(env: Mapping[str, str] | None = None) -> Path:
    return home_dir(env) / GLOSSARY_FILENAME


def papers_dir(env: Mapping[str, str] | None = None) -> Path:
    return home_dir(env) / PAPERS_DIRNAME


def legacy_dirs_present() -> list[Path]:
    return [path.expanduser() for path in LEGACY_DIRS if path.expanduser().exists()]
