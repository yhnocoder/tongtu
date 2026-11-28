from __future__ import annotations

import dataclasses
import itertools
import re
import statistics
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

import pymupdf

from ..artifacts.reconstruct import RegionKind
from .layout import Char, Item, Layout, Line, visible

CODE_MONO_MIN = 0.85

CODE_MIN_CHARS = 10

CODE_LINE_MONO_MIN = 0.5

LINE_NUMBER_GAP = 0.4

COLUMN_GAP_UNITS = 2

SAME_BASELINE = 1.5

GRAPHIC_AREA_MAX = 0.3

BACKGROUND_FILL_MIN = 0.8

UNIT_SIZE_RATIO = 0.6

PLAIN_CHARS = str.maketrans({"’": "'", "‘": "'", "′": "'", "“": '"', "”": '"', "−": "-", "–": "-", "—": "-", "∗": "*"})

COMMAND_ESCAPES = str.maketrans({"\\": "\\char92{}", "{": "\\char123{}", "}": "\\char125{}"})

LINE_NUMBER_RE = re.compile(r"\d+:?")

OVERLAYS = {"7→": "↦"}

OVERLAY_WIDTH = 0.01

Fallback = Callable[[str], str | None]


@dataclass(frozen=True)
class Token:
    spaces: int
    text: str
    color: int
    bold: bool
    command: bool


@dataclass(frozen=True)
class Verbatim:
    tex: str
    colored: bool
    left: float


def x0(char: Char) -> float:
    return char.bbox[0]


def region_lines(layout: Layout, item: Item, rect: pymupdf.Rect) -> list[Line]:
    taken = {
        id(line)
        for other in layout.items
        if other.page == item.page
        for line in [*other.caption, *(other.lines if other is not item else [])]
    }
    lines = [
        line
        for line in layout.lines
        if line.page == item.page
        and line.kind not in ("header", "footer")
        and id(line) not in taken
        and rect.contains((line.bbox.tl + line.bbox.br) / 2)
    ]
    return sorted(lines, key=layout.baseline)


def split_number(chars: list[Char]) -> tuple[list[Char], list[Char]]:
    shown = sorted(visible(chars), key=x0)
    count = 0
    while count < len(shown) and shown[count].c.isdigit():
        count += 1
    if count and count < len(shown) and shown[count].c == ":":
        count += 1
    if not count or not LINE_NUMBER_RE.fullmatch("".join(char.c for char in shown[:count])):
        return [], shown
    number, rest = shown[:count], shown[count:]
    spaced = bool(rest) and x0(rest[0]) - number[-1].bbox[2] > LINE_NUMBER_GAP * number[-1].size
    if not all(char.mono for char in number) or spaced:
        return number, rest
    return [], shown


def mono_share(chars: list[Char]) -> float:
    return sum(char.mono for char in chars) / len(chars) if chars else 0.0


def has_graphics(page: pymupdf.Page, rect: pymupdf.Rect, text: pymupdf.Rect) -> bool:
    for image in page.get_images(full=True):
        if any(not (pymupdf.Rect(found) & rect).is_empty for found in page.get_image_rects(image[0])):
            return True
    for drawing in page.get_drawings():
        bounds = pymupdf.Rect(drawing["rect"])
        inside = bounds & rect
        if inside.is_empty or inside.height < 2 or inside.get_area() <= GRAPHIC_AREA_MAX * rect.get_area():
            continue
        fill = drawing.get("fill")
        light = fill is None or min(fill) >= BACKGROUND_FILL_MIN
        if light and (bounds + (-2, -2, 2, 2)).contains(text):
            continue
        return True
    return False


def is_code(layout: Layout, item: Item, kind: RegionKind, lines: list[Line], rect: pymupdf.Rect) -> bool:
    if item.kind == "refs" or kind is RegionKind.ALGORITHM:
        return False
    shown = [char for line in lines for char in visible(line.chars)]
    if len(shown) < CODE_MIN_CHARS:
        return False
    if mono_share([char for line in lines for char in split_number(line.chars)[1]]) < CODE_MONO_MIN:
        return False
    text = pymupdf.Rect(shown[0].bbox)
    for char in shown:
        text |= pymupdf.Rect(char.bbox)
    return not has_graphics(layout.doc[item.page], rect, text)


