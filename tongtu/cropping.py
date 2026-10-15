from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfWriter
from pypdf.errors import PyPdfError
from pypdf.generic import RectangleObject

from . import processes
from .manifests import describe_error

GHOSTSCRIPT_BBOX_COMMAND: tuple[str, ...] = ("gs", "-q", "-dSAFER", "-dNOPAUSE", "-dBATCH", "-sDEVICE=bbox")

GHOSTSCRIPT_TIMEOUT_SECONDS = 120

CROP_PADDING_POINTS = 12.0

BOUNDING_BOX_RE = re.compile(r"^%%HiResBoundingBox: (\S+) (\S+) (\S+) (\S+)$", re.MULTILINE)


@dataclass(frozen=True)
class Box:
    left: float
    bottom: float
    right: float
    top: float

    @property
    def empty(self) -> bool:
        return self.right <= self.left or self.top <= self.bottom

    def union(self, other: Box) -> Box:
        return Box(
            min(self.left, other.left),
            min(self.bottom, other.bottom),
            max(self.right, other.right),
            max(self.top, other.top),
        )


def crop_margins(pdf: Path) -> list[str]:
    try:
        content = _content_box(pdf)
    except OSError as error:
        return [f"cannot run Ghostscript to measure the page content ({describe_error(error)}); the PDF keeps its margins"]
    if isinstance(content, str):
        return [f"{content}; the PDF keeps its margins"]
    try:
        _write_crop_box(pdf, content)
    except (OSError, ValueError, PyPdfError) as error:
        return [f"cannot write the crop box into {pdf.name} ({describe_error(error)}); the PDF keeps its margins"]
    return []


def _content_box(pdf: Path) -> Box | str:
    outcome = processes.run_in_process_group(
        [*GHOSTSCRIPT_BBOX_COMMAND, pdf.name], pdf.parent, GHOSTSCRIPT_TIMEOUT_SECONDS
    )
    if outcome.timed_out:
        return f"Ghostscript hit the {GHOSTSCRIPT_TIMEOUT_SECONDS}s timeout while measuring the page content"
    if outcome.returncode != 0:
        return f"Ghostscript exited with code {outcome.returncode} while measuring the page content"
    boxes = [
        Box(*(float(value) for value in match.groups())) for match in BOUNDING_BOX_RE.finditer(outcome.stderr_text)
    ]
    filled = [box for box in boxes if not box.empty]
    if not filled:
        return "Ghostscript reported no page content"
    union = filled[0]
    for box in filled[1:]:
        union = union.union(box)
    return union


def _write_crop_box(pdf: Path, content: Box) -> None:
    writer = PdfWriter(pdf, incremental=True)
    for page in writer.pages:
        media = page.mediabox
        crop = Box(
            max(media.left, media.left + content.left - CROP_PADDING_POINTS),
            max(media.bottom, media.bottom + content.bottom - CROP_PADDING_POINTS),
            min(media.right, media.left + content.right + CROP_PADDING_POINTS),
            min(media.top, media.bottom + content.top + CROP_PADDING_POINTS),
        )
        page.cropbox = RectangleObject((crop.left, crop.bottom, crop.right, crop.top))
    partial = pdf.with_name(pdf.name + ".part")
    try:
        with partial.open("wb") as stream:
            writer.write(stream)
        partial.replace(pdf)
    finally:
        partial.unlink(missing_ok=True)
