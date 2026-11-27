from __future__ import annotations

from pathlib import Path

import pytest

from tongtu.artifacts.reconstruct import Region, RegionKind, RegionsFile
from tongtu.manifests import write_manifest
from tongtu.reconstruct import formula
from tongtu.reconstruct.formula import Glyph
from tongtu.reconstruct.recognition import RECOGNITION_PACKAGES, Verdict

from ..conftest import run_skill_check

PREAMBLE = "\\usepackage{bm}\\usepackage{fontspec}\n\\begin{document}\n"

DISPLAY = "\\par\\smallskip\\noindent\\hfil\\includegraphics{crops/e-0002.pdf}\\hfil\\par\\smallskip"

SHIFTED_DISPLAY = "\\par\\smallskip\\noindent\\hspace*{12.0bp}\\includegraphics{crops/e-0003.pdf}\\par\\smallskip"


def glyphs(text: str, bold: bool = False, color: int = 0) -> list[Glyph]:
    return [Glyph(char, bold, color, 100.0, 10.0) for char in text]


@pytest.mark.parametrize(
    "latex",
    [
        r"x\kern1pt y",
        r"\raisebox{1pt}{x}",
        r"x\hspace*{1pt}",
        r"{\small x}",
        r"\Huge x",
        r"\mathrlap{x}",
        r"\raise1pt x",
        r"\hphantom{x}",
        r"\vphantom{x}",
        r"x\hskip1pt y",
        r"x\mskip2mu y",
        r"\vskip1pt",
    ],
)
def test_layout_and_size_commands_are_rejected(latex: str) -> None:
    verdict = formula.verify(Path("absent.pdf"), latex, RegionKind.INLINE)
    assert not verdict.passed
    assert "is not allowed" in verdict.problem


@pytest.mark.parametrize("latex", [r"\begin{smallmatrix}a\end{smallmatrix}", r"\lowercase{x}", r"\lambda_{\max}"])
def test_ordinary_markup_is_not_forbidden(latex: str) -> None:
    assert formula.forbidden_problem(latex) == ""


def test_skip_is_not_verified() -> None:
    assert formula.verify(Path("absent.pdf"), " SKIP ", RegionKind.DISPLAY) == Verdict(False, " SKIP ", "SKIP")


def test_identical_glyphs_pass() -> None:
    comparison = formula.compare(glyphs("x+y"), glyphs("x+y"))
    assert comparison.text and comparison.bold and comparison.color
    assert formula.within_tolerance(comparison)
    assert formula.difference_problem(comparison) == ""


def test_different_characters_fail_with_the_differing_part() -> None:
    comparison = formula.compare(glyphs("a+b=c"), glyphs("a+d=c"))
    assert not comparison.text
    assert "original 'a+b=c' vs yours 'a+d=c'" in formula.difference_problem(comparison)


def test_bold_and_colour_are_compared_glyph_by_glyph() -> None:
    original = [*glyphs("k", bold=True), *glyphs("t")]
    comparison = formula.compare(original, glyphs("kt", bold=True))
    assert comparison.text and not comparison.bold
    assert "bold pattern differs" in formula.difference_problem(comparison)
    coloured = formula.compare(glyphs("kt", color=0xFF0000), glyphs("kt"))
    assert coloured.bold and not coloured.color
    assert "colour differs" in formula.difference_problem(coloured)


def test_baseline_and_size_beyond_the_tolerance_fail() -> None:
    original = [Glyph("x", False, 0, 100.0, 10.0), Glyph("2", False, 0, 96.0, 7.0)]
    subscript = [Glyph("x", False, 0, 100.0, 10.0), Glyph("2", False, 0, 102.0, 7.0)]
    comparison = formula.compare(original, subscript)
    assert comparison.text and comparison.bold and comparison.color
    assert comparison.baseline_shift == 0.6
    assert not formula.within_tolerance(comparison)
    assert "vertical placement or size" in formula.difference_problem(comparison)
    smaller = [Glyph("x", False, 0, 100.0, 10.0), Glyph("2", False, 0, 96.0, 5.0)]
    assert formula.compare(original, smaller).size_change > formula.SIZE_TOLERANCE
    close = [Glyph("x", False, 0, 100.0, 10.0), Glyph("2", False, 0, 96.5, 7.5)]
    assert formula.within_tolerance(formula.compare(original, close))


def test_variants_try_unbold_scope_allbold_and_colour() -> None:
    names = [name for name, _ in formula.variants(r"\bm{k_t}", glyphs("kt", bold=True, color=0x0000FF))]
    assert names == [
        "raw",
        "unbold",
        "scope",
        "allbold",
        "raw+color",
        "unbold+color",
        "scope+color",
        "allbold+color",
    ]
    found = dict(formula.variants(r"\bm{k_t}", glyphs("kt", bold=True, color=0x0000FF)))
    assert found["unbold"] == "k_t"
    assert found["scope"] == r"\bm{k}_t"
    assert found["allbold"] == r"\bm{k_t}"
    assert found["raw+color"] == r"{\color[HTML]{0000FF}\bm{k_t}}"
    assert [name for name, _ in formula.variants("x", glyphs("x"))] == ["raw"]


def test_math_delimiters_are_stripped() -> None:
    assert formula.math_body(r" \[ a+b \] ") == "a+b"
    assert formula.math_body("$a$") == "a"


