from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from tongtu import cropping, processes
from tongtu.processes import ProcessOutcome


def blank_pdf(path: Path, pages: int = 2) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def wire_ghostscript(
    monkeypatch: pytest.MonkeyPatch, stderr: str, returncode: int = 0, timed_out: bool = False
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command: list[str], cwd: Path, timeout: float, **kwargs: object) -> ProcessOutcome:
        calls.append([*command, str(cwd)])
        return ProcessOutcome(
            returncode=returncode,
            stdout=b"",
            stderr=stderr.encode("utf-8"),
            timed_out=timed_out,
            duration_seconds=0.1,
        )

    monkeypatch.setattr(processes, "run_in_process_group", run)
    return calls


def bbox_lines(*boxes: tuple[float, float, float, float]) -> str:
    return "".join(
        f"%%BoundingBox: 0 0 0 0\n%%HiResBoundingBox: {left} {bottom} {right} {top}\n"
        for left, bottom, right, top in boxes
    )


def crop_boxes(pdf: Path) -> list[list[float]]:
    return [[float(value) for value in page.cropbox] for page in PdfReader(pdf).pages]


def test_every_page_gets_the_union_of_the_content_plus_padding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = blank_pdf(tmp_path / "paper.zh.pdf")
    original = pdf.read_bytes()
    calls = wire_ghostscript(monkeypatch, bbox_lines((120, 90, 480, 700), (118, 100, 520, 710)))
    assert cropping.crop_margins(pdf) == []
    assert calls == [[*cropping.GHOSTSCRIPT_BBOX_COMMAND, "paper.zh.pdf", str(tmp_path)]]
    padding = cropping.CROP_PADDING_POINTS
    expected = [118 - padding, 90 - padding, 520 + padding, 710 + padding]
    assert crop_boxes(pdf) == [expected, expected]
    assert [[float(value) for value in page.mediabox] for page in PdfReader(pdf).pages] == [[0, 0, 612, 792]] * 2
    assert pdf.read_bytes().startswith(original)
    assert not (tmp_path / "paper.zh.pdf.part").exists()


def test_the_crop_box_stays_inside_the_media_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = blank_pdf(tmp_path / "paper.zh.pdf", pages=1)
    wire_ghostscript(monkeypatch, bbox_lines((2, 3, 610, 791)))
    assert cropping.crop_margins(pdf) == []
    assert crop_boxes(pdf) == [[0, 0, 612, 792]]


def test_empty_pages_do_not_shrink_the_union(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = blank_pdf(tmp_path / "paper.zh.pdf")
    wire_ghostscript(monkeypatch, bbox_lines((100, 100, 500, 700), (0, 0, 0, 0)))
    assert cropping.crop_margins(pdf) == []
    padding = cropping.CROP_PADDING_POINTS
    expected = [100 - padding, 100 - padding, 500 + padding, 700 + padding]
    assert crop_boxes(pdf) == [expected, expected]


@pytest.mark.parametrize(
    ("stderr", "returncode", "timed_out", "fragment"),
    [
        (bbox_lines((0, 0, 0, 0), (0, 0, 0, 0)), 0, False, "no content on upright pages"),
        ("", 0, False, "measured 0 pages"),
        (bbox_lines((100, 100, 500, 700)), 0, False, "measured 1 pages"),
        ("%%HiResBoundingBox: a b c d\n", 0, False, "could not convert"),
        ("Error: /undefined", 1, False, "exited with code 1"),
        ("", -9, True, "timeout"),
    ],
)
def test_measurement_failures_keep_the_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    returncode: int,
    timed_out: bool,
    fragment: str,
) -> None:
    pdf = blank_pdf(tmp_path / "paper.zh.pdf")
    original = pdf.read_bytes()
    wire_ghostscript(monkeypatch, stderr, returncode, timed_out)
    warnings = cropping.crop_margins(pdf)
    assert len(warnings) == 1
    assert fragment in warnings[0] and "keeps its margins" in warnings[0]
    assert pdf.read_bytes() == original


def test_missing_ghostscript_keeps_the_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = blank_pdf(tmp_path / "paper.zh.pdf")
    original = pdf.read_bytes()

    def run(command: list[str], cwd: Path, timeout: float, **kwargs: object) -> ProcessOutcome:
        raise FileNotFoundError(2, "No such file or directory", "gs")

    monkeypatch.setattr(processes, "run_in_process_group", run)
    warnings = cropping.crop_margins(pdf)
    assert len(warnings) == 1 and "Ghostscript" in warnings[0]
    assert pdf.read_bytes() == original


def test_an_unreadable_pdf_keeps_its_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "paper.zh.pdf"
    pdf.write_bytes(b"%PDF-1.5 truncated")
    wire_ghostscript(monkeypatch, bbox_lines((100, 100, 500, 700)))
    warnings = cropping.crop_margins(pdf)
    assert len(warnings) == 1 and "crop box" in warnings[0]
    assert pdf.read_bytes() == b"%PDF-1.5 truncated"
    assert not (tmp_path / "paper.zh.pdf.part").exists()


def test_enabled_follows_the_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    assert cropping.enabled() is False
    (tmp_path / "config.toml").write_text("[pdf]\ncrop_margins = true\n", encoding="utf-8")
    assert cropping.enabled() is True


def test_rotated_pages_keep_their_media_box_and_stay_out_of_the_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=612, height=792)
    writer.pages[1].rotate(90)
    pdf = tmp_path / "paper.zh.pdf"
    with pdf.open("wb") as stream:
        writer.write(stream)
    wire_ghostscript(monkeypatch, bbox_lines((100, 100, 500, 700), (70, 80, 780, 600)))
    assert cropping.crop_margins(pdf) == []
    padding = cropping.CROP_PADDING_POINTS
    assert crop_boxes(pdf) == [[100 - padding, 100 - padding, 500 + padding, 700 + padding], [0, 0, 612, 792]]


def test_pages_of_each_size_get_their_own_union(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=1224, height=792)
    writer.add_blank_page(width=612, height=792)
    pdf = tmp_path / "paper.zh.pdf"
    with pdf.open("wb") as stream:
        writer.write(stream)
    wire_ghostscript(monkeypatch, bbox_lines((100, 100, 500, 700), (50, 60, 1180, 760), (110, 90, 510, 690)))
    assert cropping.crop_margins(pdf) == []
    padding = cropping.CROP_PADDING_POINTS
    letter = [100 - padding, 90 - padding, 510 + padding, 700 + padding]
    assert crop_boxes(pdf) == [letter, [50 - padding, 60 - padding, 1180 + padding, 760 + padding], letter]
