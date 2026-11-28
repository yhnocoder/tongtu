from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pymupdf

from tongtu.artifacts.reconstruct import RegionKind
from tongtu.reconstruct.rebuild import MAIN_FILENAME, rebuild

WORDS = (
    "the attention function maps a query and a set of key value pairs to an output where the query keys values "
    "and output are all vectors computed"
).split()

SERIF = pymupdf.Font("tiro")

MONO = pymupdf.Font("cour")

CODE_SIZE = 9

CODE_LEADING = 11

LAYER = [
    "class Layer(Module):",
    "    def __init__(self, size):",
    "        super().__init__()",
    "        self.size = size",
    "",
    "    def forward(self, x):",
    "        return x * self.size",
]

NUMBER_MARK_RE = re.compile(r"\\llap\{\\fontsize\{[0-9.]+bp\}\{[0-9.]+bp\}\\selectfont\{\}(\d+)\\hspace\{[0-9.]+bp\}\}")

Draw = Callable[[pymupdf.Page, float], float]


def put(page: pymupdf.Page, x: float, y: float, text: str, font: pymupdf.Font, size: float, color=(0, 0, 0)) -> None:
    writer = pymupdf.TextWriter(page.rect)
    writer.append((x, y), text, font=font, fontsize=size)
    writer.write_text(page, color=color)


def body(page: pymupdf.Page, y: float, count: int, offset: int) -> float:
    for index in range(count):
        put(page, 72, y, " ".join(WORDS[(index * 3 + offset + k) % len(WORDS)] for k in range(13)), SERIF, 10)
        y += 12
    return y


def code(page: pymupdf.Page, x: float, y: float, lines: list[str], color=(0, 0, 0)) -> float:
    for line in lines:
        if line:
            put(page, x, y, line, MONO, CODE_SIZE, color)
        y += CODE_LEADING
    return y


def code_paper(folder: Path, draw: Draw) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = draw(page, body(page, 80, 12, 0) + 14)
    body(page, y + 30, 20, 5)
    path = folder / "code.pdf"
    doc.save(path)
    return path


def rebuilt(tmp_path: Path, draw: Draw) -> tuple[str, list[RegionKind]]:
    tree = tmp_path / "tree"
    tree.mkdir()
    regions = rebuild(code_paper(tmp_path, draw), tree)
    return (tree / MAIN_FILENAME).read_text(encoding="utf-8"), [region.kind for region in regions.regions]


def verbatim_lines(tex: str) -> list[str]:
    start = tex.index("\\begin{BVerbatim}")
    body = tex[tex.index("\n", start) + 1 : tex.index("\\end{BVerbatim}", start)]
    return body.rstrip("\n").split("\n")


def captioned_layer(page: pymupdf.Page, y: float) -> float:
    y = code(page, 90, y, LAYER) + 6
    put(page, 200, y, "Listing 1: A layer that scales its input.", SERIF, 10)
    return y


def test_indentation_is_rebuilt_from_the_coordinates(tmp_path: Path) -> None:
    tex, kinds = rebuilt(tmp_path, captioned_layer)
    assert kinds == []
    assert verbatim_lines(tex) == LAYER
    assert "\\usepackage{fancyvrb}" in tex
    assert "commandchars" not in tex
    assert "\\caption[]{Listing 1: A layer that scales its input.}" in tex


def test_line_numbers_are_set_left_of_the_code(tmp_path: Path) -> None:
    def numbered(page: pymupdf.Page, y: float) -> float:
        for index in range(4):
            put(page, 76, y + index * CODE_LEADING, str(7 + index), SERIF, 6)
        return code(page, 90, y, LAYER[:4])

    tex, kinds = rebuilt(tmp_path, numbered)
    assert kinds == []
    rows = verbatim_lines(tex)
    marks = [NUMBER_MARK_RE.match(row) for row in rows]
    assert [mark.group(1) for mark in marks if mark] == ["7", "8", "9", "10"]
    assert [row[mark.end() :] for row, mark in zip(rows, marks, strict=True) if mark] == [
        line.replace("{", "\\char123{}").replace("}", "\\char125{}") for line in LAYER[:4]
    ]


