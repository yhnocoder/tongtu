from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pymupdf

from ..artifacts.reconstruct import CheckReport
from .checks import CheckDetail
from .crops import Cropper, crop_path

RENDER_ZOOM = 1.4

RENDER_GAP = 20

CROP_TEXT_CHARS = 300

COMPARISON_NAME = "cmp-{page}.png"

LOST_EXAMPLES = 8


def render_comparison(original: Path, rebuilt: Path, page: int, out: Path) -> Path | None:
    pixmaps = [
        doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM))
        for doc in (pymupdf.open(original), pymupdf.open(rebuilt))
        if 0 < page <= len(doc)
    ]
    if not pixmaps:
        return None
    width = sum(pixmap.width for pixmap in pixmaps) + RENDER_GAP
    height = max(pixmap.height for pixmap in pixmaps)
    canvas = pymupdf.open()
    sheet = canvas.new_page(width=width, height=height)
    x = 0
    for pixmap in pixmaps:
        sheet.insert_image(pymupdf.Rect(x, 0, x + pixmap.width, pixmap.height), pixmap=pixmap)
        x += pixmap.width + RENDER_GAP
    path = out / COMPARISON_NAME.format(page=page)
    sheet.get_pixmap().save(path)
    return path


def crop_original(original: Path, tree: Path, page: int, rect: pymupdf.Rect, name: str) -> str:
    doc = pymupdf.open(original)
    path = crop_path(name)
    saved = Cropper(doc, tree, set()).save(page - 1, rect, tree / path)
    pymupdf.TOOLS.set_small_glyph_heights(False)
    words = [word[4] for word in doc[page - 1].get_text("words", clip=rect)]
    return f"{path}  {saved.width:.1f}x{saved.height:.1f}bp  text inside: {' '.join(words)[:CROP_TEXT_CHARS]}"


def summary_lines(detail: CheckDetail, lost: Sequence[tuple[str, str]]) -> list[str]:
    report: CheckReport = detail.report
    verdict = "PASS" if report.passed else "FAIL"
    return [
        f"pages {report.source_pages} -> {report.pages}  recall {report.recall:.4f}  precision {report.precision:.4f}"
        f"  missing_characters {report.missing_characters}  order {report.order:.4f}  {verdict}",
        f"missing {detail.missing}",
        f"extra {detail.extra}",
        *(f"out of order {run}" for run in detail.out_of_order),
        f"inline crops lacking hint glyphs {len(lost)} {list(lost[:LOST_EXAMPLES])}",
    ]
