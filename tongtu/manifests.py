from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ValidationError


def load_manifest[ManifestT: BaseModel](path: Path, model_cls: type[ManifestT]) -> ManifestT | None:
    try:
        return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


def write_manifest(path: Path, manifest: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")


def describe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def records_sha256(hashes: Iterable[str]) -> str:
    return hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()