def test_display_blocks_of_prose_or_other_environments_are_rejected() -> None:
    assert "prose" in formula.display_problem(r"\text{for all inputs} x")
    assert "environments" in formula.display_problem(r"\begin{eqnarray}a\end{eqnarray}")
    assert formula.display_problem(r"\begin{aligned}a&=b\end{aligned}") == ""
    assert formula.display_problem(r"x \text{ if } y") == ""


def test_equation_number_becomes_a_tag() -> None:
    assert formula.display_latex(r"a=b \quad (3)") == r"\begin{equation*}a=b\tag*{(3)}\end{equation*}"
    assert formula.display_latex(r"a=b\qquad(3.1)") == r"\begin{equation*}a=b\tag*{(3.1)}\end{equation*}"
    assert formula.display_latex(r"a=b \hfill (A1)") == r"\begin{equation*}a=b\tag*{(A1)}\end{equation*}"
    assert formula.display_latex("a=b") == r"\begin{equation*}a=b\end{equation*}"


@pytest.mark.parametrize("body", ["y = f(x)", r"\sum_i g(i)", "a = b (1)"])
def test_parentheses_without_a_quad_are_not_an_equation_number(body: str) -> None:
    assert formula.display_latex(body) == r"\begin{equation*}" + body + r"\end{equation*}"


@pytest.mark.parametrize(
    ("char", "font", "text"),
    [
        ("P", "CMEX10", "∑"),
        ("X", "CMEX10", "∑"),
        ("R", "CMEX10", "∫"),
        ("a", "CMEX10", "∐"),
        ("0", "CMEX10", ""),
        ("P", "CMMI10", "P"),
        ("√", "CMSY10", ""),
        ("−", "CMSY10", "-"),
        ("ﬁ", "CMR10", "fi"),
    ],
)
def test_extension_glyphs_map_to_unicode(char: str, font: str, text: str) -> None:
    assert formula.glyph_text(char, font) == text


def test_write_back_replaces_only_the_passed_crops() -> None:
    tex = (
        PREAMBLE
        + f"Let\\im{{m-0001}}{{1.20bp}}{{x}}be and \\im{{m-0004}}{{-0.50bp}}{{y}}.\n{DISPLAY}\n{SHIFTED_DISPLAY}\n"
    )
    verdicts = {
        "m-0001": Verdict(True, "x_i", ""),
        "m-0004": Verdict(False, "y", "Glyph sequence differs"),
        "e-0002": Verdict(True, r"a=b \quad (1)", ""),
        "e-0003": Verdict(True, "c", ""),
    }
    written, count = formula.write_back(tex, verdicts)
    assert count == 3
    assert "Let $x_i$ be and \\im{m-0004}{-0.50bp}{y}." in written
    assert "\\begin{equation*}a=b\\tag*{(1)}\\end{equation*}" in written
    assert "\\begin{equation*}c\\end{equation*}" in written
    assert written.count(RECOGNITION_PACKAGES + "\\usepackage{fontspec}") == 1
    unchanged, none = formula.write_back(tex, {"m-0004": verdicts["m-0004"]})
    assert (unchanged, none) == (tex, 0)


def test_recognizable_keys_are_display_and_inline_crops_that_can_be_replaced() -> None:
    tex = (
        PREAMBLE
        + f"\\im{{m-0001}}{{1.20bp}}{{x}}\n{DISPLAY}\n\\begin{{strip}}\\includegraphics{{crops/e-0005.pdf}}\\end{{strip}}"
    )
    regions = RegionsFile(
        regions=[
            Region(id="m-0001", kind=RegionKind.INLINE, page=1, rect=(0, 0, 1, 1)),
            Region(id="e-0002", kind=RegionKind.DISPLAY, page=1, rect=(0, 0, 1, 1)),
            Region(id="e-0005", kind=RegionKind.DISPLAY, page=1, rect=(0, 0, 1, 1)),
        ]
    )
    assert formula.recognizable(tex, regions) == {"e-0002": RegionKind.DISPLAY, "m-0001": RegionKind.INLINE}


def test_failure_lines_count_the_passed_and_list_the_failures() -> None:
    verdicts = {
        "e-0001": Verdict(True, "a", ""),
        "e-0002": Verdict(False, "b", "LaTeX failed to compile."),
        "m-0003": Verdict(False, "SKIP", "SKIP"),
    }
    assert formula.failure_lines(verdicts) == [
        "passed 1/3",
        "e-0002: LaTeX failed to compile.\n  yours: b",
        "SKIP 1: m-0003",
    ]


def test_check_script_without_hints_fails(tmp_path: Path) -> None:
    result = run_skill_check(tmp_path, "reconstruct_formula")
    assert result.returncode == 1
    assert result.stdout.strip() == "cannot read hints.json"


@pytest.mark.parametrize(
    ("rec", "problem"),
    [
        (None, "the reconstruct_formula session wrote no rec.json; every crop stays an image"),
        ('{"m-0001": 1}', "cannot read rec.json written by the reconstruct_formula session (ValidationError"),
    ],
)
def test_check_script_with_a_bad_rec_json_fails(tmp_path: Path, rec: str | None, problem: str) -> None:
    write_manifest(
        tmp_path / "hints.json",
        formula.FormulaHints({"m-0001": formula.FormulaHint(kind=RegionKind.INLINE, spans=["CMMI10#000000:x"])}),
    )
    if rec is not None:
        (tmp_path / "rec.json").write_text(rec, encoding="utf-8")
    result = run_skill_check(tmp_path, "reconstruct_formula")
    assert result.returncode == 1
    assert result.stdout.startswith(problem)
