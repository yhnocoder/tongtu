from __future__ import annotations

from pathlib import Path

import pymupdf

from ..artifacts.reconstruct import FloatingLine, RegionsFile
from ..manifests import write_manifest
from .crops import Cropper
from .latex import LatexWriter
from .layout import Layout

MAIN_FILENAME = "main.tex"

REGIONS_FILENAME = "regions.json"


def rebuild(pdf: Path, out: Path) -> RegionsFile:
    pymupdf.TOOLS.set_small_glyph_heights(True)
    try:
        doc = pymupdf.open(pdf)
        layout = Layout(doc)
        layout.analyze()
        writer = LatexWriter(layout, Cropper(doc, out, layout.heavy))
        (out / MAIN_FILENAME).write_text(writer.document(), encoding="utf-8")
        regions = RegionsFile(
            regions=writer.cropper.regions,
            floating=[
                FloatingLine(page=page + 1, rect=tuple(round(value, 2) for value in rect), kind=kind)
                for page, rect, kind in writer.floating()
            ],
        )
    finally:
        pymupdf.TOOLS.set_small_glyph_heights(False)
    write_manifest(out / REGIONS_FILENAME, regions)
    return regions
