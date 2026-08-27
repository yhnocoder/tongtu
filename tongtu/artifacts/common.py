from __future__ import annotations

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


class FixSession(BaseModel):
    stop_reason: Literal["finished", "timeout", "error"]
    model: str
    duration_seconds: float
