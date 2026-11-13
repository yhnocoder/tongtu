from __future__ import annotations

from collections.abc import Mapping

import pytest

from tongtu import anchors, masking, synctex
from tongtu.artifacts.compile import AnchorKind, AnchorRect, AnchorsFile

from ..conftest import synctex_file, synctex_record

UNITS_PAPER = r"""\documentclass{article}
\begin{document}
\section{Intro}\label{sec:intro}
First paragraph line one
% a comment line
line two.
\subsection*{More}
  \label{sec:more}
Text after heading.

Before math
\begin{equation}
x = 1
\end{equation}
after math.

\begin{figure}
\centering
\includegraphics{a.pdf}
\caption{A caption
over two lines.}
\end{figure}

\begin{thebibliography}{9}
\bibitem{a} Author A.
Title A.

\bibitem{b} Author B.
\end{thebibliography}
\end{document}
"""

UNITS_TRANSLATIONS: tuple[tuple[str, str], ...] = (
    ("First paragraph line one", "第一段第一行"),
    ("line two.", "第二行。"),
    ("Text after heading.", "标题后的文字。"),
    ("Before math", "公式之前"),
    ("after math.", "公式之后。"),
    ("A caption over two lines.", "一个图题。"),
)

GEOMETRY_PAPER = r"""\documentclass{article}
\begin{document}
\section{Intro}

Alpha beta gamma
delta epsilon.

\begin{figure}
\centering
\includegraphics{a.pdf}
\caption{A caption.}
\end{figure}

Last words
\mbox{boxed} end.
\end{document}
"""

MAIN = "/tmp/tree/./main.tex"

MEDIA = anchors.MediaBox(left=10.0, bottom=20.0, right=622.0, top=812.0)


def masked_units(
    tex: str, table: Mapping[str, masking.TableEntry], translations: tuple[tuple[str, str], ...] = ()
) -> tuple[masking.UnmaskOutcome, list[anchors.Unit]]:
    outcome = masking.mask_document(tex, table)
    masked = outcome.masked
    for source, target in translations:
        masked = masked.replace(source, target)
    unmasked = masking.unmask(masked, outcome.blocks, outcome.captions)
    return unmasked, anchors.build_units(unmasked, outcome.blocks, outcome.captions)


def describe(unmasked: masking.UnmaskOutcome, units: list[anchors.Unit]) -> list[tuple[str, AnchorKind, str | None]]:
    return [(unit.id, unit.kind, unit.parent) for unit in units]


def test_units_follow_paragraphs_headings_objects_and_bibitems(
    environments_table: Mapping[str, masking.TableEntry],
) -> None:
    unmasked, units = masked_units(UNITS_PAPER, environments_table)
    spans = {unit.id: unmasked.text[unit.start : unit.end] for unit in units}
    assert describe(unmasked, units) == [
        ("PAR-0-0", AnchorKind.HEADING, None),
        ("PAR-0-1", AnchorKind.TEXT, None),
        ("PAR-0-2", AnchorKind.HEADING, None),
        ("PAR-0-3", AnchorKind.TEXT, None),
        ("PAR-1-0", AnchorKind.TEXT, None),
        ("BLK-2", AnchorKind.MATH, None),
        ("PAR-1-1", AnchorKind.TEXT, None),
        ("BLK-3", AnchorKind.FIGURE, None),
        ("CAP-0", AnchorKind.CAPTION, "BLK-3"),
        ("BLK-4", AnchorKind.BIBLIOGRAPHY, None),
        ("BLK-4.0", AnchorKind.BIBITEM, "BLK-4"),
        ("BLK-4.1", AnchorKind.BIBITEM, "BLK-4"),
    ]
    assert spans["PAR-0-0"] == "\\section{Intro}\\label{sec:intro}"
    assert spans["PAR-0-1"] == "First paragraph line one\n% a comment line\nline two."
    assert spans["PAR-0-2"] == "\\subsection*{More}\n  \\label{sec:more}"
    assert spans["BLK-2"] == "\\begin{equation}\nx = 1\n\\end{equation}"
    assert spans["CAP-0"] == "\\caption{A caption\nover two lines.}"
    assert spans["BLK-4.0"] == "\\bibitem{a} Author A.\nTitle A."
    assert spans["BLK-4.1"] == "\\bibitem{b} Author B."


def test_source_and_translation_give_the_same_units(environments_table: Mapping[str, masking.TableEntry]) -> None:
    source, source_units = masked_units(UNITS_PAPER, environments_table)
    target, target_units = masked_units(UNITS_PAPER, environments_table, UNITS_TRANSLATIONS)
    assert target.text != source.text
    assert describe(target, target_units) == describe(source, source_units)
    spans = {unit.id: target.text[unit.start : unit.end] for unit in target_units}
    assert spans["CAP-0"] == "\\caption{一个图题。}"
    assert spans["PAR-1-1"] == "公式之后。"


