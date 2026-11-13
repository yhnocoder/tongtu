from __future__ import annotations

import pytest

from tongtu import synctex

from ..conftest import synctex_file, synctex_record


def page_with_body(*lines: str) -> list[str]:
    return [
        synctex_record("[", 1, 1, 0, 700, 500, 700, 0),
        synctex_record("h", 1, 1, 0, 20, 500, 20, 0),
        synctex_record("[", 1, 1, 0, 680, 480, 640, 0),
        *lines,
        "]",
        "]",
    ]


def test_parse_reads_inputs_boxes_and_records() -> None:
    text = synctex_file(
        {1: "/tmp/tree/./main.tex", 2: "/tmp/tree/sections/intro.tex"},
        [
            page_with_body(
                synctex_record("(", 2, 7, 10, 40, 300, 10, 2),
                synctex_record("g", 2, 7, 50, 40),
                synctex_record("k", 2, 7, 60, 40, 3),
                ")",
            )
        ],
    )
    parsed = synctex.parse(text)
    assert parsed.inputs == {1: "/tmp/tree/./main.tex", 2: "/tmp/tree/sections/intro.tex"}
    assert parsed.tags_of("/tmp/tree/main.tex") == {1}
    assert parsed.tags_of("/tmp/tree/other.tex") == set()
    assert list(parsed.pages) == [1]
    root, body, line = parsed.boxes
    assert root.parent is None and root.child_count == 2 and root.children == [body.index]
    assert body.parent == root.index and body.children == [line.index]
    assert line.kind == synctex.HBOX and (line.tag, line.line) == (2, 7)
    assert [record.kind for record in line.records] == ["g", "k"]
    assert parsed.points(line.records[1].width) == pytest.approx(3, abs=1e-4)
    assert parsed.is_descendant(line, root)
    assert not parsed.is_descendant(root, line)


def test_header_offset_unit_and_magnification_scale_coordinates() -> None:
    text = synctex_file({1: "main.tex"}, [page_with_body()])
    text = text.replace("Unit:1", "Unit:2").replace("Magnification:1000", "Magnification:500")
    text = text.replace("X Offset:0", "X Offset:4736287").replace("Y Offset:0", "Y Offset:-4736287")
    parsed = synctex.parse(text)
    assert parsed.points(65536) == pytest.approx(72 / 72.27)
    assert parsed.x_points(0) == pytest.approx(72, abs=1e-4)
    assert parsed.y_points(0) == pytest.approx(-72, abs=1e-4)
    assert parsed.raw(parsed.points(123456)) == 123456


def test_body_box_is_the_tall_wide_child() -> None:
    parsed = synctex.parse(synctex_file({1: "main.tex"}, [page_with_body(), []]))
    body = parsed.body_boxes()
    assert list(body) == [1]
    assert body[1].index == 1


def test_body_search_stops_at_a_narrow_child() -> None:
    page = [
        synctex_record("[", 1, 1, 0, 700, 500, 700, 0),
        synctex_record("[", 1, 1, 0, 680, 200, 640, 0),
        "]",
        "]",
    ]
    parsed = synctex.parse(synctex_file({1: "main.tex"}, [page]))
    assert parsed.body_boxes()[1].index == 0


def test_no_pages_is_an_error() -> None:
    with pytest.raises(synctex.SynctexError):
        synctex.parse("SyncTeX Version:1\nInput:1:main.tex\nContent:\nPostamble:\n")


def test_non_integer_header_is_an_error() -> None:
    text = synctex_file({1: "main.tex"}, [page_with_body()]).replace("Unit:1", "Unit:one")
    with pytest.raises(synctex.SynctexError):
        synctex.parse(text)
