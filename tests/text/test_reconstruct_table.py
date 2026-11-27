from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pymupdf
import pytest

from tongtu.artifacts.reconstruct import Region, RegionKind, RegionsFile
from tongtu.reconstruct import table
from tongtu.reconstruct.recognition import RECOGNITION_PACKAGES
from tongtu.reconstruct.table import Glyph, TableVerdict

CHAR_WIDTH = 5.0

SIZE = 10.0

PREAMBLE = "\\usepackage{fontspec}\n\\begin{document}\n"

TABULAR = r"\begin{tabular}{ll}a & b\\\end{tabular}"


def word(text: str, x: float, y: float, bold: bool = False, color: int = 0) -> list[Glyph]:
    return [
        Glyph(char, bold, color, x + index * CHAR_WIDTH, x + (index + 1) * CHAR_WIDTH, y, SIZE)
        for index, char in enumerate(text)
    ]


def grid(rows: Sequence[Sequence[str]], bold: set[str] = frozenset()) -> list[Glyph]:
    return [
        glyph
        for row_index, row in enumerate(rows)
        for column, text in enumerate(row)
        for glyph in word(text, column * 60.0, row_index * 15.0, bold=text in bold)
    ]


ORIGINAL = [["Model", "BLEU"], ["Base", "25.8"], ["Big", "28.4"]]


@pytest.mark.parametrize(
    "latex",
    [r"\resizebox{1cm}{!}{x}", r"\scalebox{2}{x}", r"a\hspace{1pt}b", r"\hphantom{x}", r"\lower 2pt\hbox{x}"],
)
def test_forbidden_commands_are_rejected(latex: str) -> None:
    verdict = table.verify(Path("absent.pdf"), latex)
    assert not verdict.passed
    assert verdict.problems[0].startswith("Your LaTeX failed: forbidden command")


def test_size_commands_and_rotation_are_allowed() -> None:
    assert table.FORBIDDEN_RE.search(r"{\small a} & \rotatebox{90}{b} & \raisebox") is not None
    assert table.FORBIDDEN_RE.search(r"{\small a} & \rotatebox{90}{b} & \lowercase{c}") is None


def test_skip_is_not_verified() -> None:
    assert table.verify(Path("absent.pdf"), "SKIP") == TableVerdict(False, "SKIP", ("SKIP",))


def test_identical_tables_pass() -> None:
    comparison = table.compare(grid(ORIGINAL), grid(ORIGINAL))
    assert comparison.passed
    assert table.comparison_problems(comparison) == []


def test_glyphs_close_on_one_baseline_form_one_cell() -> None:
    cells = table.cells(grid(ORIGINAL))
    assert ["".join(glyph.char for glyph in cell) for cell in cells] == ["Model", "BLEU", "Base", "25.8", "Big", "28.4"]


def test_different_word_boundaries_fall_back_to_characters() -> None:
    rendered = [*word("Mo", 0, 0), *word("del", 20, 0), *grid(ORIGINAL)[len("Model") :]]
    comparison = table.compare(grid(ORIGINAL), rendered)
    assert comparison.text
    assert comparison.missing_words == ["model"]
    assert sorted(comparison.extra_words) == ["del", "mo"]
    assert comparison.passed


def test_different_characters_fail_with_the_missing_and_extra_characters() -> None:
    changed = [["Model", "BLEU"], ["Base", "25.9"], ["Big", "28.4"]]
    comparison = table.compare(grid(ORIGINAL), grid(changed))
    assert not comparison.text
    assert (comparison.missing_chars, comparison.extra_chars) == ("8", "9")
    problems = table.comparison_problems(comparison)
    assert "Characters in the original but missing from yours: '8'" in problems[0]
    assert "original-only: ['25.8']; yours-only: ['25.9']" in problems[1]


def test_swapped_rows_are_a_layout_difference() -> None:
    swapped = [["Model", "BLEU"], ["Big", "28.4"], ["Base", "25.8"]]
    comparison = table.compare(grid(ORIGINAL), grid(swapped))
    assert comparison.text
    assert "'base' is above 'big' in the original but below it in yours" in comparison.layout
    assert not comparison.passed


