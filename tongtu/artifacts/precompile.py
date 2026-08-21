from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from .common import CompileReport, FixSession


class PrecompileStatus(StrEnum):
    OK = "ok"
    MAIN_NOT_FOUND = "main_not_found"
    MAIN_AMBIGUOUS = "main_ambiguous"
    EXPAND_FAILED = "expand_failed"
    COMPILE_FAILED = "compile_failed"


class PrecompileManifest(BaseModel):
    status: PrecompileStatus
    main_file: str = ""
    report: CompileReport | None = None
    fix_session: FixSession | None = None
    warnings: list[str] = []
    message: str = ""
