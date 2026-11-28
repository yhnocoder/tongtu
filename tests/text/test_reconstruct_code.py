from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pymupdf

from tongtu.artifacts.reconstruct import RegionKind, RegionsFile
from tongtu.reconstruct.latex import code_font
from tongtu.reconstruct.layout import MONO_FONT_RE, Char, Line
from tongtu.reconstruct.rebuild import MAIN_FILENAME, rebuild

WORDS = (
    "the attention function maps a query and a set of key value pairs to an output where the query keys values "
    "and output are all vectors computed"
).split()

SERIF = pymupdf.Font("tiro")

BOLD_SERIF = pymupdf.Font("tibo")

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
    tex, regions = rebuilt_tree(tmp_path, code_paper(tmp_path, draw))
    return tex, [region.kind for region in regions.regions]


def rebuilt_tree(tmp_path: Path, pdf: Path) -> tuple[str, RegionsFile]:
    tree = tmp_path / "tree"
    tree.mkdir()
    regions = rebuild(pdf, tree)
    return (tree / MAIN_FILENAME).read_text(encoding="utf-8"), regions


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


def test_special_characters_stay_literal_without_commandchars(tmp_path: Path) -> None:
    lines = ['printf("%d\\n", a[0]); # $x & y_z', "if (~m ^ n) { return {}; }"]

    def special(page: pymupdf.Page, y: float) -> float:
        return code(page, 90, y, lines)

    tex, kinds = rebuilt(tmp_path, special)
    assert kinds == []
    assert "commandchars" not in tex
    assert verbatim_lines(tex) == lines


def test_code_with_a_caption_is_masked_in_the_order_check(tmp_path: Path) -> None:
    def captioned_box(page: pymupdf.Page, y: float) -> float:
        page.draw_rect(pymupdf.Rect(85, y - 12, 460, y + 75), color=None, fill=(0.95, 0.95, 0.95))
        y = code(page, 90, y, LAYER) + 6
        put(page, 200, y, "Listing 3: A layer.", SERIF, 10)
        return y + 10

    _, regions = rebuilt_tree(tmp_path, code_paper(tmp_path, captioned_box))
    kinds = sorted(line.kind for line in regions.floating)
    assert kinds == ["caption"] + ["code"] * len([line for line in LAYER if line])


def two_columns(page: pymupdf.Page, y: float) -> None:
    for column in (72, 320):
        line_y = y
        for index in range(40):
            put(page, column, line_y, " ".join(WORDS[(index * 3 + k) % len(WORDS)] for k in range(7)), SERIF, 10)
            line_y += 12


def test_code_at_the_top_of_a_two_column_page_stays_a_crop(tmp_path: Path) -> None:
    doc = pymupdf.open()
    two_columns(doc.new_page(width=612, height=792), 80)
    page = doc.new_page(width=612, height=792)
    put(page, 230, 80, "2 Implementation Details", BOLD_SERIF, 12)
    wide = ["result = compute_the_attention_scores(query_vectors, key_vectors, value_vectors)", *LAYER]
    two_columns(page, code(page, 90, 104, wide) + 14)
    path = tmp_path / "twocolumn.pdf"
    doc.save(path)
    tex, regions = rebuilt_tree(tmp_path, path)
    assert "\\twocolumn[" in tex
    assert "BVerbatim" not in tex
    assert [region.kind for region in regions.regions] == [RegionKind.DISPLAY]


def test_monospace_font_names() -> None:
    mono = ["SFTT0900", "Inconsolatazi4-Regular", "NimbusMonoPS-Regular", "Courier-Bold", "CMTT10", "txtt", "LMMono10"]
    proportional = ["Montserrat-Regular", "HarmoniaSans-Bold", "MonotypeCorsiva", "Barcode39", "NimbusRomNo9L-Regu"]
    assert [name for name in mono if MONO_FONT_RE.search(name)] == mono
    assert [name for name in proportional if MONO_FONT_RE.search(name)] == []


def code_line(fonts: list[str]) -> Line:
    chars = [
        Char("x", (10.0 * index, 0, 10.0 * index + 5, 9), (10.0 * index, 8), font, 9, False, False, mono=True)
        for index, font in enumerate(fonts)
    ]
    return Line(0, pymupdf.Rect(0, 0, 10 * len(fonts), 9), chars)


def test_courier_code_in_a_times_paper_uses_tex_gyre_cursor(tmp_path: Path) -> None:
    tex, kinds = rebuilt(tmp_path, captioned_layer)
    assert kinds == []
    assert "\\setmonofont{texgyrecursor}" in tex
    assert "\\newfontfamily\\codefontCursor{texgyrecursor}" in tex
    assert "formatcom=\\codefontCursor," in tex
    assert "codefontLatinModern" not in tex


def test_code_font_follows_the_majority_of_the_code_characters() -> None:
    typewriter = [code_line(["SFTT0900"] * 8 + ["NimbusMonL-Regu"] * 2), code_line(["CMTT10"] * 6)]
    assert code_font(typewriter).command == "codefontLatinModern"
    assert code_font(typewriter).advance == 0.525
    assert code_font([code_line(["NimbusMonL-Regu"] * 5 + ["CMTT10"])]).command == "codefontCursor"
    assert code_font([code_line(["Courier-Bold"] * 3)]).advance == 0.6
    assert code_font([code_line(["Inconsolatazi4-Regular"] * 3)]).command == "codefontInconsolata"
    assert code_font([code_line(["Menlo-Regular"] * 3)]).command == "codefontLatinModern"
