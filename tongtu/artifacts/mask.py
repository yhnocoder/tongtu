from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from ..masking import BlockCategory, CaptionKind, DecidedBy, EnvironmentClass
from .common import Manifest


class MaskStatus(StrEnum):
    OK = "ok"
    MASK_FAILED = "mask_failed"


class EnvironmentDecisionRecord(BaseModel):
    classification: EnvironmentClass
    category: str = ""
    decided_by: DecidedBy
    occurrences: int = 0
    blocks: int = 0


class MaskManifest(Manifest):
    status: MaskStatus
    environments: dict[str, EnvironmentDecisionRecord] = {}
    blocks_total: int = 0
    captions_total: int = 0
    precompile_chars: int = 0
    masked_chars: int = 0
    masked_chars_ratio: float = 0.0


class BlockRecord(BaseModel):
    id: str
    category: BlockCategory
    environment: str = ""
    decided_by: str = ""
    labels: list[str] = []
    tex: str
    start: int
    end: int
    line: int


class CaptionRecord(BaseModel):
    id: str
    block_id: str
    kind: CaptionKind
    tex: str
    masked_text: str


class BlocksFile(BaseModel):
    blocks: list[BlockRecord] = []
    captions: list[CaptionRecord] = []