def test_side_by_side_code_with_a_caption_inside_the_box_is_split(tmp_path: Path) -> None:
    left = [
        "def forward(self, x):",
        "    h = self.conv(x)",
        "    h = self.norm(h)",
        "    h = self.drop(h)",
        "    return h",
    ]
    right = [
        "def backward(self, g):",
        "    d = self.grad(g)",
        "    d = self.clip(d)",
        "    d = self.scale(d)",
        "    return d",
    ]

    def two_columns(page: pymupdf.Page, y: float) -> float:
        page.draw_rect(pymupdf.Rect(85, y - 12, 460, y + 75), color=None, fill=(0.95, 0.95, 0.95))
        code(page, 90, y, left)
        y = code(page, 330, y, right) + 6
        put(page, 200, y, "Listing 2: Two methods.", SERIF, 10)
        return y + 10

    tex, kinds = rebuilt(tmp_path, two_columns)
    assert kinds == []
    assert tex.count("\\begin{minipage}") == 3
    assert "\\hfill\\begin{minipage}" in tex
    assert verbatim_lines(tex) == left
    assert verbatim_lines(tex[tex.index("\\hfill") :]) == right
    assert "\\caption[]{Listing 2: Two methods.}" in tex
    assert tex.index("\\end{BVerbatim}") < tex.index("\\caption[]")


def test_curly_quotes_become_straight_quotes(tmp_path: Path) -> None:
    def quoted(page: pymupdf.Page, y: float) -> float:
        return code(page, 90, y, ["print(“hello”)", "name = ‘world’", "value = a − b ∗ c"])

    tex, _ = rebuilt(tmp_path, quoted)
    assert verbatim_lines(tex) == ['print("hello")', "name = 'world'", "value = a - b * c"]


def test_coloured_code_uses_commandchars(tmp_path: Path) -> None:
    def coloured(page: pymupdf.Page, y: float) -> float:
        put(page, 90, y, "if", MONO, CODE_SIZE, (0, 0.5, 0))
        put(page, 90 + 3 * 0.6 * CODE_SIZE, y, "x: {y}", MONO, CODE_SIZE)
        return code(page, 90, y + CODE_LEADING, ["    return \\n", "pass"])

    tex, kinds = rebuilt(tmp_path, coloured)
    assert kinds == []
    assert "commandchars=\\\\\\{\\}" in tex
    assert "\\usepackage{xcolor}" in tex
    assert verbatim_lines(tex) == [
        "\\textcolor[HTML]{008000}{if} x: \\char123{}y\\char125{}",
        "    return \\char92{}n",
        "pass",
    ]


def test_mostly_proportional_region_stays_a_crop(tmp_path: Path) -> None:
    def mixed(page: pymupdf.Page, y: float) -> float:
        page.draw_rect(pymupdf.Rect(85, y - 12, 460, y + 40), color=None, fill=(0.95, 0.95, 0.95))
        code(page, 90, y, LAYER[:2])
        put(page, 90, y + 2 * CODE_LEADING, "Answer the question about the code above in plain words.", SERIF, 9)
        return y + 50

    tex, kinds = rebuilt(tmp_path, mixed)
    assert "BVerbatim" not in tex
    assert kinds == [RegionKind.FIGURE]


def test_region_with_an_image_stays_a_crop(tmp_path: Path) -> None:
    def with_image(page: pymupdf.Page, y: float) -> float:
        end = code(page, 90, y, LAYER)
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
        pixmap.set_rect(pixmap.irect, (200, 30, 30))
        page.insert_image(pymupdf.Rect(300, y - 10, 400, end - 10), pixmap=pixmap)
        return end

    tex, kinds = rebuilt(tmp_path, with_image)
    assert "BVerbatim" not in tex
    assert kinds == [RegionKind.FIGURE]


def test_algorithm_caption_stays_a_crop(tmp_path: Path) -> None:
    def algorithm(page: pymupdf.Page, y: float) -> float:
        put(page, 90, y, "Algorithm 1: Scale the input.", SERIF, 10)
        return code(page, 90, y + 16, LAYER)

    tex, kinds = rebuilt(tmp_path, algorithm)
    assert "BVerbatim" not in tex
    assert kinds == [RegionKind.ALGORITHM]
