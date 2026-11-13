from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

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


class PaperStatus(BaseModel):
    workdir: Path
    arxiv_id: str | None = None
    stages: list[StageState]
    translate_chunks: ChunkCounts | None = None
    pdf: Path | None = None
