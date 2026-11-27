from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from ..artifacts.reconstruct import Region, RegionKind
from ..manifests import describe_error
from .layout import TEXT_FLAGS, Box

CROPS_DIRNAME = "crops"

LONG_STRING_CHARS = 30000

SQRT = "√"

SYMBOL_FONT_RE = re.compile(r"cmsy|lmsy|msy|txsy|pxsy|sy\d|symbol", re.I)

GlyphPair = tuple[Box, Box]


class CropError(Exception):
    pass


def crop_path(name: str) -> str:
    return f"{CROPS_DIRNAME}/{name}.pdf"


def open_crop(path: Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path)
    except (RuntimeError, ValueError, OSError) as error:
        raise CropError(f"cannot open the crop {path.name} ({describe_error(error)})") from error
    if len(doc) == 0:
        raise CropError(f"the crop {path.name} has no page")
    return doc


def ink_box(char: dict, span: dict) -> Box:
    if char["c"] == SQRT and SYMBOL_FONT_RE.search(span["font"]):
        x0, _, x1, _ = char["bbox"]
        y = char["origin"][1]
        return (x0, y - 0.05 * span["size"], x1, y + 0.95 * span["size"])
    return tuple(char["bbox"])


class Cropper:
    def __init__(self, doc: pymupdf.Document, out: Path, heavy: set[int]) -> None:
        self.doc = doc
        self.out = out
        self.heavy = heavy
        self.regions: list[Region] = []
        self.glyph_pairs: dict[int, list[list[GlyphPair]]] = {}
        (out / CROPS_DIRNAME).mkdir(parents=True, exist_ok=True)

    def crop(self, page_number: int, rect: pymupdf.Rect, prefix: str, kind: RegionKind, hint: str = "") -> str:
        name = f"{prefix}-{len(self.regions) + 1:04d}"
        rect = self.save(page_number, rect, self.out / crop_path(name))
        self.regions.append(
            Region(id=name, kind=kind, page=page_number + 1, rect=tuple(round(value, 2) for value in rect), hint=hint)
        )
        return name

    def save(self, page_number: int, rect: pymupdf.Rect, path: Path) -> pymupdf.Rect:
        rect = pymupdf.Rect(rect) & self.doc[page_number].rect
        if page_number in self.heavy:
            out = pymupdf.open()
            page = out.new_page(width=rect.width, height=rect.height)
            page.show_pdf_page(page.rect, self.doc, page_number, clip=rect)
            out.save(path, garbage=3, deflate=True)
        else:
            self.redacted_page(page_number, rect).save(path, garbage=3, deflate=True)
        return rect

    def redacted_page(self, page_number: int, rect: pymupdf.Rect) -> pymupdf.Document:
        out = pymupdf.open()
        out.insert_pdf(self.doc, from_page=page_number, to_page=page_number)
        page = out[0]
        width, height = page.rect.width, page.rect.height
        outside = [
            pymupdf.Rect(0, 0, width, rect.y0),
            pymupdf.Rect(0, rect.y1, width, height),
            pymupdf.Rect(0, rect.y0, rect.x0, rect.y1),
            pymupdf.Rect(rect.x1, rect.y0, width, rect.y1),
        ]
        for area in outside:
            if area.width > 0 and area.height > 0:
                page.add_redact_annot(area)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_NONE,
        )
        text_redactions = 0
        for area in self.outside_text(page_number, rect):
            if area.width > 0.05 and area.height > 0.05:
                page.add_redact_annot(area)
                text_redactions += 1
        if text_redactions:
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
        page.set_cropbox(rect)
        for xref in range(1, out.xref_length()):
            try:
                keys = out.xref_get_keys(xref)
            except Exception:
                continue
            for key in keys:
                kind, value = out.xref_get_key(xref, key)
                if kind == "string" and len(value) > LONG_STRING_CHARS:
                    out.xref_set_key(xref, key, "()")
        return out

    def outside_text(self, page_number: int, rect: pymupdf.Rect) -> list[pymupdf.Rect]:
        inside_big: list[pymupdf.Rect] = []
        segments: list[pymupdf.Rect] = []
        for line in self.page_glyphs(page_number):
            segment = None
            for small, big in line:
                middle = pymupdf.Point((small[0] + small[2]) / 2, (small[1] + small[3]) / 2)
                if rect.contains(middle):
                    inside_big.append(pymupdf.Rect(big))
                    if segment is not None:
                        segments.append(segment)
                        segment = None
                else:
                    segment = pymupdf.Rect(small) if segment is None else segment | pymupdf.Rect(small)
            if segment is not None:
                segments.append(segment)
        return [self.trim(segment, inside_big) for segment in segments]

    def trim(self, segment: pymupdf.Rect, inside_big: list[pymupdf.Rect]) -> pymupdf.Rect:
        area = pymupdf.Rect(
            segment.x0, segment.y0 + 0.25 * segment.height, segment.x1, segment.y1 - 0.25 * segment.height
        )
        middle = pymupdf.Point((area.x0 + area.x1) / 2, (area.y0 + area.y1) / 2)
        for big in inside_big:
            if not area.intersects(big):
                continue
            if middle.y < big.y0:
                area.y1 = min(area.y1, big.y0 - 0.01)
            elif middle.y > big.y1:
                area.y0 = max(area.y0, big.y1 + 0.01)
            elif area.x1 <= big.x1 and area.x0 < big.x0:
                area.x1 = min(area.x1, big.x0 - 0.01)
            elif area.x0 >= big.x0 and area.x1 > big.x1:
                area.x0 = max(area.x0, big.x1 + 0.01)
            else:
                return pymupdf.Rect()
        return area

    def page_glyphs(self, page_number: int) -> list[list[GlyphPair]]:
        if page_number not in self.glyph_pairs:
            boxes = []
            for small in (True, False):
                pymupdf.TOOLS.set_small_glyph_heights(small)
                raw = self.doc[page_number].get_text("rawdict", flags=TEXT_FLAGS)
                boxes.append(
                    [
                        [
                            ink_box(char, span) if small else tuple(char["bbox"])
                            for span in line["spans"]
                            for char in span["chars"]
                            if not char["c"].isspace()
                        ]
                        for block in raw["blocks"]
                        if block["type"] == 0
                        for line in block["lines"]
                    ]
                )
            pymupdf.TOOLS.set_small_glyph_heights(True)
            self.glyph_pairs[page_number] = [
                list(zip(small, big, strict=False)) for small, big in zip(*boxes, strict=False)
            ]
        return self.glyph_pairs[page_number]
