from __future__ import annotations

import collections
import re
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from pydantic import BaseModel, RootModel

from .. import processes
from ..artifacts.reconstruct import RegionKind, RegionsFile
from ..compiling import COMPILE_TIMEOUT_SECONDS
from ..manifests import describe_error, write_manifest
from .crops import CROPS_DIRNAME, crop_path, open_crop
from .latex import CJK_FONT
from .recognition import (
    FONTSPEC_PACKAGE,
    HINTS_FILENAME,
    IMAGES_DIRNAME,
    SKIP,
    copy_crops,
    image_path,
    verify_each,
    with_packages,
)

TABLE_CROP_RE = re.compile(r"\\includegraphics\{" + CROPS_DIRNAME + r"/(?P<key>r-\d+)\.pdf\}")

PREAMBLE = (
    r"\documentclass{article}\usepackage[paperwidth=80cm,paperheight=80cm,margin=1cm]{geometry}"
    r"\usepackage{amsmath,amssymb,bm,booktabs,multirow,makecell,array,graphicx,pifont}\usepackage[table]{xcolor}"
    r"\usepackage{fontspec}\defaultfontfeatures{Ligatures=NoCommon}\setmainfont{lmroman10-regular.otf}"
    r"[BoldFont=lmroman10-bold.otf,ItalicFont=lmroman10-italic.otf,BoldItalicFont=lmroman10-bolditalic.otf]"
    r"\pagestyle{empty}"
)

CJK_PACKAGE = r"\usepackage{xeCJK}\setCJKmainfont{" + CJK_FONT + "}"

COMPILE_COMMAND: tuple[str, ...] = ("xelatex", "-interaction=nonstopmode", "-halt-on-error", "f.tex")

FORBIDDEN_RE = re.compile(
    r"\\(raisebox|hspace|vspace|kern|mkern|phantom|hphantom|vphantom|rlap|llap|mathrlap|mathllap|resizebox"
    r"|scalebox|lower|raise)(?![A-Za-z])"
)

CJK_RE = re.compile(r"[\u2e80-\u9fff]")

QUOTES = str.maketrans({"’": "'", "′": "'", "‘": "'", "“": '"', "”": '"', "∗": "*", "−": "-", "–": "-", "—": "-"})

BOLD_RE = re.compile(r"(MIB|CMBX|CMBSY|BSY|bold|-Bd|Semibold|Medi|Black(?!board)|txb|pxb)", re.I)

EXTENSION_FONT_RE = re.compile(r"cmex|mathex|lmex", re.I)

CMEX: dict[str, str] = {"P": "∑", "X": "∑", "Q": "∏", "Y": "∏", "R": "∫", "Z": "∫"}

DINGBATS_FONT = "Dingbats"

DINGBATS_BASE = 0x2700

SQRT = "√"

LIGATURE_RE = re.compile("f[il]")

IGNORED_CHARS = "-\u2010\u00ad"

ERROR_LINE_RE = re.compile(r"^!.*", re.M)

HORIZONTAL = 0.01

SAME_ROW = 0.35

SMALLER_SIZE = 0.85

SMALLER_SAME_ROW = 0.6

CELL_GAP = 0.18

ABOVE_GAP = 0.8

SAME_LINE = 0.3

FAR_APART = 1.5

DIFFERENCE_WORDS = 20

DIFFERENCE_CHARS = 60

LAYOUT_PROBLEMS = 10

STYLE_CELLS = 20

IMAGE_ZOOM_RANGE = (2, 4)

IMAGE_TARGET_WIDTH = 1400


@dataclass(frozen=True)
class Glyph:
    char: str
    bold: bool
    color: int
    x: float
    x2: float
    y: float
    size: float
    rotation: int | None = None


@dataclass(frozen=True)
class Cell:
    text: str
    cx: float
    cy: float
    y: float
    x: float
    size: float
    glyphs: tuple[Glyph, ...]


@dataclass
class TableComparison:
    text: bool = True
    missing_words: list[str] = field(default_factory=list)
    extra_words: list[str] = field(default_factory=list)
    missing_chars: str = ""
    extra_chars: str = ""
    layout: list[str] = field(default_factory=list)
    bold_cells: list[str] = field(default_factory=list)
    color_cells: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.text and not self.layout and not self.bold_cells and not self.color_cells


@dataclass(frozen=True)
class TableVerdict:
    passed: bool
    latex: str
    problems: tuple[str, ...]


class TableHint(BaseModel):
    width_bp: float
    spans_y_x: list[str]


class TableHints(RootModel[dict[str, TableHint]]):
    pass


