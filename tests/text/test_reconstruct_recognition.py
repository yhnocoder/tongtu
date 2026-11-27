from __future__ import annotations

from pathlib import Path

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


def test_verify_each_keeps_the_key_order() -> None:
    keys = [f"k{index}" for index in range(10)]
    assert list(recognition.verify_each(keys, str.upper).items()) == [(key, key.upper()) for key in keys]