def test_swapped_columns_are_a_layout_difference() -> None:
    swapped = [["BLEU", "Model"], ["25.8", "Base"], ["28.4", "Big"]]
    comparison = table.compare(grid(ORIGINAL), grid(swapped))
    assert "'model' is left of 'bleu' in the original but right of it in yours" in comparison.layout


def test_repeated_words_are_paired_by_position() -> None:
    repeated = [["a", "x"], ["b", "x"]]
    assert table.compare(grid(repeated), grid(repeated)).passed


def test_bold_and_colour_differences_name_the_cells() -> None:
    comparison = table.compare(grid(ORIGINAL, bold={"28.4"}), grid(ORIGINAL))
    assert comparison.text and not comparison.layout
    assert comparison.bold_cells == ["28.4"]
    assert table.comparison_problems(comparison) == ["Bold differs for: 28.4"]
    coloured = [Glyph(g.char, g.bold, 0xFF0000 if g.char == "B" else 0, g.x, g.x2, g.y, g.size) for g in grid(ORIGINAL)]
    assert table.compare(coloured, grid(ORIGINAL)).color_cells == ["base", "big", "bleu"]


def test_rotated_text_forms_one_cell_per_line() -> None:
    rotated = [
        Glyph(char, False, 0, 0.0, 5.0, 100.0 - index * 5, SIZE, rotation=1) for index, char in enumerate("Rank")
    ]
    groups = table.cells([*rotated, *word("Top", 20, 50)])
    assert sorted("".join(glyph.char for glyph in group) for group in groups) == ["Rank", "Top"]


def crop(tree: Path, key: str, width: float) -> None:
    (tree / "crops").mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    doc.new_page(width=width, height=50)
    doc.save(tree / "crops" / f"{key}.pdf")


def test_write_back_scales_the_passed_tables_to_the_crop_width(tmp_path: Path) -> None:
    crop(tmp_path, "r-0001", 123.4)
    tex = PREAMBLE + "\\includegraphics{crops/r-0001.pdf}\n\\includegraphics{crops/r-0002.pdf}\n"
    verdicts = {
        "r-0001": TableVerdict(True, f" {TABULAR} ", ()),
        "r-0002": TableVerdict(False, TABULAR, ("Bold differs for: a",)),
    }
    written, count = table.write_back(tex, verdicts, tmp_path)
    assert count == 1
    assert f"\\resizebox{{123.4bp}}{{!}}{{{TABULAR}}}" in written
    assert "\\includegraphics{crops/r-0002.pdf}" in written
    assert RECOGNITION_PACKAGES + "\\usepackage{fontspec}" in written
    assert "xeCJK" not in written
    assert table.write_back(tex, {"r-0002": verdicts["r-0002"]}, tmp_path) == (tex, 0)


def test_write_back_adds_xecjk_for_chinese_cells(tmp_path: Path) -> None:
    crop(tmp_path, "r-0001", 100)
    tex = PREAMBLE + "\\includegraphics{crops/r-0001.pdf}\n"
    chinese = r"\begin{tabular}{l}模型\end{tabular}"
    written, _ = table.write_back(tex, {"r-0001": TableVerdict(True, chinese, ())}, tmp_path)
    assert table.CJK_PACKAGE in written


def test_recognizable_keys_are_referenced_table_crops() -> None:
    tex = PREAMBLE + "\\includegraphics{crops/r-0001.pdf}\\includegraphics{crops/r-0002.pdf}"
    regions = RegionsFile(
        regions=[
            Region(id="r-0001", kind=RegionKind.TABLE, page=1, rect=(0, 0, 1, 1)),
            Region(id="r-0002", kind=RegionKind.FIGURE, page=1, rect=(0, 0, 1, 1)),
            Region(id="r-0003", kind=RegionKind.TABLE, page=1, rect=(0, 0, 1, 1)),
        ]
    )
    assert table.recognizable(tex, regions) == ["r-0001"]


def test_failure_lines_count_the_passed_and_list_the_failures() -> None:
    verdicts = {
        "r-0001": TableVerdict(True, TABULAR, ()),
        "r-0002": TableVerdict(False, TABULAR, ("Bold differs for: a", "Colour differs for: b")),
        "r-0003": TableVerdict(False, "SKIP", ("SKIP",)),
    }
    assert table.failure_lines(verdicts) == [
        "passed 1/3",
        "r-0002: Bold differs for: a Colour differs for: b",
        "SKIP 1: r-0003",
    ]
