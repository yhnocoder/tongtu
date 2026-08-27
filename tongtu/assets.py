from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

PACKAGED_DIRNAME = "data"


def asset_path(name: str) -> Path:
    packaged = PACKAGE_DIR / PACKAGED_DIRNAME / name
    if packaged.exists():
        return packaged
    return PACKAGE_DIR.parent / name