def test_unmask_mismatch_is_a_mask_error(environments_table: Mapping[str, masking.TableEntry]) -> None:
    outcome = masking.mask_document(UNITS_PAPER, environments_table)
    unmasked = masking.unmask(outcome.masked, outcome.blocks, outcome.captions)
    with pytest.raises(masking.MaskError):
        anchors.build_units(
            masking.UnmaskOutcome(text=unmasked.text, fallbacks=(), stream=unmasked.stream, placements=()),
            outcome.blocks,
            outcome.captions,
        )


def test_line_ranges_follow_lines_changed_after_unmask(environments_table: Mapping[str, masking.TableEntry]) -> None:
    unmasked, units = masked_units(UNITS_PAPER, environments_table)
    final = unmasked.text.replace("\\begin{document}\n", "\\begin{document}\n\\usepackage{fix}\n\\fixed\n")
    ranges = dict(zip((unit.id for unit in units), anchors.line_ranges(units, unmasked.text, final), strict=True))
    assert ranges["PAR-0-0"] == (5, 5)
    assert ranges["BLK-2"] == (14, 16)
    assert ranges["BLK-4.1"] == (30, 30)
    same = dict(zip((unit.id for unit in units), anchors.line_ranges(units, unmasked.text, unmasked.text), strict=True))
    assert same["PAR-0-0"] == (3, 3)


def line(tag_line: int, x: float, baseline: float, width: float, *records: str, height: float = 10) -> list[str]:
    return [synctex_record("(", 1, tag_line, x, baseline, width, height, 2), *records, ")"]


def geometry_page() -> list[str]:
    return [
        synctex_record("[", 1, 2, 0, 700, 500, 700, 0),
        *line(3, 0, 20, 500, synctex_record("g", 1, 3, 100, 20)),
        synctex_record("[", 1, 2, 0, 680, 480, 640, 0),
        *line(3, 0, 60, 480, synctex_record("g", 1, 3, 100, 60), synctex_record("k", 1, 3, 200, 60, 1)),
        *line(
            5,
            0,
            90,
            480,
            synctex_record("g", 1, 5, 100, 90),
            synctex_record("g", 1, 8, 479.7, 90),
        ),
        *line(6, 0, 102, 200, synctex_record("g", 1, 6, 50, 102), synctex_record("k", 1, 6, 150, 102, 1)),
        synctex_record("(", 1, 10, 90, 200, 220, 80, 0),
        synctex_record("(", 1, 10, 100, 200, 200, 80, 0),
        ")",
        ")",
        *line(11, 50, 300, 380, synctex_record("g", 1, 11, 100, 300), synctex_record("k", 1, 11, 300, 300, 1)),
        synctex_record("(", 1, 14, 0, 330, 480, 10, 2),
        synctex_record("g", 1, 14, 0, 330),
        *line(14, 20, 330, 200, synctex_record("g", 1, 14, 60, 330), synctex_record("k", 1, 14, 120, 330, 1)),
        synctex_record("g", 1, 14, 220, 330),
        ")",
        synctex_record("(", 1, 15, 0, 342, 480, 10, 2),
        synctex_record("g", 1, 15, 40, 342),
        synctex_record("g", 1, 15, 70, 342),
        *line(15, 100, 342, 50, synctex_record("x", 1, 15, 110, 342)),
        *line(15, 200, 342, 50, synctex_record("x", 1, 15, 210, 342)),
        synctex_record("k", 1, 15, 160, 342, 1),
        ")",
        "]",
        "]",
    ]


def geometry_anchors(
    table: Mapping[str, masking.TableEntry], pages: list[list[str]], media: list[anchors.MediaBox]
) -> AnchorsFile:
    unmasked, units = masked_units(GEOMETRY_PAPER, table)
    parsed = synctex.parse(synctex_file({1: MAIN, 2: "/tmp/tree/other.tex"}, pages))
    return anchors.anchors_file(
        parsed, ["/tmp/tree/main.tex"], units, unmasked.text, unmasked.text, media, "x.zh.pdf", "zh.tex"
    )


def box(x0: float, top: float, x1: float, bottom: float) -> tuple[float, ...]:
    return (MEDIA.left + x0, MEDIA.top - bottom, MEDIA.left + x1, MEDIA.top - top)


def corners(rects: list[AnchorRect]) -> list[tuple[float, ...]]:
    return [tuple(round(value, 1) for value in (rect.x0, rect.y0, rect.x1, rect.y1)) for rect in rects]


