from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from .common import Manifest, Session


class TranslateStatus(StrEnum):
    OK = "ok"
    GLOSSARY_INVALID = "glossary_invalid"
    TRANSLATE_FAILED = "translate_failed"


class GlossaryCounts(BaseModel):
    user: int = 0
    learned: int = 0
    added: int = 0


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
    mode: str
    session: Session | None = None
    paragraphs_total: int = 0
    paragraphs_translated: int = 0
    problems: list[str] = []
    glossary: GlossaryCounts = GlossaryCounts()
