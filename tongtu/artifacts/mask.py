from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .common import Manifest


class MaskStatus(StrEnum):
    OK = "ok"
    MASK_FAILED = "mask_failed"


class EnvironmentClass(StrEnum):
    TEXT = "text"
    NON_TRANSLATABLE = "non_translatable"


class BlockCategory(StrEnum):
    MATH = "math"
    TABLE = "table"
    FIGURE = "figure"
    TIKZ = "tikz"
    CODE = "code"
    ALGORITHM = "algorithm"
    BIBLIOGRAPHY = "bibliography"
    BOX = "box"
    UNKNOWN = "unknown"
    PREAMBLE = "preamble"
    POSTAMBLE = "postamble"
    COMMENT = "comment"
    METADATA = "metadata"


class DecisionSource(StrEnum):
    NEWTHEOREM = "newtheorem"
    NEWENVIRONMENT = "newenvironment"
    TABLE = "table"
    DEFAULT = "default"


class CaptionKind(StrEnum):
    CAPTION = "caption"
    ABSTRACT = "abstract"


class EnvironmentDecisionRecord(BaseModel):
    classification: EnvironmentClass
    category: BlockCategory | None = None
    decided_by: DecisionSource
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
    model_config = ConfigDict(frozen=True)

    id: str
    category: BlockCategory
    environment: str = ""
    decided_by: DecisionSource | None = None
    labels: list[str] = []
    tex: str
    start: int
    end: int
    line: int


class CaptionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    block_id: str
    kind: CaptionKind
    tex: str
    masked_text: str


class BlocksFile(BaseModel):
    blocks: list[BlockRecord] = []
    captions: list[CaptionRecord] = []
