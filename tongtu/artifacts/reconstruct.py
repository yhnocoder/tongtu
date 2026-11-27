from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from .common import Manifest, Session

Rect = tuple[float, float, float, float]


class ReconstructStatus(StrEnum):
    OK = "ok"
    NO_TEXT_LAYER = "no_text_layer"
    RECONSTRUCT_FAILED = "reconstruct_failed"


class RegionKind(StrEnum):
    FIGURE = "figure"
    TABLE = "table"
    DISPLAY = "display"
    INLINE = "inline"
    ALGORITHM = "algorithm"
    REFERENCES = "references"
    AUTHORS = "authors"


class Region(BaseModel):
    id: str
    kind: RegionKind
    page: int
    rect: Rect
    hint: str = ""


class FloatingLine(BaseModel):
    page: int
    rect: Rect
    kind: str


class RegionsFile(BaseModel):
    regions: list[Region] = []
    floating: list[FloatingLine] = []


class CheckReport(BaseModel):
    source_pages: int
    pages: int
    recall: float
    precision: float
    missing_characters: int
    order: float
    passed: bool


class RecognitionReport(BaseModel):
    total: int
    passed: int
    session: Session | None = None


class ReconstructManifest(Manifest):
    status: ReconstructStatus
    rebuilt: bool = False
    regions: dict[str, int] = {}
    rule_check: CheckReport | None = None
    final_check: CheckReport | None = None
    crop_lost_glyphs: int = 0
    fix_session: Session | None = None
    formulas: RecognitionReport | None = None
    tables: RecognitionReport | None = None
