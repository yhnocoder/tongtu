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


def hbox(x: float, baseline: float, width: float, height: float, depth: float, *inside: str) -> list[str]:
    return [synctex_record("(", 1, 5, x, baseline, width, height, depth), *inside, ")"]


def scaled(x: float, baseline: float, scale: float, natural: tuple[float, float, float], *inside: str) -> list[str]:
    width, height, depth = natural
    rlap = hbox(x, baseline, 0, height, depth, *hbox(x, baseline, width, height, depth, *inside))
    return hbox(
        x,
        baseline,
        width * scale,
        height * scale,
        depth * scale,
        *hbox(x, baseline, 0, height * scale, depth * scale, *rlap),
    )


def turned(x: float, baseline: float, outer: tuple[float, float, float, float], *inside: str) -> list[str]:
    offset, width, height, depth = outer
    natural = hbox(x, baseline, 100, 20, 10, *hbox(x, baseline, 100, 20, 10, *inside))
    return hbox(x - offset, baseline, width, height, depth, *natural)


def parse_page(*boxes: str) -> synctex.Synctex:
    return synctex.parse(synctex_file({1: "main.tex"}, [page_with_body(*boxes)]))


def page_bounds(parsed: synctex.Synctex, index: int) -> tuple[float, ...]:
    box = parsed.boxes[index]
    return tuple(parsed.points(value) for value in parsed.page_bounds(box, box.bounds))


def test_scaled_group_maps_inner_boxes_around_the_reference_point() -> None:
    row = hbox(100, 172, 120, 8, 2, synctex_record("g", 1, 5, 130, 172))
    parsed = parse_page(*scaled(100, 200, 0.5, (120, 40, 20), *row))
    outer, holder, rlap, natural, leaf = parsed.boxes[2:]
    assert set(parsed.transforms) == {rlap.index, natural.index, leaf.index}
    assert page_bounds(parsed, leaf.index) == pytest.approx((100, 182, 160, 187), abs=1e-3)
    assert page_bounds(parsed, natural.index) == pytest.approx(page_bounds(parsed, outer.index), abs=1e-3)
    assert page_bounds(parsed, holder.index) == pytest.approx((100, 180, 100, 210), abs=1e-3)
    assert parsed.points(parsed.page_extent(natural)) == pytest.approx(30, abs=1e-3)


@pytest.mark.parametrize(
    ("outer", "expected"),
    [
        ((20, 30, 100, 0), (300, 350, 330, 400)),
        ((10, 30, 0, 100), (310, 400, 340, 450)),
        ((100, 100, 10, 20), (270, 390, 320, 420)),
    ],
    ids=["90", "270", "180"],
)
def test_turned_group_maps_inner_boxes_by_quarter_turns(
    outer: tuple[float, float, float, float], expected: tuple[float, ...]
) -> None:
    parsed = parse_page(*turned(320, 400, outer, *hbox(320, 400, 50, 20, 10, synctex_record("g", 1, 5, 330, 400))))
    holder, turned_box, natural, leaf = parsed.boxes[2:]
    assert set(parsed.transforms) == {turned_box.index, natural.index, leaf.index}
    assert page_bounds(parsed, leaf.index) == pytest.approx(expected, abs=1e-3)
    assert page_bounds(parsed, turned_box.index) == pytest.approx(page_bounds(parsed, holder.index), abs=1e-3)


def test_scaled_group_inside_a_turned_group_applies_the_inner_transform_first() -> None:
    row = hbox(320, 400, 50, 20, 10, synctex_record("g", 1, 5, 330, 400))
    parsed = parse_page(*turned(320, 400, (20, 30, 100, 0), *scaled(320, 400, 0.5, (50, 20, 10), *row)))
    assert page_bounds(parsed, len(parsed.boxes) - 1) == pytest.approx((310, 375, 325, 400), abs=1e-3)


@pytest.mark.parametrize(
    "boxes",
    [
        hbox(100, 200, 120, 40, 20, *hbox(100, 200, 120, 40, 20, *hbox(100, 200, 120, 40, 20))),
        hbox(
            100, 200, 120, 40, 20, *hbox(100, 200, 0, 40, 20, *hbox(100, 200, 0, 40, 20, *hbox(100, 200, 60, 40, 20)))
        ),
        hbox(100, 200, 120, 0, 0, *hbox(100, 200, 0, 0, 0, *hbox(100, 200, 0, 40, 20, *hbox(100, 200, 60, 40, 20)))),
        hbox(100, 200, 30, 100, 0, *hbox(120, 200, 100, 20, 10, *hbox(120, 200, 90, 20, 10))),
        hbox(100, 200, 30, 90, 0, *hbox(120, 200, 100, 20, 10, *hbox(120, 200, 100, 20, 10))),
    ],
    ids=["nested", "rlap", "smash", "inner-differs", "outer-differs"],
)
def test_boxes_without_a_transform_keep_their_coordinates(boxes: list[str]) -> None:
    parsed = parse_page(*boxes)
    assert parsed.transforms == {}
    box = parsed.boxes[-1]
    assert parsed.page_bounds(box, box.bounds) == box.bounds
    assert parsed.page_extent(box) == box.extent
