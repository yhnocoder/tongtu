from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .assets import asset_path
from .model.config import FontsConfig, load_config

FONTS_DIR = asset_path("fonts")

SEARCH_VARIABLES: tuple[str, ...] = ("TTFONTS", "OPENTYPEFONTS")

FONT_FILE_SUFFIXES: tuple[str, ...] = (".ttf", ".otf", ".ttc")


def configured() -> FontsConfig:
    config, _ = load_config()
    if config is None:
        return FontsConfig()
    return config.fonts


def search_directories(fonts: FontsConfig) -> list[Path]:
    directories = [FONTS_DIR]
    for value in (fonts.main, fonts.bold, fonts.sans, fonts.sans_bold, fonts.mono):
        path = Path(value).expanduser() if value else None
        if path is not None and len(path.parts) > 1:
            directories.append(path.parent)
    return list(dict.fromkeys(directory for directory in directories if directory.is_dir()))


def environment(fonts: FontsConfig, base: Mapping[str, str]) -> dict[str, str]:
    search_path = ":".join(f"{directory}//" for directory in search_directories(fonts))
    merged = dict(base)
    for variable in SEARCH_VARIABLES:
        merged[variable] = f"{search_path}:{base.get(variable, '')}"
    return merged
