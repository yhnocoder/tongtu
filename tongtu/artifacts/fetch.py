from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from .common import Manifest

FetchKind = Literal["tar.gz", "tar", "gz", "tex", "pdf", "local", "local_pdf"]

FetchRoute = Literal["tex", "pdf"]


class FetchStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    DOWNLOAD_FAILED = "download_failed"
    UNPACK_FAILED = "unpack_failed"
    SOURCE_MISSING = "source_missing"


class FetchManifest(Manifest):
    status: FetchStatus
    source: str
    arxiv_id: str | None = None
    version: int | None = None
    kind: FetchKind | None = None
    route: FetchRoute = "tex"
    url: str = ""
    payload_bytes: int = 0
    tex_files: list[str] = []
    tex_chars: int = 0
    rejected: list[str] = []


class PaperMeta(BaseModel):
    arxiv_id: str
    version: int
    title: str
    authors: list[str]
    abstract: str
    primary_category: str
    categories: list[str]
    published: datetime
    updated: datetime
    comment: str | None
    journal_ref: str | None
    doi: str | None
    abs_url: str
    pdf_url: str
    abstract_zh: str | None = None


class CachedMeta(BaseModel):
    fetched_at: AwareDatetime
    meta: PaperMeta
