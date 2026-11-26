from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class Manifest(BaseModel):
    status: str
    warnings: list[str] = []
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class CompileReport(BaseModel):
    pages: int
    pdf_bytes: int
    overfull_hboxes: int
    undefined_references: int
    undefined_citations: int
    missing_characters: int
    duration_seconds: float


class Usage(BaseModel):
    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float | None = None
    thinking_tokens: int | None = None


class StopReason(StrEnum):
    FINISHED = "finished"
    TIMEOUT = "timeout"
    ERROR = "error"


class Session(BaseModel):
    stop_reason: StopReason
    model: str
    effort: str | None = None
    duration_seconds: float
    usage: Usage | None = None


NoteSession = Literal["translate", "precompile_fix", "compile_fix"]


class Note(BaseModel):
    session: NoteSession
    where: str
    problem: str
    resolution: str
    suggestion: str


class NotesFile(BaseModel):
    notes: list[Note] = []
