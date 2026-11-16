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


class ChunkCounts(BaseModel):
    total: int
    done: int


class RunCommand(StrEnum):
    RUN = "run"
    STAGE = "stage"


class RunMarker(BaseModel):
    pid: PositiveInt
    started_at: datetime
    command: RunCommand


class PaperStatus(BaseModel):
    workdir: Path
    arxiv_id: str | None = None
    running: bool
    pid: int | None = None
    stages: list[StageState]
    translate_chunks: ChunkCounts | None = None
    pdf: Path | None = None
