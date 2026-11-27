from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import RootModel, ValidationError

from ..manifests import describe_error
from .crops import crop_path

TRANSCRIPTIONS_FILENAME = "rec.json"

HINTS_FILENAME = "hints.json"

IMAGES_DIRNAME = "img"

SKIP = "SKIP"

VERIFY_WORKERS = 4

FONTSPEC_PACKAGE = r"\usepackage{fontspec}"

RECOGNITION_PACKAGES = (
    r"\usepackage{mathtools,mathrsfs,upgreek}\usepackage[table]{xcolor}"
    r"\usepackage{booktabs,multirow,makecell,array,pifont}"
)


class Transcriptions(RootModel[dict[str, str]]):
    pass


def read_transcriptions(session_dir: Path, session: str) -> tuple[dict[str, str], str]:
    path = session_dir / TRANSCRIPTIONS_FILENAME
    if not path.is_file():
        return {}, f"the {session} session wrote no {TRANSCRIPTIONS_FILENAME}; every crop stays an image"
    try:
        return Transcriptions.model_validate_json(path.read_text(encoding="utf-8")).root, ""
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        return {}, (
            f"cannot read {TRANSCRIPTIONS_FILENAME} written by the {session} session ({describe_error(error)}); "
            "every crop stays an image"
        )


def copy_crops(tree: Path, session_dir: Path, keys: Sequence[str]) -> None:
    for key in keys:
        target = session_dir / crop_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tree / crop_path(key), target)


def image_path(session_dir: Path, key: str) -> Path:
    return session_dir / IMAGES_DIRNAME / f"{key}.png"


def verify_each[VerdictT](keys: Sequence[str], verify: Callable[[str], VerdictT]) -> dict[str, VerdictT]:
    with ThreadPoolExecutor(VERIFY_WORKERS) as pool:
        return dict(zip(keys, pool.map(verify, keys), strict=True))


def with_packages(tex: str) -> str:
    if RECOGNITION_PACKAGES in tex:
        return tex
    return tex.replace(FONTSPEC_PACKAGE, RECOGNITION_PACKAGES + FONTSPEC_PACKAGE, 1)
