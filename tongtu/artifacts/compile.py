from __future__ import annotations

from enum import StrEnum

from .common import CompileReport, FixSession, Manifest


class CompileStatus(StrEnum):
    OK = "ok"
    COMPILE_FAILED = "compile_failed"


class CompileManifest(Manifest):
    status: CompileStatus
    report: CompileReport | None = None
    baseline: CompileReport | None = None
    fix_session: FixSession | None = None
