from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from .common import CompileReport, FixSession


class CompileStatus(StrEnum):
    OK = "ok"
    COMPILE_FAILED = "compile_failed"


class CompileManifest(BaseModel):
    status: CompileStatus
    report: CompileReport | None = None
    baseline: CompileReport | None = None
    fix_session: FixSession | None = None
    warnings: list[str] = []
    message: str = ""
