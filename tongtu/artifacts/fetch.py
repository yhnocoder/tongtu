from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

FetchKind = Literal["tar.gz", "tar", "gz", "tex", "pdf", "local"]


class FetchStatus(StrEnum):
    OK = "ok"
    PDF_ONLY = "pdf_only"
    EMPTY = "empty"
    DOWNLOAD_FAILED = "download_failed"
    UNPACK_FAILED = "unpack_failed"
    SOURCE_MISSING = "source_missing"


class FetchManifest(BaseModel):
    status: FetchStatus
    source: str
    kind: FetchKind | None = None
    url: str = ""
    payload_bytes: int = 0
    tex_files: list[str] = []
    tex_chars: int = 0
    rejected: list[str] = []
    warnings: list[str] = []
    message: str = ""