def normalized(char: str) -> str:
    char = unicodedata.normalize("NFKC", char.translate(QUOTES))
    return "".join(c for c in char if not c.isspace() and unicodedata.category(c)[0] not in "ZC")


def page_glyphs(page: pymupdf.Page) -> list[Glyph]:
    glyphs = []
    line_number = 0
    for block in page.get_text("rawdict", clip=page.rect)["blocks"]:
        for line in block.get("lines", []):
            line_number += 1
            rotation = None if abs(line["dir"][1]) < HORIZONTAL else line_number
            for span in line["spans"]:
                font = span["font"]
                extension = bool(EXTENSION_FONT_RE.search(font))
                bold = bool(BOLD_RE.search(font.replace("BlackboardBold", "").replace("Blackboard", "")))
                for char in span["chars"]:
                    if not pymupdf.Rect(char["bbox"]).intersects(page.rect):
                        continue
                    text = char["c"]
                    if extension:
                        text = text if text in CMEX.values() else CMEX.get(text, "")
                    if font == DINGBATS_FONT and text and ord(text[0]) < 0x100:
                        text = chr(DINGBATS_BASE + ord(text[0]))
                    if text == SQRT:
                        text = ""
                    glyphs += [
                        Glyph(
                            piece,
                            bold,
                            span["color"],
                            char["origin"][0],
                            char["bbox"][2],
                            char["origin"][1],
                            span["size"],
                            rotation,
                        )
                        for piece in normalized(text)
                    ]
    return glyphs


def rows(glyphs: Sequence[Glyph]) -> list[list[Glyph]]:
    found: list[tuple[float, float, list[Glyph]]] = []
    for glyph in sorted(glyphs, key=lambda glyph: glyph.y):
        for y, size, members in found:
            if abs(y - glyph.y) < SAME_ROW * max(glyph.size, size) or (
                glyph.size < size * SMALLER_SIZE and abs(y - glyph.y) < SMALLER_SAME_ROW * size
            ):
                members.append(glyph)
                break
        else:
            found.append((glyph.y, glyph.size, [glyph]))
    return [sorted(members, key=lambda glyph: glyph.x) for _, _, members in found]


def cells(glyphs: Sequence[Glyph]) -> list[list[Glyph]]:
    rotated: dict[int, list[Glyph]] = {}
    for glyph in glyphs:
        if glyph.rotation is not None:
            rotated.setdefault(glyph.rotation, []).append(glyph)
    found = list(rotated.values())
    for row in rows([glyph for glyph in glyphs if glyph.rotation is None]):
        current: list[Glyph] = []
        for glyph in row:
            if current and glyph.x - current[-1].x2 > CELL_GAP * max(glyph.size, current[-1].size):
                found.append(current)
                current = []
            current.append(glyph)
        if current:
            found.append(current)
    return found


def cell_infos(groups: Sequence[Sequence[Glyph]]) -> list[Cell]:
    if not groups:
        return []
    xs = [glyph.x for group in groups for glyph in group]
    ys = [glyph.y for group in groups for glyph in group]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    infos = []
    for group in groups:
        mean_x = sum(glyph.x for glyph in group) / len(group)
        mean_y = sum(glyph.y for glyph in group) / len(group)
        infos.append(
            Cell(
                text=LIGATURE_RE.sub("f", "".join(glyph.char for glyph in group).casefold()),
                cx=(mean_x - x0) / max(1, x1 - x0),
                cy=(mean_y - y0) / max(1, y1 - y0),
                y=mean_y,
                x=group[0].x,
                size=max(glyph.size for glyph in group),
                glyphs=tuple(group),
            )
        )
    return infos


def char_counts(groups: Sequence[Sequence[Glyph]]) -> collections.Counter[str]:
    return collections.Counter(
        char
        for group in groups
        for glyph in group
        for char in unicodedata.normalize("NFKC", glyph.char).casefold()
        if not char.isspace() and char not in IGNORED_CHARS
    )


def matched_cells(
    original: Sequence[Cell], rendered: Sequence[Cell], shared: collections.Counter[str]
) -> dict[int, int]:
    match: dict[int, int] = {}
    for text in shared:
        left = [index for index, cell in enumerate(original) if cell.text == text]
        right = [index for index, cell in enumerate(rendered) if cell.text == text]
        pairs = sorted(
            (abs(original[i].cx - rendered[j].cx) + abs(original[i].cy - rendered[j].cy), i, j)
            for i in left
            for j in right
        )
        used_left: set[int] = set()
        used_right: set[int] = set()
        for _, i, j in pairs:
            if i in used_left or j in used_right or len(used_left) >= shared[text]:
                continue
            match[i] = j
            used_left.add(i)
            used_right.add(j)
    return match


