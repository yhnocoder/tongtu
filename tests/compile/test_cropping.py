from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from pypdf import PdfReader

from tongtu import cropping, fonts
from tongtu.model.config import FontsConfig

pytestmark = pytest.mark.compile

LINKED_PAPER = (
    "\\documentclass{article}\n\\usepackage[UTF8]{ctex}\n\\usepackage{hyperref}\n\\begin{document}\n"
    "\\section{引言}\\label{intro}\n第\\ref{intro}节见 \\url{https://arxiv.org} 与文献 \\cite{x}。"
    "这一段写得足够长，让正文排满整个版心的宽度，裁边之后左右两侧都应当只剩下很窄的白边。\n"
    "\\newpage\n\\section{方法}\n第二页的正文。\n"
    "\\begin{thebibliography}{1}\\bibitem{x} 一篇文献。\\end{thebibliography}\n\\end{document}\n"
)


def test_cropping_a_xelatex_pdf_keeps_links_and_outline(tmp_path: Path) -> None:
    (tmp_path / "paper.tex").write_text(LINKED_PAPER, encoding="utf-8")
    for _ in range(2):
        subprocess.run(
            ["xelatex", "-interaction=nonstopmode", "-halt-on-error", "paper.tex"],
            cwd=tmp_path,
            env=fonts.environment(FontsConfig(), os.environ),
            capture_output=True,
            timeout=120,
            check=True,
        )
    pdf = tmp_path / "paper.pdf"
    before = PdfReader(pdf)
    annotations = [len(page["/Annots"].get_object()) if "/Annots" in page else 0 for page in before.pages]
    outline = len(before.outline)
    destinations = sorted(before.named_destinations)

    assert cropping.crop_margins(pdf) == []

    after = PdfReader(pdf)
    assert len(after.pages) == len(before.pages)
    assert [len(page["/Annots"].get_object()) if "/Annots" in page else 0 for page in after.pages] == annotations
    assert sum(annotations) > 0
    assert len(after.outline) == outline > 0
    assert sorted(after.named_destinations) == destinations
    for page in after.pages:
        assert float(page.cropbox.width) < float(page.mediabox.width)
        assert float(page.cropbox.height) < float(page.mediabox.height)
    assert cropping.crop_margins(pdf) == []
    assert [list(page.cropbox) for page in PdfReader(pdf).pages] == [list(page.cropbox) for page in after.pages]


def test_landscape_pages_are_left_uncropped(tmp_path: Path) -> None:
    source = (
        "\\documentclass{article}\n\\usepackage{pdflscape}\n\\begin{document}\nPortrait page.\n"
        "\\begin{landscape}\nA landscape page with a wide table.\n\\end{landscape}\n\\end{document}\n"
    )
    (tmp_path / "paper.tex").write_text(source, encoding="utf-8")
    subprocess.run(
        ["xelatex", "-interaction=nonstopmode", "-halt-on-error", "paper.tex"],
        cwd=tmp_path,
        capture_output=True,
        timeout=120,
        check=True,
    )
    pdf = tmp_path / "paper.pdf"
    assert [page.rotation for page in PdfReader(pdf).pages] == [0, 90]
    assert cropping.crop_margins(pdf) == []
    portrait, landscape = PdfReader(pdf).pages
    assert float(portrait.cropbox.width) < float(portrait.mediabox.width)
    assert list(landscape.cropbox) == list(landscape.mediabox)