def char_unit(lines: list[Line]) -> float:
    advances = [
        right.bbox[0] - left.bbox[0]
        for line in lines
        for left, right in itertools.pairwise(sorted(line.chars, key=x0))
        if left.mono and right.mono and not left.c.isspace() and not right.c.isspace()
    ]
    advances = [advance for advance in advances if advance > 0]
    if advances:
        return statistics.median(advances)
    return UNIT_SIZE_RATIO * statistics.median(char.size for line in lines for char in visible(line.chars))


def sub_line(line: Line, chars: list[Char]) -> Line:
    shown = visible(chars)
    bbox = pymupdf.Rect(shown[0].bbox)
    for char in shown:
        bbox |= pymupdf.Rect(char.bbox)
    return dataclasses.replace(line, chars=chars, bbox=bbox, text="".join(char.c for char in chars).strip())


def column_lines(lines: list[Line], low: float, high: float) -> list[Line]:
    rows: list[tuple[float, Line, list[Char]]] = []
    for line in lines:
        chars = [char for char in line.chars if low <= (char.bbox[0] + char.bbox[2]) / 2 < high]
        if not visible(chars):
            continue
        level = baseline(visible(chars))
        if rows and abs(rows[-1][0] - level) < SAME_BASELINE:
            rows[-1][2].extend(chars)
        else:
            rows.append((level, line, chars))
    return [sub_line(line, sorted(chars, key=x0)) for _, line, chars in rows]


def gaps(spans: list[tuple[float, float]], width: float) -> list[tuple[float, float]]:
    ordered = sorted(spans)
    found: list[tuple[float, float]] = []
    reach = ordered[0][1]
    for left, right in ordered[1:]:
        if left > reach and left - reach >= width:
            found.append((reach, left))
        reach = max(reach, right)
    return found


def split_columns(lines: list[Line], unit: float) -> list[list[Line]]:
    shown = [char for line in lines for char in visible(line.chars)]
    spans = [(char.bbox[0], char.bbox[2]) for char in shown]
    cuts: list[float] = []
    for start, end in gaps([(char.bbox[0], char.bbox[2]) for char in shown if char.mono], COLUMN_GAP_UNITS * unit):
        inside = [(max(left, start), min(right, end)) for left, right in spans if left < end and right > start]
        free = gaps([(start, start), *inside, (end, end)], 0)
        if free:
            low, high = max(free, key=lambda gap: gap[1] - gap[0])
            cuts.append((low + high) / 2)
    bounds = [-float("inf"), *cuts, float("inf")]
    kept = [bounds[0]]
    for cut, following in zip(cuts, bounds[2:], strict=True):
        left = column_lines(lines, kept[-1], cut)
        right = column_lines(lines, cut, following)
        if side_by_side(left, right):
            kept.append(cut)
    kept.append(bounds[-1])
    return [column_lines(lines, low, high) for low, high in itertools.pairwise(kept)]


def side_by_side(left: list[Line], right: list[Line]) -> bool:
    lefts = [baseline(visible(line.chars)) for line in left if split_number(line.chars)[1]]
    rights = [baseline(visible(line.chars)) for line in right]
    return bool(lefts and rights) and min(lefts) <= max(rights) and min(rights) <= max(lefts)


def is_code_line(line: Line) -> bool:
    number, rest = split_number(line.chars)
    return mono_share(rest) >= CODE_LINE_MONO_MIN or (bool(number) and not rest)


def segments(lines: list[Line]) -> list[tuple[bool, list[Line]]]:
    return [(code, list(group)) for code, group in itertools.groupby(lines, key=is_code_line)]


def plain(text: str) -> str:
    return unicodedata.normalize("NFKC", text.translate(PLAIN_CHARS))


def overlaid(row: list[Char]) -> list[tuple[Char, Char, str]]:
    out: list[tuple[Char, Char, str]] = []
    index = 0
    while index < len(row):
        char = row[index]
        following = row[index + 1] if index + 1 < len(row) else None
        stacked = following is not None and char.bbox[2] - char.bbox[0] < OVERLAY_WIDTH
        if stacked and char.c + following.c in OVERLAYS:
            out.append((char, following, OVERLAYS[char.c + following.c]))
            index += 2
        elif stacked and unicodedata.combining(char.c):
            out.append((char, following, unicodedata.normalize("NFC", plain(following.c) + char.c)))
            index += 2
        else:
            out.append((char, char, plain(char.c)))
            index += 1
    return out