def layout_problems(original: Sequence[Cell], rendered: Sequence[Cell], match: Mapping[int, int]) -> list[str]:
    problems = []
    for p in match:
        for q in match:
            if p == q:
                continue
            first, second = original[p], original[q]
            first_rendered, second_rendered = rendered[match[p]], rendered[match[q]]
            size = max(first.size, second.size)
            rendered_size = max(first_rendered.size, second_rendered.size)
            same_row = abs(first.y - second.y) < SAME_LINE * size
            rendered_same_row = abs(first_rendered.y - second_rendered.y) < SAME_LINE * rendered_size
            if (
                second.y - first.y > ABOVE_GAP * size
                and first_rendered.y - second_rendered.y > SAME_LINE * rendered_size
            ):
                problems.append(f"'{first.text}' is above '{second.text}' in the original but below it in yours")
            if same_row and second.x > first.x and rendered_same_row and first_rendered.x > second_rendered.x:
                problems.append(f"'{first.text}' is left of '{second.text}' in the original but right of it in yours")
            if (
                same_row
                and abs(first_rendered.y - second_rendered.y) > FAR_APART * rendered_size
                and abs(first.x - second.x) > 0
            ):
                problems.append(
                    f"'{first.text}' and '{second.text}' are on the same row in the original "
                    "but far apart vertically in yours"
                )
    return problems


def compare(original: Sequence[Glyph], rendered: Sequence[Glyph]) -> TableComparison:
    original_groups, rendered_groups = cells(original), cells(rendered)
    original_cells, rendered_cells = cell_infos(original_groups), cell_infos(rendered_groups)
    original_words = collections.Counter(cell.text for cell in original_cells)
    rendered_words = collections.Counter(cell.text for cell in rendered_cells)
    result = TableComparison()
    if original_words != rendered_words:
        result.missing_words = list((original_words - rendered_words).elements())[:DIFFERENCE_WORDS]
        result.extra_words = list((rendered_words - original_words).elements())[:DIFFERENCE_WORDS]
        original_chars, rendered_chars = char_counts(original_groups), char_counts(rendered_groups)
        if original_chars != rendered_chars:
            result.missing_chars = "".join((original_chars - rendered_chars).elements())[:DIFFERENCE_CHARS]
            result.extra_chars = "".join((rendered_chars - original_chars).elements())[:DIFFERENCE_CHARS]
            result.text = False
            return result
    shared = original_words & rendered_words
    original_cells = [cell for cell in original_cells if cell.text in shared]
    rendered_cells = [cell for cell in rendered_cells if cell.text in shared]
    match = matched_cells(original_cells, rendered_cells, shared)
    result.layout = layout_problems(original_cells, rendered_cells, match)[:LAYOUT_PROBLEMS]
    if result.layout:
        return result
    bold: set[str] = set()
    color: set[str] = set()
    for p, q in match.items():
        for a, b in zip(original_cells[p].glyphs, rendered_cells[q].glyphs, strict=False):
            if a.bold != b.bold:
                bold.add(original_cells[p].text)
            if a.color != b.color:
                color.add(original_cells[p].text)
    result.bold_cells = sorted(bold)[:STYLE_CELLS]
    result.color_cells = sorted(color)[:STYLE_CELLS]
    return result


def comparison_problems(comparison: TableComparison) -> list[str]:
    problems = []
    if not comparison.text:
        if comparison.missing_chars or comparison.extra_chars:
            problems.append(
                f"Characters in the original but missing from yours: {comparison.missing_chars!r}. "
                f"Characters in yours but not in the original: {comparison.extra_chars!r}."
            )
        problems.append(
            f"Words differing (original-only: {comparison.missing_words}; yours-only: {comparison.extra_words})."
        )
    if comparison.layout:
        problems.append("Layout differences: " + "; ".join(comparison.layout))
    if comparison.bold_cells:
        problems.append("Bold differs for: " + ", ".join(comparison.bold_cells))
    if comparison.color_cells:
        problems.append("Colour differs for: " + ", ".join(comparison.color_cells))
    return problems


