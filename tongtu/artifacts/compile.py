from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from .common import CompileReport, FixSession, Manifest


class CompileStatus(StrEnum):
    OK = "ok"
    COMPILE_FAILED = "compile_failed"


class CompileManifest(Manifest):
    status: CompileStatus
    report: CompileReport | None = None
    baseline: CompileReport | None = None
    fix_session: FixSession | None = None


class AnchorKind(StrEnum):
    TEXT = "text"
    HEADING = "heading"
    CAPTION = "caption"
    ABSTRACT = "abstract"
    BIBITEM = "bibitem"
    MATH = "math"
    TABLE = "table"
    FIGURE = "figure"
    TIKZ = "tikz"
    CODE = "code"
    ALGORITHM = "algorithm"
    BIBLIOGRAPHY = "bibliography"
    BOX = "box"
    UNKNOWN = "unknown"
    METADATA = "metadata"


class AnchorRect(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class AnchorPage(BaseModel):
    page: int
    width: float
    height: float
    body: AnchorRect | None = None


class AnchorBlock(BaseModel):
    id: str
    kind: AnchorKind
    parent: str | None = None
    first_line: int
    last_line: int
    rects: list[AnchorRect] = []


class AnchorsFile(BaseModel):
    pdf: str
    tex: str
    pages: list[AnchorPage] = []
    blocks: list[AnchorBlock] = []
