from __future__ import annotations

from pathlib import Path

import pytest

from tongtu.reconstruct import packing
from tongtu.reconstruct.crops import CropError
from tongtu.reconstruct.latex import INLINE_CROP_MACRO

SOURCE = (
    "\\documentclass{article}\n"
    + INLINE_CROP_MACRO
    + "\n\\begin{document}\n"
    + "\\includegraphics[width=3cm]{crops/r-0001.pdf}\n"
    + "text \\im{m-0002}{1.5bp}{x} and \\im{m-0002}{1.5bp}{x}\n"
    + "\\includegraphics{crops/e-0003}\n"
    + "\\end{document}\n"
)


def test_referenced_crops_lists_each_crop_once_in_order() -> None:
    assert packing.referenced_crops(SOURCE) == ["r-0001", "m-0002", "e-0003"]


def test_packed_source_points_every_crop_at_its_page() -> None:
    packed = packing.packed_source(SOURCE, {"r-0001": 1, "m-0002": 2, "e-0003": 3})
    assert "{\\includegraphics[page=1,width=3cm]{crops.pdf}}" in packed
    assert "{\\includegraphics[page=3]{crops.pdf}}" in packed
    assert "crops/" not in packed
    assert packing.PACKED_INLINE_MACRO in packed
    assert INLINE_CROP_MACRO not in packed
    assert "\\expandafter\\def\\csname crop@e-0003\\endcsname{3}\n\\begin{document}" in packed
    assert packed.count("\\im{m-0002}{1.5bp}{x}") == 2


def test_missing_crops_names_crops_without_a_file(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "crops").mkdir()
    (tmp_path / "crops" / "m-0002.pdf").write_bytes(b"%PDF-1.5")
    assert packing.missing_crops(tmp_path) == ["r-0001", "e-0003"]


def test_pack_of_an_unreadable_crop_names_the_crop(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "crops").mkdir()
    for name in ("r-0001", "m-0002", "e-0003"):
        (tmp_path / "crops" / f"{name}.pdf").write_bytes(b"not a pdf")
    with pytest.raises(CropError, match="cannot open the crop r-0001.pdf"):
        packing.pack(tmp_path, tmp_path / "packed")
