from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from .common import Manifest


class TranslateStatus(StrEnum):
    OK = "ok"
    TRANSLATE_FAILED = "translate_failed"


class ChunkTranslateStatus(StrEnum):
    TRANSLATED = "translated"
    FALLBACK = "fallback"
    SKIPPED = "skipped"


class ChunkTranslateRecord(BaseModel):
    status: ChunkTranslateStatus
    attempts: int
    failures: list[str] = []


class TranslateManifest(Manifest):
    status: TranslateStatus
    model: str = ""
    effort: str = ""
    prompt_version: str = ""
    jobs: int = 0
    chunks: dict[str, ChunkTranslateRecord] = {}
