from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from .common import FixSession


class ReviewStatus(StrEnum):
    OK = "ok"
    REVIEW_FAILED = "review_failed"


class ReviewManifest(BaseModel):
    status: ReviewStatus
    session: FixSession | None = None
    changed: list[str] = []
    reverted: list[str] = []
    warnings: list[str] = []
    message: str = ""
