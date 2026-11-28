import re
import statistics
from dataclasses import dataclass
import pymupdf
from tongtu.reconstruct import table

table.EXTENSION_FONT_RE = re.compile(r"cmex|mathex|lmex|txex|pxex|stixex", re.I)
table.CMEX = {}

NUMBER_RE = re.compile(r"^\d+:?")
NUMBER_GAP = 0.4
SMALL = 0.85
ATTACH = 0.9
NUMBER_SIZE = 0.9
HEAD_RE = re.compile(r"^[A-Z][A-Za-z]{1,20}:")


def number_width(row, size):
    match = NUMBER_RE.match("".join(glyph.char for glyph in row))
    if not match or match.end() >= len(row):
        return 0
    last, following = row[match.end() - 1], row[match.end()]
    if match.group(0).endswith(":") or last.size < NUMBER_SIZE * size or following.x - last.x2 > NUMBER_GAP * size:
        return match.end()
    return 0


@dataclass
class Char:
    char: str
    bold: bool
    dy: float
    size: float


@dataclass
class Line:
    number: str
    x: float
    size: float
    chars: list[Char]

    @property
    def text(self) -> str:
        return "".join(char.char for char in self.chars)


def rows(page: pymupdf.Page) -> list[list[table.Glyph]]:
    found = table.page_glyphs(page)
    if not found:
        return []
    median = statistics.median(glyph.size for glyph in found)
    main = [glyph for glyph in found if glyph.size >= SMALL * median]
    small = [glyph for glyph in found if glyph.size < SMALL * median]
    lines = table.rows(main)
    bases = [statistics.median(glyph.y for glyph in line) for line in lines]
    rest = []
    for glyph in small:
        distance, index = min(((abs(glyph.y - base), index) for index, base in enumerate(bases)), default=(None, None))
        if distance is not None and distance < ATTACH * median:
            lines[index].append(glyph)
        else:
            rest.append(glyph)
    lines += table.rows(rest)
    lines = [sorted(line, key=lambda glyph: glyph.x) for line in lines if line]
    return sorted(lines, key=lambda line: statistics.median(glyph.y for glyph in line))


def lines(page: pymupdf.Page, kind: str) -> list[Line]:
    every = rows(page)
    if not every:
        return []
    block = statistics.median(glyph.size for row in every for glyph in row)
    widths = [number_width(row, statistics.median(glyph.size for glyph in row)) for row in every]
    numbered = kind == "algorithm" and any(widths)
    found: list[Line] = []
    seen_number = False
    for row, width in zip(every, widths):
        size = statistics.median(glyph.size for glyph in row)
        main = [glyph.y for glyph in row if glyph.size >= NUMBER_SIZE * size] or [glyph.y for glyph in row]
        baseline = statistics.median(main)
        chars = [Char(glyph.char, glyph.bold, (glyph.y - baseline) / block, glyph.size / block) for glyph in row[width:]]
        seen_number = seen_number or bool(width)
        text = "".join(char.char for char in chars)
        if numbered and found and not width and (seen_number or not HEAD_RE.match(text)):
            found[-1].chars.extend(chars)
        else:
            number = "".join(glyph.char for glyph in row[:width])
            found.append(Line(number, row[width].x, size, chars))
    return found


def text_lines(page: pymupdf.Page) -> list[str]:
    found = []
    for row in rows(page):
        width = number_width(row, statistics.median(glyph.size for glyph in row))
        found.append(("".join(glyph.char for glyph in row[:width]), row[width:]))
    widths = [glyph.x2 - glyph.x for _, row in found for glyph in row if glyph.x2 > glyph.x]
    unit = statistics.median(widths) if widths else 5.0
    left = min((row[0].x for _, row in found), default=0.0)
    digits = max((len(number) for number, _ in found), default=0)
    lines = []
    for number, row in found:
        line = (number.rjust(digits) + "  " if digits else "") + " " * round((row[0].x - left) / unit)
        previous = None
        for glyph in row:
            if previous is not None:
                line += " " * max(0, round((glyph.x - previous.x2) / unit))
            line += glyph.char
            previous = glyph
        lines.append(line)
    return lines