def test_anchors_file_maps_blocks_to_rects_in_pdf_space(environments_table: Mapping[str, masking.TableEntry]) -> None:
    result = geometry_anchors(environments_table, [geometry_page()], [MEDIA])
    assert (result.pdf, result.tex) == ("x.zh.pdf", "zh.tex")
    (page,) = result.pages
    assert (page.page, page.width, page.height) == (1, 612.0, 792.0)
    assert page.body is not None
    assert corners([page.body]) == [box(0, 40, 480, 680)]
    blocks = {block.id: block for block in result.blocks}
    assert [(block.id, block.kind, block.parent, block.first_line, block.last_line) for block in result.blocks] == [
        ("PAR-0-0", AnchorKind.HEADING, None, 3, 3),
        ("PAR-1-0", AnchorKind.TEXT, None, 5, 6),
        ("BLK-1", AnchorKind.FIGURE, None, 8, 12),
        ("CAP-0", AnchorKind.CAPTION, "BLK-1", 11, 11),
        ("PAR-3-0", AnchorKind.TEXT, None, 14, 15),
    ]
    assert corners(blocks["PAR-0-0"].rects) == [box(0, 50, 480, 62)]
    assert corners(blocks["PAR-1-0"].rects) == [box(0, 80, 480, 92), box(0, 92, 200, 104)]
    assert corners(blocks["CAP-0"].rects) == [box(50, 290, 430, 302)]
    assert corners(blocks["BLK-1"].rects) == [box(90, 120, 310, 200), box(50, 290, 430, 302)]
    assert corners(blocks["PAR-3-0"].rects) == [box(20, 320, 220, 332), box(0, 332, 480, 344)]
    assert all(rect.page == 1 for block in result.blocks for rect in block.rects)


def test_page_without_body_has_no_body_rect(environments_table: Mapping[str, masking.TableEntry]) -> None:
    result = geometry_anchors(environments_table, [geometry_page(), []], [MEDIA, MEDIA])
    assert [page.body is None for page in result.pages] == [False, True]


def test_page_count_mismatch_is_an_anchors_error(environments_table: Mapping[str, masking.TableEntry]) -> None:
    with pytest.raises(anchors.AnchorsError):
        geometry_anchors(environments_table, [geometry_page()], [MEDIA, MEDIA])


def test_missing_main_input_is_an_anchors_error(environments_table: Mapping[str, masking.TableEntry]) -> None:
    unmasked, units = masked_units(GEOMETRY_PAPER, environments_table)
    parsed = synctex.parse(synctex_file({2: "/tmp/tree/other.tex"}, [geometry_page()]))
    with pytest.raises(anchors.AnchorsError):
        anchors.anchors_file(
            parsed, ["/tmp/tree/main.tex"], units, unmasked.text, unmasked.text, [MEDIA], "x.zh.pdf", "zh.tex"
        )


SCALED_PAPER = r"""\documentclass{article}
\begin{document}
Some words.

\begin{table}
\scalebox{0.5}{\begin{tabular}{ll}
a & b \\
\end{tabular}}
\end{table}
\end{document}
"""


def scaled_page() -> list[str]:
    def group(line: int, x: float, width: float, height: float, depth: float) -> str:
        return synctex_record("(", 1, line, x, 200, width, height, depth)

    return [
        synctex_record("[", 1, 2, 0, 700, 500, 700, 0),
        synctex_record("[", 1, 2, 0, 680, 480, 640, 0),
        *line(3, 0, 60, 480, synctex_record("g", 1, 3, 100, 60), synctex_record("k", 1, 3, 200, 60, 1)),
        group(6, 100, 60, 20, 10),
        synctex_record("g", 1, 6, 160, 200),
        group(6, 100, 0, 20, 10),
        group(6, 100, 0, 40, 20),
        group(6, 100, 120, 40, 20),
        *line(7, 100, 172, 120, synctex_record("g", 1, 7, 130, 172), synctex_record("k", 1, 7, 150, 172, 1), height=8),
        ")",
        ")",
        ")",
        ")",
        "]",
        "]",
    ]


def test_scaled_table_rect_follows_the_scaled_box(environments_table: Mapping[str, masking.TableEntry]) -> None:
    unmasked, units = masked_units(SCALED_PAPER, environments_table)
    parsed = synctex.parse(synctex_file({1: MAIN}, [scaled_page()]))
    result = anchors.anchors_file(
        parsed, ["/tmp/tree/main.tex"], units, unmasked.text, unmasked.text, [MEDIA], "x.zh.pdf", "zh.tex"
    )
    (table,) = [block for block in result.blocks if block.kind is AnchorKind.TABLE]
    assert corners(table.rects) == [box(100, 182, 160, 187)]


def rect(x: float, y: float, width: float, height: float) -> anchors.Rect:
    return anchors.Rect(page=1, x=x, y=y, width=width, height=height)


def test_merge_drops_contained_rects_and_joins_close_rects_on_one_line() -> None:
    merged = anchors.merge(
        [
            rect(0, 0, 100, 10),
            rect(10, 2, 20, 5),
            rect(104, 0, 50, 10),
            rect(0, 40, 100, 10),
            rect(120, 40, 50, 10),
            rect(0, 80, 0.1, 10),
        ]
    )
    assert [(item.x, item.y, item.width, item.height) for item in merged] == [
        (0, 0, 154, 10),
        (0, 40, 100, 10),
        (120, 40, 50, 10),
    ]
