from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, PositiveInt

from .fetch import PaperMeta

ABSENT_STATUS = "absent"


class StageState(BaseModel):
    name: str
    status: str
    outputs_present: bool
    message: str = ""
    warnings: list[str] = []
    manifest: Path | None = None


class RunCommand(StrEnum):
    RUN = "run"
    STAGE = "stage"


class RunMarker(BaseModel):
    pid: PositiveInt
    started_at: datetime
    command: RunCommand
    mode: str


class PaperStatus(BaseModel):
    workdir: Path
    arxiv_id: str | None = None
    version: int | None = None
    running: bool
    pid: int | None = None
    started_at: datetime | None = None
    mode: str | None = None
    stages: list[StageState]
    pdf: Path | None = None
    meta: Path | None = None
    glossary: Path | None = None
    notes: Path | None = None


class PaperState(StrEnum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class PaperEntry(BaseModel):
    name: str
    state: PaperState
    stage: str | None = None
    status: PaperStatus
    info: PaperMeta | None = None
    added_at: datetime
    updated_at: datetime
    pages: int | None = None
    source_pages: int | None = None
    translate_seconds: float | None = None
    translate_cost_usd: float | None = None
    recent_actions: list[str] = []


class PaperListing(BaseModel):
    papers: list[PaperEntry]
