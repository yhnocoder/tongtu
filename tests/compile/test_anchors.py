from __future__ import annotations

import gzip
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfReader, Transformation

from tongtu import anchors, compiling, masking, synctex
from tongtu.artifacts.compile import AnchorBlock, AnchorKind, AnchorRect

pytestmark = pytest.mark.compile

DOCUMENT = r"""\documentclass{article}
\usepackage{graphicx}
\pagestyle{empty}
\setlength{\textwidth}{400pt}
\begin{document}
A short paragraph before the scaled table.

\begin{table}[h]
\begin{center}
\resizebox{0.5\textwidth}{!}{%
\begin{tabular}{|l|l|l|}
\hline
alpha & beta & gamma \\
delta & epsilon & zeta \\
\hline
\end{tabular}}
\end{center}
\end{table}
\clearpage
A short paragraph before the turned table.

\begin{table}[h]
\begin{center}
\rotatebox{90}{%
\begin{tabular}{|l|l|l|}
\hline
alpha & beta & gamma \\
delta & epsilon & zeta \\
\hline
\end{tabular}}
\end{center}
\end{table}
\end{document}
"""

RULE_TOLERANCE = 1.0

SCALED_WIDTH = 200 * synctex.PDF_POINTS_PER_INCH / synctex.TEX_POINTS_PER_INCH

PATH_POINTS = frozenset({b"m", b"l"})


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))


def rule_extent(pdf: Path, page: int) -> AnchorRect:
    points: list[tuple[float, float]] = []

    def visit(operator: bytes, operands: list[float], cm: list[float], tm: list[float]) -> None:
        if operator in PATH_POINTS:
            x, y = (float(value) for value in operands)
            points.append(Transformation(tuple(cm)).apply_on((x, y)))

    PdfReader(pdf).pages[page - 1].extract_text(visitor_operand_before=visit)
    assert points
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return AnchorRect(page=page, x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))


def test_scaled_and_turned_tables_match_their_rules(
    tmp_path: Path, environments_table: Mapping[str, masking.TableEntry]
) -> None:
    tree = tmp_path / "paper"
    tree.mkdir()
    (tree / "main.tex").write_text(DOCUMENT, encoding="utf-8")
    attempt = compiling.attempt_compile(tree, "main.tex")
    assert attempt.passed, compiling.failure_message(attempt)
    outcome = masking.mask_document(DOCUMENT, environments_table)
    unmasked = masking.unmask(outcome.masked, outcome.blocks, outcome.captions)
    units = anchors.build_units(unmasked, outcome.blocks, outcome.captions)
    parsed = synctex.parse(gzip.decompress((tree / "main.synctex.gz").read_bytes()).decode("utf-8"))
    pdf = tree / "main.pdf"
    media = [anchors.MediaBox(*(float(value) for value in page.mediabox)) for page in PdfReader(pdf).pages]
    main = tree / "main.tex"
    result = anchors.anchors_file(
        parsed, [str(main), str(main.resolve())], units, DOCUMENT, DOCUMENT, media, "main.pdf", "main.tex"
    )
    tables = [block for block in result.blocks if block.kind is AnchorKind.TABLE]
    assert [[rect.page for rect in block.rects] for block in tables] == [[1], [2]]
    for block in tables:
        (rect,) = block.rects
        body = result.pages[rect.page - 1].body
        assert body is not None
        assert_inside(rect, body, 0)
        rules = rule_extent(pdf, rect.page)
        assert (rect.x0, rect.y0, rect.x1, rect.y1) == pytest.approx(
            (rules.x0, rules.y0, rules.x1, rules.y1), abs=RULE_TOLERANCE
        )
    scaled, turned = (block.rects[0] for block in tables)
    assert scaled.x1 - scaled.x0 == pytest.approx(SCALED_WIDTH, abs=RULE_TOLERANCE)
    assert turned.y1 - turned.y0 > turned.x1 - turned.x0


def assert_inside(inner: AnchorRect, outer: AnchorRect, tolerance: float) -> None:
    assert outer.x0 - tolerance <= inner.x0 <= inner.x1 <= outer.x1 + tolerance, (inner, outer)
    assert outer.y0 - tolerance <= inner.y0 <= inner.y1 <= outer.y1 + tolerance, (inner, outer)


TITLE_DOCUMENT = r"""\documentclass{article}
\pagestyle{empty}
\begin{document}
\title{A Short Title}
\author{An Author}
\maketitle

A short paragraph after the title.
\end{document}
"""


def test_title_block_has_the_rects_of_maketitle(
    tmp_path: Path, environments_table: Mapping[str, masking.TableEntry]
) -> None:
    tree = tmp_path / "paper"
    tree.mkdir()
    (tree / "main.tex").write_text(TITLE_DOCUMENT, encoding="utf-8")
    attempt = compiling.attempt_compile(tree, "main.tex")
    assert attempt.passed, compiling.failure_message(attempt)
    outcome = masking.mask_document(TITLE_DOCUMENT, environments_table)
    unmasked = masking.unmask(outcome.masked, outcome.blocks, outcome.captions)
    units = anchors.build_units(unmasked, outcome.blocks, outcome.captions)
    parsed = synctex.parse(gzip.decompress((tree / "main.synctex.gz").read_bytes()).decode("utf-8"))
    media = [
        anchors.MediaBox(*(float(value) for value in page.mediabox)) for page in PdfReader(tree / "main.pdf").pages
    ]
    main = tree / "main.tex"

    def anchored(units: list[anchors.Unit]) -> list[AnchorBlock]:
        paths = [str(main), str(main.resolve())]
        return anchors.anchors_file(
            parsed, paths, units, TITLE_DOCUMENT, TITLE_DOCUMENT, media, "main.pdf", "main.tex"
        ).blocks

    title, *members, paragraph = anchored(units)
    assert (title.kind, title.parent, title.first_line, title.last_line) == (AnchorKind.METADATA, None, 4, 6)
    assert [(block.kind, block.parent, block.rects) for block in members] == [
        (AnchorKind.METADATA, title.id, []),
        (AnchorKind.METADATA, title.id, []),
    ]
    assert len(title.rects) >= 2
    assert all(rect.y0 > paragraph.rects[0].y1 for rect in title.rects)
    separate = sorted((replace(unit, parent=None) for unit in units), key=lambda unit: unit.start)
    unmerged = {block.id: block for block in anchored(separate)}
    assert unmerged[title.id].rects == title.rects
    assert (paragraph.kind, paragraph.parent, paragraph.first_line) == (AnchorKind.TEXT, None, 8)
    assert paragraph.rects == unmerged[paragraph.id].rects
