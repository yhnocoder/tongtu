from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, PositiveInt

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


class RunRecord(BaseModel):
    started_at: datetime
    command: RunCommand
    mode: str


class PaperState(StrEnum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class PaperStatus(BaseModel):
    workdir: Path
    arxiv_id: str | None = None
    version: int | None = None
    running: bool
    pid: int | None = None
    started_at: datetime | None = None
    mode: str | None = None
    state: PaperState
    stage: str | None = None
    stages: list[StageState]
    pdf: Path | None = None
    meta: Path | None = None
    glossary: Path | None = None
    notes: Path | None = None
    pages: int | None = None
    source_pages: int | None = None
    translate_seconds: float | None = None
    translate_cost_usd: float | None = None
    recent_actions: list[str] = []


class PaperSummary(BaseModel):
    name: str
    workdir: Path
    state: PaperState
    stage: str | None = None
    arxiv_id: str | None = None
    version: int | None = None
    title: str | None = None
    primary_category: str | None = None
    added_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    pdf: Path | None = None


class PaperListing(BaseModel):
    papers: list[PaperSummary]