def rendered_glyphs(latex: str) -> list[Glyph] | str:
    cjk = CJK_PACKAGE if CJK_RE.search(latex) else ""
    with tempfile.TemporaryDirectory(prefix="tongtu-table-") as temporary:
        folder = Path(temporary)
        source = PREAMBLE + cjk + r"\begin{document}" + latex + r"\end{document}"
        (folder / "f.tex").write_text(source, encoding="utf-8")
        try:
            outcome = processes.run_in_process_group(list(COMPILE_COMMAND), folder, COMPILE_TIMEOUT_SECONDS)
        except OSError as error:
            return f"failed to run {COMPILE_COMMAND[0]} ({describe_error(error)})"
        pdf = folder / "f.pdf"
        if outcome.returncode or outcome.timed_out or not pdf.is_file():
            errors = ERROR_LINE_RE.findall(outcome.stdout.decode("utf-8", errors="replace"))
            return "compile: " + (errors[0] if errors else "?")
        return page_glyphs(pymupdf.open(pdf)[0])


def verify(crop: Path, latex: str) -> TableVerdict:
    if latex.strip() == SKIP:
        return TableVerdict(False, latex, (SKIP,))
    forbidden = FORBIDDEN_RE.search(latex)
    if forbidden is not None:
        return TableVerdict(False, latex, (f"Your LaTeX failed: forbidden command: {forbidden.group(0)}",))
    original = page_glyphs(open_crop(crop)[0])
    if not original:
        return TableVerdict(False, latex, ("the crop has no text layer to compare with; it stays an image.",))
    rendered = rendered_glyphs(latex)
    if isinstance(rendered, str):
        return TableVerdict(False, latex, (f"Your LaTeX failed: {rendered}",))
    comparison = compare(original, rendered)
    return TableVerdict(comparison.passed, latex, tuple(comparison_problems(comparison)))


def recognizable(tex: str, regions: RegionsFile) -> list[str]:
    tables = {region.id for region in regions.regions if region.kind is RegionKind.TABLE}
    return [key for key in dict.fromkeys(match.group("key") for match in TABLE_CROP_RE.finditer(tex)) if key in tables]


def span_hints(page: pymupdf.Page) -> list[str]:
    found = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = "".join(char["c"] for char in span["chars"])
                if text.strip():
                    origin_x, origin_y = span["origin"]
                    found.append((round(origin_y, 1), round(origin_x, 1), f"{span['font']}#{span['color']:06X}:{text}"))
    return [f"y={y} x={x} {span}" for y, x, span in sorted(found)]


def prepare(tree: Path, session_dir: Path, keys: Sequence[str]) -> None:
    (session_dir / IMAGES_DIRNAME).mkdir(parents=True, exist_ok=True)
    copy_crops(tree, session_dir, keys)
    hints = {}
    low, high = IMAGE_ZOOM_RANGE
    for key in keys:
        page = open_crop(tree / crop_path(key))[0]
        zoom = max(low, min(high, IMAGE_TARGET_WIDTH / page.rect.width))
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(image_path(session_dir, key))
        hints[key] = TableHint(width_bp=round(page.rect.width, 1), spans_y_x=span_hints(page))
    write_manifest(session_dir / HINTS_FILENAME, TableHints(hints))


def verify_all(tree: Path, transcriptions: Mapping[str, str], keys: Sequence[str]) -> dict[str, TableVerdict]:
    def one(key: str) -> TableVerdict:
        if key not in transcriptions:
            return TableVerdict(False, "", ("no transcription in rec.json",))
        return verify(tree / crop_path(key), transcriptions[key])

    return verify_each(keys, one)


def write_back(tex: str, verdicts: Mapping[str, TableVerdict], tree: Path) -> tuple[str, int]:
    accepted = {key: verdict.latex.strip() for key, verdict in verdicts.items() if verdict.passed}
    written = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal written
        key = match.group("key")
        if key not in accepted:
            return match.group(0)
        written += 1
        width = open_crop(tree / crop_path(key))[0].rect.width
        return rf"\resizebox{{{width:.1f}bp}}{{!}}{{{accepted[key]}}}"

    tex = TABLE_CROP_RE.sub(replace, tex)
    if not written:
        return tex, 0
    tex = with_packages(tex)
    if CJK_RE.search("".join(accepted.values())) and "xeCJK" not in tex:
        tex = tex.replace(FONTSPEC_PACKAGE, FONTSPEC_PACKAGE + CJK_PACKAGE, 1)
    return tex, written


def failure_lines(verdicts: Mapping[str, TableVerdict]) -> list[str]:
    passed = sum(verdict.passed for verdict in verdicts.values())
    lines = [f"passed {passed}/{len(verdicts)}"]
    lines += [
        f"{key}: " + " ".join(verdict.problems)
        for key, verdict in verdicts.items()
        if not verdict.passed and verdict.problems != (SKIP,)
    ]
    skipped = [key for key, verdict in verdicts.items() if verdict.problems == (SKIP,)]
    if skipped:
        lines.append(f"SKIP {len(skipped)}: {' '.join(skipped)}")
    return lines
