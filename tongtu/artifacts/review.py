from __future__ import annotations

from enum import StrEnum

from .common import FixSession, Manifest


class ReviewStatus(StrEnum):
    OK = "ok"
    REVIEW_FAILED = "review_failed"


class ReviewManifest(Manifest):
    status: ReviewStatus
    session: FixSession | None = None
    changed: list[str] = []
    reverted: list[str] = []
