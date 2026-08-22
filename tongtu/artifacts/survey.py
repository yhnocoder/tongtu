from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class SurveyStatus(StrEnum):
    OK = "ok"
    GLOSSARY_INVALID = "glossary_invalid"
    CHUNK_FAILED = "chunk_failed"


class DecidedBy(StrEnum):
    SURVEY = "survey"
    GLOBAL = "global"
    PAPER = "paper"
    CLI = "cli"


class Part(StrEnum):
    FRONT = "front"
    BODY = "body"
    APPENDIX = "appendix"


class Heading(BaseModel):
    command: str
    argument: str
    depth: int


class TermEntry(BaseModel):
    word: str
    translation: str
    decided_by: DecidedBy


class DoNotTranslateEntry(BaseModel):
    word: str
    decided_by: DecidedBy


class FilteredTerm(BaseModel):
    word: str
    decided_by: DecidedBy


class ChunkRecord(BaseModel):
    id: str
    start: int
    end: int
    part: Part
    tokens: int
    paragraphs: int
    headings: list[Heading] = []
    translatable_chars: int


class BriefFile(BaseModel):
    abstract: str | None = None
    heading_tree: list[Heading] = []
    terms: list[TermEntry] = []
    do_not_translate: list[DoNotTranslateEntry] = []
    style: str | None = None
    chunks: list[ChunkRecord] = []


class SurveyManifest(BaseModel):
    status: SurveyStatus
    chunks_total: int = 0
    transparent_environments: list[str] = []
    terms_total: int = 0
    do_not_translate_total: int = 0
    filtered: list[FilteredTerm] = []
    warnings: list[str] = []
    message: str = ""
