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