def baseline(chars: list[Char]) -> float:
    return statistics.median(char.origin[1] for char in chars)


def styled_text(tokens: list[Token]) -> str:
    out = []
    for (color, bold), group in itertools.groupby(tokens, key=lambda token: (token.color, token.bold)):
        text = "".join(
            " " * token.spaces + (token.text if token.command else token.text.translate(COMMAND_ESCAPES))
            for token in group
        )
        if bold:
            text = f"\\textbf{{{text}}}"
        if color:
            text = f"\\textcolor[HTML]{{{color:06X}}}{{{text}}}"
        out.append(text)
    return "".join(out)


def verbatim(lines: list[Line], fallback: Fallback, advance: float) -> Verbatim:
    splits = [split_number(line.chars) for line in lines]
    numbers = [int("".join(char.c for char in number).rstrip(":")) if number else None for number, _ in splits]
    first = numbers[0]
    numbered = first is not None and numbers == list(range(first, first + len(numbers)))
    rows = [rest if numbered else sorted([*number, *rest], key=x0) for number, rest in splits]
    shown = [char for row in rows for char in row]
    unit = char_unit(lines)
    left = min(x0(char) for char in shown) if shown else min(line.bbox.x0 for line in lines)
    size = statistics.median(char.size for line in lines for char in visible(line.chars))
    baselines = [baseline(visible(line.chars)) for line in lines]
    steps = [lower - upper for upper, lower in itertools.pairwise(baselines)]
    gap = statistics.median(steps) if steps else 0.0
    table: list[list[Token]] = []
    for index, row in enumerate(rows):
        if index and gap > 0 and not numbered:
            table += [[] for _ in range(round(steps[index - 1] / gap) - 1)]
        tokens: list[Token] = []
        previous: Char | None = None
        for char, last, text in overlaid(row):
            space = min(x0(char), x0(last)) - (left if previous is None else previous.bbox[2])
            special = fallback(text) if len(text) == 1 else None
            tokens.append(Token(max(0, round(space / unit)), special or text, last.color, last.bold, bool(special)))
            previous = last
        table.append(tokens)
    styled = numbered or any(token.color or token.bold or token.command for tokens in table for token in tokens)
    texts = [
        styled_text(tokens) if styled else "".join(" " * token.spaces + token.text for token in tokens)
        for tokens in table
    ]
    width = max(
        1, *(sum(token.spaces + (1 if token.command else len(token.text)) for token in tokens) for tokens in table)
    )
    options = [f"fontsize=\\fontsize{{{size:.2f}bp}}{{{size:.2f}bp}}", "baseline=t"]
    scale = unit / (advance * size)
    if gap > 0:
        options.append(f"baselinestretch={gap / size / scale:.3f}")
    colored = any(token.color for tokens in table for token in tokens)
    if numbered:
        sep = (left - max(number[-1].bbox[2] for number, _ in splits)) / scale
        texts = [number_mark(number, sep, scale) + text for (number, _), text in zip(splits, texts, strict=True)]
        colored = colored or any(char.color for number, _ in splits for char in number)
    if styled:
        options.append("commandchars=\\\\\\{\\}")
    tex = (
        "\\begin{lrbox}{\\codebox}\\begin{BVerbatim}["
        + ",".join(options)
        + "]\n"
        + "\n".join(texts)
        + "\n\\end{BVerbatim}\n\\end{lrbox}"
        + f"\\resizebox{{{width * unit:.1f}bp}}{{!}}{{\\usebox{{\\codebox}}}}"
    )
    return Verbatim(tex, colored, left)


def number_mark(number: list[Char], sep: float, scale: float) -> str:
    size = number[-1].size / scale
    text = f"\\fontsize{{{size:.2f}bp}}{{{size:.2f}bp}}\\selectfont{{}}" + "".join(char.c for char in number)
    if number[-1].color:
        text = f"\\textcolor[HTML]{{{number[-1].color:06X}}}{{{text}}}"
    return f"\\llap{{{text}\\hspace{{{sep:.1f}bp}}}}"
