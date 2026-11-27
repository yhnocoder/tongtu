from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tongtu.reconstruct import recognition


def test_transcriptions_are_read_from_rec_json(tmp_path: Path) -> None:
    (tmp_path / "rec.json").write_text('{"e-0001": "a+b", "m-0002": "SKIP"}', encoding="utf-8")
    assert recognition.read_transcriptions(tmp_path, "reconstruct_formula") == (
        {"e-0001": "a+b", "m-0002": "SKIP"},
        "",
    )


def test_missing_rec_json_keeps_every_crop(tmp_path: Path) -> None:
    transcriptions, problem = recognition.read_transcriptions(tmp_path, "reconstruct_table")
    assert transcriptions == {}
    assert problem == "the reconstruct_table session wrote no rec.json; every crop stays an image"


def test_invalid_rec_json_keeps_every_crop(tmp_path: Path) -> None:
    (tmp_path / "rec.json").write_text('{"e-0001": 3}', encoding="utf-8")
    transcriptions, problem = recognition.read_transcriptions(tmp_path, "reconstruct_formula")
    assert transcriptions == {}
    assert problem.startswith("cannot read rec.json written by the reconstruct_formula session (ValidationError")


def test_packages_are_added_once_before_fontspec() -> None:
    tex = "\\usepackage{bm}\\usepackage{fontspec}\n\\begin{document}"
    once = recognition.with_packages(tex)
    assert once == "\\usepackage{bm}" + recognition.RECOGNITION_PACKAGES + "\\usepackage{fontspec}\n\\begin{document}"
    assert recognition.with_packages(once) == once


def test_verify_all_keeps_the_key_order_and_reports_missing_transcriptions() -> None:
    keys = [f"k{index}" for index in range(10)]
    transcriptions = {key: key.upper() for key in keys[1:]}
    verdicts = recognition.verify_all(keys, transcriptions, lambda key, latex: recognition.Verdict(True, latex, ""))
    assert list(verdicts) == keys
    assert verdicts["k0"] == recognition.Verdict(False, "", recognition.MISSING_TRANSCRIPTION)
    assert verdicts["k5"] == recognition.Verdict(True, "K5", "")


@pytest.mark.parametrize(
    ("font", "bold"),
    [
        ("SFBX1000", True),
        ("CMBX10", True),
        ("NimbusRomNo9L-Medi", True),
        ("CMMIB10", True),
        ("LMRoman10-Bold", True),
        ("Times-Semibold", True),
        ("MSBM10", False),
        ("BlackboardBold", False),
        ("STIXMath-BlackboardBold", False),
        ("CMMI10", False),
        ("NimbusRomNo9L-Regu", False),
    ],
)
def test_bold_fonts_are_judged_by_name(font: str, bold: bool) -> None:
    assert recognition.is_bold(font) is bold


@pytest.mark.parametrize(
    ("latex", "command"),
    [
        (r"\raise1pt x", r"\raise"),
        (r"\hphantom{x}", r"\hphantom"),
        (r"\vphantom{x}", r"\vphantom"),
        (r"a\hskip 2pt b", r"\hskip"),
        (r"a\mskip2mu b", r"\mskip"),
        (r"\vskip1pt", r"\vskip"),
        (r"\resizebox{1cm}{!}{x}", r"\resizebox"),
    ],
)
def test_layout_commands_are_found_even_before_digits(latex: str, command: str) -> None:
    assert recognition.layout_command(latex) == command


def test_ordinary_commands_are_not_layout_commands() -> None:
    assert recognition.layout_command(r"\lowercase{x}\raised\phantomx") == ""


def test_failure_lines_hide_or_show_the_latex() -> None:
    verdicts = {"a": recognition.Verdict(False, "x", "bad"), "b": recognition.Verdict(True, "y", "")}
    assert recognition.failure_lines(verdicts, show_latex=False) == ["passed 1/2", "a: bad"]
    assert recognition.failure_lines(verdicts, show_latex=True) == ["passed 1/2", "a: bad\n  yours: x"]


@pytest.mark.parametrize(
    ("command", "problem"),
    [
        ([sys.executable, "-c", "raise SystemExit(1)"], recognition.COMPILE_FAILED),
        (
            [sys.executable, "-c", "print('! Undefined control sequence.'); raise SystemExit(1)"],
            "! Undefined control sequence.",
        ),
        (["tongtu-no-such-engine"], "failed to run tongtu-no-such-engine (FileNotFoundError"),
    ],
)
def test_snippet_compile_failures_name_the_cause(command: list[str], problem: str) -> None:
    result = recognition.compile_snippet("x", command, lambda page: page)
    assert isinstance(result, str) and result.startswith(problem)
