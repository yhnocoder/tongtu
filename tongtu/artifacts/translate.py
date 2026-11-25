from __future__ import annotations

from enum import StrEnum
from typing import Literal

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


class PaperTerm(BaseModel):
    word: str
    translation: str
    source: Literal["user", "learned", "new"]


class PaperKeptWord(BaseModel):
    word: str
    source: Literal["user", "learned", "new"]


class PaperGlossary(BaseModel):
    terms: list[PaperTerm]
    do_not_translate: list[PaperKeptWord]


class TranslateManifest(Manifest):
    status: TranslateStatus
    model: str = ""
    effort: str = ""
    prompt_version: str = ""
    jobs: int = 0
    chunks: dict[str, ChunkTranslateRecord] = {}
