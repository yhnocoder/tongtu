from __future__ import annotations

import collections
import re
import statistics
from dataclasses import dataclass, field

import pymupdf

MATH_FONT_RE = re.compile(
    r"^(CMMI|CMSY|CMEX|CMBSY|CMMIB|MSBM|MSAM|LMMath|EUFM|EUSM|RSFS|STIXMath|CMR\d|CMBX\d|txsy|txmi|txex|rtxmi|MnSymbol|Latin Modern Math)",
    re.I,
)

CAPTION_RE = re.compile(
    r"^((Figure|Fig\.|FIG\.|Table|TABLE|Algorithm|Listing)\s*([A-Z]?\d+|[IVX]+(?=\.))(\s*[:.|]|\s+\||\s+[A-Z][a-z])|Algorithm\s*[A-Z]?\d+\s+[A-Z])"
)

REF_HEAD_RE = re.compile(r"^(\d+\s*)?(references|bibliography)$", re.I)

NUM_HEAD_RE = re.compile(r"^(\d+(\.\d+)*\.?|[A-Z](\.\d+)+|[A-Z]\.|[IVX]+\.)\s+[A-Za-z]")

PROSE_RE = re.compile(r"[a-z]{3,} [a-z]{2,} [a-z]{3,}")

LONG_PROSE_RE = re.compile(r"[a-z]{3,} [a-z]{3,} [a-z]{3,}")

DELIMITER_FONT_RE = re.compile(r"CMEX|Extension|-EX$|txex|pxex", re.I)

MONO_FONT_RE = re.compile(r"Mono|Courier|Typewriter|Code|CMTT|SFTT")

FOOTNOTE_MONO_FONT_RE = re.compile(r"Mono|Courier|Typewriter|CMTT|SFTT")

THEOREM_RE = re.compile(r"^(Theorem|Lemma|Corollary|Proposition|Definition|Proof|Remark|Example)\b")

FOOTNOTE_MARKS = "*†‡§¶∗"

TEXT_FLAGS = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_LIGATURES

HEAVY_PAGE_DRAWINGS = 20000

TITLE_SIZE_MARGIN = 0.6

Box = tuple[float, float, float, float]


@dataclass
class Char:
    c: str
    bbox: Box
    origin: tuple[float, float]
    font: str
    size: float
    bold: bool
    italic: bool
    math: bool = False
    line: Line | None = field(default=None, compare=False, repr=False)


@dataclass
class Line:
    page: int
    bbox: pymupdf.Rect
    chars: list[Char]
    kind: str = ""
    col: int = 0
    text: str = ""
    size: float = 0
    font: str = ""
    algo_bottom: float | None = field(default=None, compare=False)


@dataclass
class Item:
    kind: str
    page: int
    col: int
    rect: pymupdf.Rect
    lines: list[Line] = field(default_factory=list)
    caption: list[Line] = field(default_factory=list)
    wide: bool = False
    caption_above: bool = field(default=False, compare=False)
    wrap: tuple[str, float] | None = field(default=None, compare=False)


@dataclass
class Wrap:
    graphic: pymupdf.Rect
    captions: list[Line]
    side: str
    zone: pymupdf.Rect
    col: int


def family(font: str) -> str:
    return re.split(r"[-+,]", font.split("+")[-1])[0].rstrip("0123456789")


def visible(chars: list[Char]) -> list[Char]:
    return [char for char in chars if not char.c.isspace()]


def caption_distance(region: Item, caption: Item) -> float:
    return min(abs(region.rect.y1 - caption.rect.y0), abs(caption.rect.y1 - region.rect.y0))


def center_y(rect: pymupdf.Rect) -> float:
    return (rect.y0 + rect.y1) / 2


class Layout:
    def __init__(self, doc: pymupdf.Document) -> None:
        self.doc = doc
        self.lines: list[Line] = []
        self.items: list[Item] = []
        self.heavy: set[int] = set()
        self.page_boxes: dict[int, list[pymupdf.Rect]] = {}
        self.onecol_pages: set[int] = set()
        self.wraps: dict[int, list[Wrap]] = {}
        self.table_spans: dict[int, list[tuple[int, float, float]]] = {}
        self.running: set[int] = set()
        self.text_families: set[str] = set()
        self.body_family = ""
        self.body_size = 0.0
        self.indented = False
        self.parindent = 0.0
        self.parskip = 0.0

    def analyze(self) -> None:
        self.load()
        self.geometry()
        self.classify()
        self.join_split_paragraphs()
        self.paragraph_style()

    def load(self) -> None:
        counts: collections.Counter[tuple[str, float]] = collections.Counter()
        for page_number, page in enumerate(self.doc):
            raw = page.get_text("rawdict", flags=TEXT_FLAGS)
            for block in raw["blocks"]:
                if block["type"] != 0:
                    continue
                for raw_line in block["lines"]:
                    if abs(raw_line["dir"][0] - 1) > 1e-3:
                        continue
                    chars = self.line_chars(raw_line, counts)
                    if not visible(chars):
                        continue
                    rect = pymupdf.Rect(chars[0].bbox)
                    for char in visible(chars):
                        rect |= pymupdf.Rect(char.bbox)
                    self.lines.append(Line(page_number, rect, chars))
        (self.body_family, self.body_size), _ = counts.most_common(1)[0]
        text_families = {self.body_family}
        for (font_family, _size), count in counts.items():
            math_family = MATH_FONT_RE.match(font_family) or (
                MATH_FONT_RE.match(font_family + "10") and not re.match(r"(CM|LM|SF)", self.body_family, re.I)
            )
            if not math_family and count > 50:
                text_families.add(font_family)
        self.text_families = text_families
        for line in self.lines:
            for char in line.chars:
                char.math = (
                    bool(MATH_FONT_RE.match(char.font))
                    and not char.c.isspace()
                    and family(char.font) not in text_families
                ) or 0x1D400 <= ord(char.c[:1] or " ") <= 0x1D7FF
            shown = visible(line.chars)
            body_chars = [char for char in shown if family(char.font) == self.body_family]
            sized = body_chars if len(body_chars) >= 0.25 * len(shown) else shown
            sizes = collections.Counter(round(char.size, 1) for char in sized)
            line.size = sizes.most_common(1)[0][0]
            fonts = collections.Counter(char.font for char in shown)
            line.font = fonts.most_common(1)[0][0]
            line.text = "".join(char.c for char in line.chars).strip()

    def line_chars(self, raw_line: dict, counts: collections.Counter[tuple[str, float]]) -> list[Char]:
        chars: list[Char] = []
        for span in raw_line["spans"]:
            font = span["font"].split("+")[-1]
            size = span["size"]
            bold = bool(span["flags"] & 16) or "Bold" in font or "Medi" in font or "BX" in font
            italic = bool(span["flags"] & 2) or "Ital" in font or "TI" in font
            for raw_char in span["chars"]:
                box = list(raw_char["bbox"])
                origin_y = raw_char["origin"][1]
                box[1] = max(box[1], origin_y - 1.05 * size)
                box[3] = min(box[3], origin_y + 0.4 * size)
                chars.append(Char(raw_char["c"], tuple(box), tuple(raw_char["origin"]), font, size, bold, italic))
                if not raw_char["c"].isspace() and not MONO_FONT_RE.search(font):
                    counts[(family(font), round(size * 2) / 2)] += 1
        return chars

    def geometry(self) -> None:
        seen: dict[tuple[str, int], set[int]] = collections.defaultdict(set)
        for line in self.lines:
            seen[self.running_key(line)].add(line.page)
        page_count = len(self.doc)
        for line in self.lines:
            if page_count >= 3 and len(seen[self.running_key(line)]) >= max(2, page_count // 2):
                self.running.add(id(line))
        body = [line for line in self.lines if self.is_body_like(line) and id(line) not in self.running]
        self.page_w, self.page_h = self.doc[0].rect.width, self.doc[0].rect.height
        lefts = [line.bbox.x0 for line in body]
        rights = [line.bbox.x1 for line in body]
        mid = self.page_w / 2
        right_col = [line for line in body if line.bbox.x0 > mid - 10]
        left_only = [line for line in body if line.bbox.x1 < mid + 10]
        crossing = [line for line in body if line.bbox.x0 < mid - 10 and line.bbox.x1 > mid + 10]
        self.twocol = (
            len(right_col) > 0.25 * len(body) and len(left_only) > 0.25 * len(body) and len(crossing) < 0.25 * len(body)
        )
        self.left = statistics.quantiles(lefts, n=20)[0]
        self.right = statistics.quantiles(rights, n=20)[-1]
        if self.twocol:
            left_col = [line for line in body if line.bbox.x1 < mid + 10]
            self.col_right = statistics.quantiles([line.bbox.x1 for line in left_col], n=20)[-1]
            second = [line.bbox.x0 for line in right_col if line.bbox.x0 > self.col_right + 2]
            self.col2_left = statistics.quantiles(second or [line.bbox.x0 for line in right_col], n=20)[0]
            self.col_w = self.col_right - self.left
        else:
            self.col_w = self.right - self.left
        long_body = [line for line in body if len(line.text) > 25]
        self.top = min(line.bbox.y0 for line in long_body)
        self.bottom = max(line.bbox.y1 for line in long_body)
        gaps = []
        previous = None
        ordered = sorted(body, key=lambda line: (line.page, line.bbox.x0 > mid if self.twocol else 0, line.bbox.y0))
        for line in ordered:
            if previous and previous.page == line.page and 0 < line.bbox.y0 - previous.bbox.y0 < 2 * self.body_size:
                gaps.append(round(self.baseline(line) - self.baseline(previous), 1))
            previous = line
        self.leading = collections.Counter(gaps).most_common(1)[0][0]

    def running_key(self, line: Line) -> tuple[str, int]:
        return re.sub(r"\d+", "#", line.text), round(line.bbox.y0 / 3)

    def baseline(self, line: Line) -> float:
        ink = [char for char in visible(line.chars) if not DELIMITER_FONT_RE.search(char.font)]
        top = max((char.size for char in ink), default=line.size)
        origins = [char.origin[1] for char in ink if abs(char.size - top) < 0.3]
        return statistics.median(origins) if origins else line.bbox.y1

    def is_body_like(self, line: Line) -> bool:
        return family(line.font) in self.text_families and abs(line.size - self.body_size) < 0.6

    def column(self, rect: pymupdf.Rect) -> int:
        if not self.twocol:
            return 0
        mid = self.page_w / 2
        if rect.x0 < mid - 20 and rect.x1 > mid + 20:
            return 2
        if self.col_right < rect.x0 + 2 or (rect.x0 + rect.x1) / 2 > (self.col_right + self.col2_left) / 2:
            return 1
        return 0

    def col_bounds(self, col: int) -> tuple[float, float]:
        if not self.twocol or col == 2:
            return self.left, self.right
        return (self.left, self.col_right) if col == 0 else (self.col2_left, self.right)

    def visible_drawings(self, page: pymupdf.Page) -> list[pymupdf.Rect]:
        out = []
        clips: list[tuple[int, pymupdf.Rect]] = []
        for drawing in page.get_drawings(extended=True):
            level = drawing.get("level", 0)
            while clips and clips[-1][0] >= level:
                clips.pop()
            if drawing["type"] == "clip":
                clips.append((level, pymupdf.Rect(drawing["scissor"])))
                continue
            if drawing["type"] == "group":
                continue
            rect = pymupdf.Rect(drawing["rect"])
            for _, clip in clips:
                rect = pymupdf.Rect(
                    max(rect.x0, clip.x0), max(rect.y0, clip.y0), min(rect.x1, clip.x1), min(rect.y1, clip.y1)
                )
            if rect.x1 < rect.x0 or rect.y1 < rect.y0:
                continue
            out.append(rect)
        return out

    def graphics(self, page_number: int) -> list[tuple[str, pymupdf.Rect]]:
        page = self.doc[page_number]
        rects: list[tuple[pymupdf.Rect, bool]] = []
        drawings = self.visible_drawings(page)
        if len(drawings) > HEAVY_PAGE_DRAWINGS:
            self.heavy.add(page_number)
        for rect in drawings:
            if rect.width < 1 and rect.height < 1:
                continue
            if rect.width > self.page_w * 0.9 and rect.height > self.page_h * 0.9:
                continue
            if rect.width < 0.5:
                rect.x0 -= 0.25
                rect.x1 += 0.25
            if rect.height < 0.5:
                rect.y0 -= 0.25
                rect.y1 += 0.25
            rects.append((rect, False))
        for image in page.get_images(full=True):
            for rect in page.get_image_rects(image[0]):
                rects.append((pymupdf.Rect(rect), True))
        clusters: list[tuple[pymupdf.Rect, bool]] = []
        for start, is_image in rects:
            rect = pymupdf.Rect(start)
            merged = True
            while merged:
                merged = False
                for cluster in clusters:
                    if self.separate_rule(cluster, rect, is_image):
                        continue
                    margin = 4 if cluster[1] == is_image else 1
                    grown = cluster[0] + (-margin, -margin, margin, margin)
                    if grown.intersects(rect) or grown.contains(rect):
                        rect |= cluster[0]
                        is_image = is_image or cluster[1]
                        clusters.remove(cluster)
                        merged = True
                        break
            clusters.append((rect, is_image))
        out = []
        for cluster, _ in clusters:
            if cluster.height < 2 and cluster.width < self.col_w * 0.5:
                continue
            out.append(("rule" if cluster.height < 2 else "graphic", cluster))
        return out

    def separate_rule(self, cluster: tuple[pymupdf.Rect, bool], rect: pymupdf.Rect, is_image: bool) -> bool:
        cluster_rect, cluster_image = cluster
        if cluster_image and not is_image and rect.height < 1.5 and rect.width > 0.6 * self.col_w:
            return True
        return is_image and not cluster_image and cluster_rect.height < 1.5 and cluster_rect.width > 0.6 * self.col_w

    def mathy(self, line: Line) -> bool:
        count = len(visible(line.chars))
        return sum(char.math for char in line.chars) >= 0.5 * count or (
            line.size < self.body_size - 0.8 and count <= 12
        )

    def attach_fragments(self, lines: list[Line]) -> list[Line]:
        hosts = sorted([line for line in lines if not self.mathy(line)], key=lambda line: line.bbox.y0)
        out = list(hosts)
        for line in lines:
            if not self.mathy(line):
                continue
            middle = center_y(line.bbox)
            best = None
            for host in hosts:
                if self.column(host.bbox) != self.column(line.bbox):
                    continue
                if (
                    host.bbox.y0 - 1 < middle < host.bbox.y1 + 1
                    and host.bbox.x0 - 2 * self.body_size < line.bbox.x0
                    and line.bbox.x1 < host.bbox.x1 + 2 * self.body_size
                ):
                    if best is None or abs(center_y(host.bbox) - middle) < abs(center_y(best.bbox) - middle):
                        best = host
            delimiter = all(DELIMITER_FONT_RE.search(char.font) for char in visible(line.chars))
            if (
                best is not None
                and (delimiter or abs(line.bbox.y1 - best.bbox.y1) < best.size * 0.9)
                and line.bbox.height < best.bbox.height * 1.6
            ):
                merged = list(best.chars)
                for fragment in line.chars:
                    index = next(
                        (k for k, host_char in enumerate(merged) if host_char.bbox[0] > fragment.bbox[0] + 0.1),
                        len(merged),
                    )
                    merged.insert(index, fragment)
                best.chars = merged
                best.bbox |= line.bbox
                best.text = "".join(char.c for char in best.chars).strip()
            else:
                out.append(line)
        return out

    def merge_lines(self, lines: list[Line]) -> list[Line]:
        lines = self.attach_fragments(lines)
        lines = sorted(lines, key=lambda line: (line.bbox.x0, self.baseline(line)))
        out: list[Line] = []
        for line in lines:
            hit = None
            for other in reversed(out):
                size = max(line.size, other.size)
                if (
                    abs(self.baseline(other) - self.baseline(line)) < 1.5
                    and self.column(other.bbox) == self.column(line.bbox)
                    and -2.0 * size <= line.bbox.x0 - other.bbox.x1 < 2.2 * size
                    and not (self.twocol and other.bbox.x1 < self.page_w / 2 < line.bbox.x0)
                ):
                    hit = other
                    break
            if hit:
                space = Char(
                    " ",
                    (hit.bbox.x1, hit.bbox.y0, line.bbox.x0, hit.bbox.y1),
                    (hit.bbox.x1, self.baseline(hit)),
                    hit.chars[-1].font,
                    hit.chars[-1].size,
                    False,
                    False,
                )
                gap = line.bbox.x0 - hit.bbox.x1 > 0.15 * min(line.size, hit.size)
                hit.chars += ([space] if gap else []) + line.chars
                hit.bbox |= line.bbox
                hit.text = "".join(char.c for char in hit.chars).strip()
                if line.size > hit.size:
                    hit.size, hit.font = line.size, line.font
            else:
                out.append(line)
        out = self.attach_fragments(out)
        return sorted(out, key=lambda line: line.bbox.y0)

    def classify(self) -> None:
        in_refs = False
        merged: list[Line] = []
        for page_number in range(len(self.doc)):
            merged += self.merge_lines([line for line in self.lines if line.page == page_number])
        self.lines = merged
        for page_number in range(len(self.doc)):
            in_refs = self.classify_page(page_number, in_refs)

    def classify_page(self, page_number: int, in_refs: bool) -> bool:
        lines = [line for line in self.lines if line.page == page_number]
        graphics = self.graphics(page_number)
        boxes = [rect for kind, rect in graphics if kind == "graphic"]
        framed = []
        for box in boxes:
            inside = [line for line in lines if (box + (-2, -2, 2, 2)).contains(line.bbox)]
            prose_count = sum(1 for line in inside if PROSE_RE.search(line.text) and line.size >= self.body_size * 0.8)
            if prose_count >= 3 and prose_count >= 0.5 * len(inside):
                framed.append(box)
        boxes = [box for box in boxes if box not in framed]
        self.page_boxes[page_number] = boxes
        rules = [rect for kind, rect in graphics if kind == "rule"]
        for line in lines:
            line.col = self.column(line.bbox)
            if id(line) in self.running:
                line.kind = "header" if line.bbox.y0 < self.page_h / 2 else "footer"
            elif line.bbox.y1 < self.top - 2 and not (
                page_number == 0 and line.size > self.body_size + TITLE_SIZE_MARGIN
            ):
                line.kind = "header"
            elif line.bbox.y0 > self.bottom + 2 and (
                len(line.text) <= 8 or line.bbox.y0 > self.bottom + 3 * self.leading
            ):
                line.kind = "footer"
        if self.twocol and page_number > 0:
            self.detect_onecol_page(page_number, lines, boxes)
        if lines:
            low = max(lines, key=lambda line: line.bbox.y1)
            if (
                not low.kind
                and re.fullmatch(r"[\divxlc]{1,5}", low.text.lower())
                and abs((low.bbox.x0 + low.bbox.x1) / 2 - self.page_w / 2) < 0.1 * self.page_w
            ):
                low.kind = "footer"
        running_lines = [line for line in lines if line.kind in ("header", "footer")]

        def running_box(box: pymupdf.Rect) -> bool:
            inside = [line for line in lines if (box + (-2, -2, 2, 2)).intersects(line.bbox)]
            if inside:
                return all(line.kind in ("header", "footer") for line in inside)
            return box.height < 3 and any(
                abs(box.y0 - line.bbox.y1) < 8 or abs(box.y1 - line.bbox.y0) < 8 for line in running_lines
            )

        boxes = [box for box in boxes if not running_box(box)]
        rules = [rule for rule in rules if not running_box(rule)]
        self.classify_lines(page_number, lines, boxes)
        boxes, rules = self.wrap_zones(page_number, lines, boxes, rules)
        self.algorithm_captions(lines, rules)
        for _ in range(3):
            for line in lines:
                if line.kind == "body" and not PROSE_RE.search(line.text):
                    for other in lines:
                        if (
                            other.kind in ("display", "ingraphic")
                            and other.col == line.col
                            and min(line.bbox.y1, other.bbox.y1) - max(line.bbox.y0, other.bbox.y0)
                            > 0.3 * min(line.bbox.height, other.bbox.height)
                        ):
                            line.kind = "display"
                            break
        for line in lines:
            if line.kind == "caption" and re.match(r"^(Table|TABLE)", line.text):
                self.table_below(page_number, line, lines, rules, boxes)
        self.resolve_footnotes(lines)
        self.resolve_captions(lines)
        for line in lines:
            reference_head = REF_HEAD_RE.match(line.text.replace(" ", ""))
            if line.kind in ("heading", "body", "display") and reference_head and line.size >= self.body_size - 0.5:
                in_refs = True
                line.kind = "heading"
            elif line.kind == "heading" and in_refs and not reference_head:
                in_refs = False
            elif in_refs and line.kind in ("body", "small", "display"):
                line.kind = "refs"
        self.build_items(page_number, lines, boxes, rules)
        return in_refs

    def detect_onecol_page(self, page_number: int, lines: list[Line], boxes: list[pymupdf.Rect]) -> None:
        gutter = (self.col_right + self.col2_left) / 2
        body_lines = [
            line
            for line in lines
            if line.bbox.y0 > self.top - 2
            and line.bbox.y1 < self.bottom + 2
            and PROSE_RE.search(line.text)
            and abs(line.size - self.body_size) < 1.5
            and not any((box + (-2, -2, 2, 2)).contains(line.bbox) for box in boxes)
        ]
        crossing = [line for line in body_lines if line.bbox.x0 < gutter - 5 and line.bbox.x1 > gutter + 5]
        if len(body_lines) >= 5 and len(crossing) >= 0.6 * len(body_lines):
            self.onecol_pages.add(page_number)
            for line in lines:
                line.col = 2

    def classify_lines(self, page_number: int, lines: list[Line], boxes: list[pymupdf.Rect]) -> None:
        first_body_y = None
        if page_number == 0:
            first_body_y = min(
                [
                    line.bbox.y0
                    for line in lines
                    if line.kind == ""
                    and REF_HEAD_RE.match(line.text) is None
                    and re.match(r"^abstract$", line.text, re.I)
                ]
                or [None]
            )
        for line in lines:
            if line.kind:
                continue
            in_box = any((box + (-2, -2, 2, 2)).contains(line.bbox) for box in boxes)
            if page_number == 0 and in_box:
                line.kind = "ingraphic"
                continue
            if page_number == 0 and first_body_y is not None and line.bbox.y1 <= first_body_y + 1:
                line.kind = "title" if not re.match(r"^abstract$", line.text, re.I) else "heading"
                if line.kind == "title" and self.twocol:
                    line.col = 2
                continue
            if (
                page_number == 0
                and first_body_y is None
                and line.bbox.y1 <= self.first_heading_y(lines)
                and (
                    line.size > self.body_size + TITLE_SIZE_MARGIN
                    or line.bbox.width < 0.6 * (self.right - self.left)
                    or not PROSE_RE.search(line.text)
                )
            ):
                line.kind = "title"
                if self.twocol:
                    line.col = 2
                continue
            if in_box:
                line.kind = "ingraphic"
                continue
            if CAPTION_RE.match(line.text) and line.size <= self.body_size + 0.6:
                line.kind = "caption"
                continue
            if self.is_heading(line):
                line.kind = "heading"
                continue
            line.kind = self.text_kind(line)

    def text_kind(self, line: Line) -> str:
        math_fraction = sum(char.math for char in line.chars) / max(1, len(visible(line.chars)))
        low, high = self.col_bounds(line.col)
        if re.fullmatch(r"\(\d+(\.\d+)?[a-z]?\)", line.text):
            return "display"
        if (math_fraction > 0.35 and not (PROSE_RE.search(line.text) and abs(line.bbox.x0 - low) < 20)) or (
            re.search(r"\(\d+\)\s*$", line.text) and line.bbox.x1 > high - 3 and line.bbox.x0 > low + 15
        ):
            return "display"
        if self.max_gap(line) > 2.2 * self.body_size:
            return "display"
        if self.is_body_like(line) or family(line.font) in self.text_families and line.size > self.body_size * 0.8:
            if (
                line.col != 2
                and line.bbox.x0 > low + 0.12 * self.col_w
                and line.bbox.x1 < high - 0.12 * self.col_w
                and not PROSE_RE.search(line.text)
            ):
                letters = [char for char in line.chars if char.c.isalpha()]
                bold = letters and all(char.bold for char in letters)
                return "heading" if bold and len(line.text) < 40 else "display"
            return "body"
        return "display"

    def algorithm_captions(self, lines: list[Line], rules: list[pymupdf.Rect]) -> None:
        for line in lines:
            if line.kind != "caption" or not line.text.startswith("Algorithm"):
                continue
            caption_bottom = line.bbox.y1
            rest = sorted(
                [
                    other
                    for other in lines
                    if other is not line and other.col == line.col and other.bbox.y0 > line.bbox.y0
                ],
                key=lambda other: other.bbox.y0,
            )
            caption_lines = []
            for other in rest:
                close = other.bbox.y0 - caption_bottom < 0.6 * self.leading
                if close and other.size < line.size - 1 and other.bbox.y1 < caption_bottom + 0.6 * self.leading:
                    caption_lines.append(other)
                elif (
                    close
                    and abs(other.size - line.size) < 0.6
                    and not other.text.startswith(("Require", "Input", "Ensure"))
                ):
                    caption_lines.append(other)
                    caption_bottom = max(caption_bottom, other.bbox.y1)
                else:
                    break
            below = sorted(
                [
                    rule
                    for rule in rules
                    if rule.y0 > caption_bottom + 1.5 * self.leading
                    and rule.width > 0.5 * self.col_w
                    and self.column(rule) in (line.col, 2)
                ],
                key=lambda rule: rule.y0,
            )
            if below:
                stop = below[0].y0
                for other in caption_lines:
                    other.kind = "caption+"
                for other in rest:
                    if other not in caption_lines and other.bbox.y0 < stop:
                        other.kind = "ingraphic"
                line.algo_bottom = stop

    def table_below(
        self,
        page_number: int,
        line: Line,
        lines: list[Line],
        rules: list[pymupdf.Rect],
        boxes: list[pymupdf.Rect],
    ) -> None:
        below = sorted(
            [
                rule
                for rule in rules
                if rule.y0 > line.bbox.y1 - 1 and rule.width > 0.5 * self.col_w and self.column(rule) in (line.col, 2)
            ],
            key=lambda rule: rule.y0,
        )
        if (
            not below
            or below[0].y0 - line.bbox.y1 > 4 * self.leading + 60
            or any(
                other is not line
                and other.kind == "caption"
                and (other.col == line.col or 2 in (other.col, line.col))
                and line.bbox.y1 < other.bbox.y0 < below[0].y0
                for other in lines
            )
        ):
            self.table_above(page_number, line, lines, rules, boxes)
            return
        continuation = self.caption_cont(line, lines)
        stop = below[0].y0
        first = min(
            [below[0].y0]
            + [
                box.y0
                for box in boxes
                if line.bbox.y1 - 1 < box.y0 < below[0].y0
                and box.width > 0.5 * self.col_w
                and self.column(box) in (line.col, 2)
            ]
        )
        first = min(
            [first]
            + [
                other.bbox.y0
                for other in lines
                if other.col == line.col
                and line.bbox.y1 < other.bbox.y0 < first
                and first - other.bbox.y1 < 2.5 * self.leading
                and other.kind in ("heading", "display")
                and not CAPTION_RE.match(other.text)
                and other not in continuation
            ]
        )
        extensions = sorted(
            below[1:]
            + [
                box
                for box in boxes
                if box.y0 > first and box.width > 0.5 * self.col_w and self.column(box) in (line.col, 2)
            ],
            key=lambda rect: rect.y0,
        )
        for rect in extensions:
            between = [
                other
                for other in lines
                if (other.col == line.col or line.col == 2)
                and stop < other.bbox.y0 < rect.y0
                and (
                    PROSE_RE.search(other.text)
                    and other.kind == "body"
                    and other.bbox.width > 0.85 * self.col_w
                    or other.kind == "caption"
                )
            ]
            if rect.y0 - stop < 20 * self.leading and not between:
                stop = max(stop, rect.y1)
        for other in lines:
            if (
                (other.col == line.col or line.col == 2)
                and first - 1 < other.bbox.y0
                and other.bbox.y1 < stop + 1
                and other.kind in ("body", "display", "heading", "small")
                and other not in continuation
            ):
                other.kind = "ingraphic"
        self.table_spans.setdefault(page_number, []).append((line.col, first, stop))

    def wrap_zones(
        self, page_number: int, lines: list[Line], boxes: list[pymupdf.Rect], rules: list[pymupdf.Rect]
    ) -> tuple[list[pymupdf.Rect], list[pymupdf.Rect]]:
        for caption in [line for line in lines if line.kind == "caption"]:
            low, high = self.col_bounds(caption.col)
            width = high - low
            near = [
                other
                for other in lines
                if other.kind == "body"
                and PROSE_RE.search(other.text)
                and abs(other.bbox.y0 - caption.bbox.y0) < 12 * self.leading
            ]
            if caption.bbox.x0 > low + 0.3 * width:
                side = "r"
                beside = [
                    other
                    for other in near
                    if other.bbox.x1 < caption.bbox.x0 - 3 and abs(other.bbox.x0 - low) < 0.1 * width
                ]
            elif caption.bbox.x1 < high - 0.3 * width:
                side = "l"
                beside = [
                    other
                    for other in near
                    if other.bbox.x0 > caption.bbox.x1 + 3 and abs(other.bbox.x1 - high) < 0.1 * width
                ]
            else:
                continue
            if len(beside) < 3:
                continue
            if side == "r":
                x0, x1 = max(other.bbox.x1 for other in beside) + 3, high + 3
            else:
                x0, x1 = low - 3, min(other.bbox.x0 for other in beside) - 3
            zone = pymupdf.Rect(x0, caption.bbox.y0, x1, caption.bbox.y1)
            elements: list[tuple[pymupdf.Rect, Line | None]] = [
                (pymupdf.Rect(other.bbox), other) for other in lines if other.kind not in ("header", "footer")
            ] + [(pymupdf.Rect(rect), None) for rect in boxes + rules]
            members: set[int] = set()
            grown = True
            while grown:
                grown = False
                for rect, _ in elements:
                    if id(rect) in members:
                        continue
                    if (
                        rect.x0 >= x0 - 1
                        and rect.x1 <= x1 + 1
                        and rect.y0 < zone.y1 + 2 * self.leading
                        and rect.y1 > zone.y0 - 2 * self.leading
                    ):
                        zone |= rect
                        members.add(id(rect))
                        grown = True
            captions = [caption]
            inside = sorted(
                [
                    other
                    for rect, other in elements
                    if other is not None and id(rect) in members and other.bbox.y0 > caption.bbox.y0
                ],
                key=lambda other: other.bbox.y0,
            )
            for other in inside:
                if other.bbox.y0 - captions[-1].bbox.y1 < 0.6 * self.leading and abs(other.size - caption.size) < 0.6:
                    captions.append(other)
                else:
                    break
            for rect, other in elements:
                if other is not None and id(rect) in members:
                    other.kind = "wrapped"
            graphic = pymupdf.Rect()
            for rect, other in elements:
                if id(rect) in members and not (other is not None and other in captions):
                    graphic |= rect
            if graphic.is_empty:
                continue
            self.wraps.setdefault(page_number, []).append(Wrap(graphic, captions, side, zone, caption.col))
            boxes = [box for box in boxes if not zone.contains(box)]
            rules = [rule for rule in rules if not zone.contains(rule)]
        return boxes, rules

    def table_above(
        self,
        page_number: int,
        line: Line,
        lines: list[Line],
        rules: list[pymupdf.Rect],
        boxes: list[pymupdf.Rect],
    ) -> None:
        above = sorted(
            [
                rect
                for rect in rules + boxes
                if rect.y1 < line.bbox.y0 + 1 and rect.width > 0.3 * self.col_w and self.column(rect) in (line.col, 2)
            ],
            key=lambda rect: -rect.y1,
        )
        if not above or line.bbox.y0 - above[0].y1 > 3 * self.leading:
            return
        stop = above[0].y1
        first = above[0].y0
        for rect in above[1:]:
            between = [
                other
                for other in lines
                if (other.col == line.col or line.col == 2)
                and rect.y1 < other.bbox.y0 < first
                and (
                    PROSE_RE.search(other.text)
                    and other.kind == "body"
                    and other.bbox.width > 0.85 * self.col_w
                    or other.kind == "caption"
                )
            ]
            if first - rect.y1 < 20 * self.leading and not between:
                first = min(first, rect.y0)
        header = [
            other
            for other in lines
            if (other.col == line.col or line.col == 2)
            and other.bbox.y1 <= first + 1
            and first - other.bbox.y1 < 1.5 * self.leading
            and other.kind in ("body", "display", "heading", "small")
            and not PROSE_RE.search(other.text)
            and not self.para_tail(other, lines)
            and not NUM_HEAD_RE.match(other.text)
        ]
        if header:
            first = min(other.bbox.y0 for other in header)
        for other in lines:
            if (
                (other.col == line.col or line.col == 2)
                and first - 1 < other.bbox.y0
                and other.bbox.y1 < stop + 1
                and other.kind in ("body", "display", "heading", "small")
            ):
                other.kind = "ingraphic"
        self.table_spans.setdefault(page_number, []).append((line.col, first, stop))

    def first_heading_y(self, lines: list[Line]) -> float:
        heights = [
            line.bbox.y0
            for line in lines
            if (NUM_HEAD_RE.match(line.text) or re.match(r"^(introduction|abstract)$", line.text, re.I))
            and self.is_heading(line)
        ]
        return min(heights) if heights else -1

    def max_gap(self, line: Line) -> float:
        gap = 0
        previous = None
        for char in visible(line.chars):
            if previous is not None:
                gap = max(gap, char.bbox[0] - previous.bbox[2])
            previous = char
        return gap

    def is_heading(self, line: Line) -> bool:
        if len(line.text) > 90 or len(line.text) < 2 or sum(char.math for char in line.chars) > 0.3 * len(line.text):
            return False
        low, high = self.col_bounds(line.col)
        if (
            line.bbox.x1 > high - 0.04 * self.col_w
            and line.bbox.width > 0.8 * (high - low)
            and not NUM_HEAD_RE.match(line.text)
        ):
            return False
        if line.size > self.body_size + 0.9 and abs(line.bbox.x0 - low) < 25:
            return True
        letters = [char for char in line.chars if char.c.isalpha()]
        if (
            letters
            and all(char.bold for char in letters)
            and (abs(line.bbox.x0 - low) < 25 or abs((line.bbox.x0 + line.bbox.x1) / 2 - (low + high) / 2) < 10)
            and (NUM_HEAD_RE.match(line.text) or len(line.text) < 60)
        ):
            return True
        return bool(line.text.isupper() and NUM_HEAD_RE.match(line.text) and len(line.text) < 70)

    def resolve_footnotes(self, lines: list[Line]) -> None:
        for col in (0, 1, 2):
            column_lines = sorted([line for line in lines if line.col == col], key=lambda line: line.bbox.y0)
            body = [line for line in column_lines if line.kind == "body" and line.size >= self.body_size - 0.4]
            if not body:
                continue
            last = body[-1].bbox.y1
            started = False
            for line in column_lines:
                if line.bbox.y0 < last - 1 or line.kind in ("footer", "header", "caption", "heading"):
                    continue
                first = next(iter(visible(line.chars)), None)
                small = line.size < self.body_size - 0.4
                if small and first is not None and (first.size < line.size * 0.85 or first.c in FOOTNOTE_MARKS):
                    started = True
                if started and small and not FOOTNOTE_MONO_FONT_RE.search(line.font):
                    line.kind = "footnote"

    def caption_cont(self, line: Line, lines: list[Line]) -> list[Line]:
        out = []
        previous = line
        following = sorted(
            [
                other
                for other in lines
                if other is not line and (other.col == line.col or line.col == 2) and other.bbox.y0 > line.bbox.y0
            ],
            key=lambda other: other.bbox.y0,
        )
        for other in following:
            aligned = (
                abs(other.bbox.x0 - line.bbox.x0) < 3
                or abs((other.bbox.x0 + other.bbox.x1) - (line.bbox.x0 + line.bbox.x1)) < 6
            )
            if (
                other.kind in ("body", "small", "heading", "display")
                and aligned
                and abs(other.size - line.size) < 0.6
                and 0 < other.bbox.y0 - previous.bbox.y0 < 1.6 * line.size
                and not NUM_HEAD_RE.match(other.text)
            ):
                out.append(other)
                previous = other
            else:
                break
        return out

    def para_tail(self, line: Line, lines: list[Line]) -> bool:
        return any(
            other is not line
            and other.kind == "body"
            and 0 <= line.bbox.y0 - other.bbox.y1 < 0.6 * self.leading
            and other.bbox.width > 0.85 * self.col_w
            and abs(other.bbox.x0 - line.bbox.x0) < 3
            and abs(other.size - line.size) < 0.6
            for other in lines
        )

    def resolve_captions(self, lines: list[Line]) -> None:
        for line in lines:
            if line.kind != "caption" or line.algo_bottom is not None:
                continue
            previous = line
            following = sorted(
                [
                    other
                    for other in lines
                    if other is not line and (other.col == line.col or line.col == 2) and other.bbox.y0 > line.bbox.y0
                ],
                key=lambda other: other.bbox.y0,
            )
            for other in following:
                if (
                    other.kind in ("body", "small", "display", "heading")
                    and abs(other.size - line.size) < 0.6
                    and 0 < other.bbox.y0 - previous.bbox.y0 < 2.2 * line.size
                    and not NUM_HEAD_RE.match(other.text)
                ):
                    other.kind = "caption+"
                    previous = other
                else:
                    break

    def build_items(
        self, page_number: int, lines: list[Line], boxes: list[pymupdf.Rect], rules: list[pymupdf.Rect]
    ) -> None:
        groups = self.group_lines(page_number, lines)
        self.attach_graphics(page_number, groups, boxes, rules)
        self.merge_regions(page_number, groups)
        if self.twocol:
            self.join_gutter_regions(groups, boxes)
        for group in groups:
            if group.kind == "body":
                inside = [
                    region
                    for region in groups
                    if region.kind == "region" and region.col == group.col and region.rect.contains(group.rect)
                ]
                if inside:
                    inside[0].lines += group.lines
                    group.kind = "drop"
        groups = [group for group in groups if group.kind != "drop"]
        self.attach_captions(groups)
        groups = [group for group in groups if group.kind != "drop"]
        for group in groups:
            if group.kind == "body" and len(group.lines) == 1 and group.col != 2:
                low, high = self.col_bounds(group.col)
                line = group.lines[0]
                if (
                    line.bbox.x0 > low + 0.12 * self.col_w
                    and line.bbox.x1 < high - 0.12 * self.col_w
                    and not LONG_PROSE_RE.search(line.text)
                ):
                    group.kind = "region"
        for wrap in self.wraps.get(page_number, []):
            item = Item("region", page_number, wrap.col, wrap.graphic, [], wrap.captions)
            item.caption_above = wrap.captions[0].bbox.y1 < wrap.graphic.y0
            item.wrap = (wrap.side, wrap.zone.width)
            groups.append(item)
        if self.twocol:
            self.join_side_by_side(groups)
        self.absorb_equation_fragments(groups)
        for group in groups:
            group.wide = self.twocol and group.col == 2
        if self.twocol:
            groups.sort(
                key=lambda group: (
                    group.rect.y0 if group.col == 2 else 0,
                    group.col if group.col != 2 else -1,
                    group.rect.y0,
                )
            )
            groups = self.order_twocol(groups)
        else:
            groups.sort(key=lambda group: group.rect.y0)
        for wrapped in [group for group in groups if group.wrap]:
            host = next(
                (
                    group
                    for group in groups
                    if group.kind == "body"
                    and group.col == wrapped.col
                    and group.rect.y1 > wrapped.rect.y0 + self.leading
                ),
                None,
            )
            if host is not None:
                groups.remove(wrapped)
                groups.insert(groups.index(host), wrapped)
        self.items += groups

    def group_lines(self, page_number: int, lines: list[Line]) -> list[Item]:
        groups: list[Item] = []
        for line in sorted(lines, key=lambda line: line.bbox.y0):
            if line.kind in ("header", "footer", "wrapped"):
                continue
            kind = line.kind
            if kind in ("ingraphic", "display", "small", "title", "refs"):
                kind = {"title": "title", "refs": "refs"}.get(kind, "region")
            same = None
            for group in reversed(groups):
                if group.col == line.col or (self.twocol and (group.col == 2) != (line.col == 2)):
                    if group.col == line.col:
                        same = group
                    break
            if kind == "caption":
                groups.append(Item("caption", page_number, line.col, pymupdf.Rect(line.bbox), [line]))
                continue
            if kind == "caption+":
                same = (
                    next(
                        (
                            group
                            for group in reversed(groups)
                            if group.kind == "caption" and (group.col == line.col or group.col == 2 or line.col == 2)
                        ),
                        None,
                    )
                    or next((group for group in reversed(groups) if group.kind == "caption"), None)
                    or same
                )
                if same is None:
                    groups.append(Item("body", page_number, line.col, pymupdf.Rect(line.bbox), [line]))
                    continue
                same.lines.append(line)
                same.rect |= line.bbox
                continue
            if (
                same
                and kind == "refs"
                and same.kind == "refs"
                and (line.bbox.x0 < same.lines[-1].bbox.x0 - 2 or line.bbox.y0 - same.rect.y1 > 0.6 * self.leading)
            ):
                groups.append(Item(kind, page_number, line.col, pymupdf.Rect(line.bbox), [line]))
                continue
            probe = Item(kind, page_number, line.col, pymupdf.Rect(line.bbox), [line])
            if (
                same
                and same.kind == kind
                and kind in ("region", "title", "refs", "footnote")
                and line.bbox.y0 - same.rect.y1 < 2.5 * self.leading
                and (kind != "region" or self.in_table(page_number, same) == self.in_table(page_number, probe))
            ):
                same.lines.append(line)
                same.rect |= line.bbox
                continue
            if same and same.kind == "body" and kind == "body" and self.continues(same, line):
                same.lines.append(line)
                same.rect |= line.bbox
                continue
            groups.append(probe)
        return groups

    def attach_graphics(
        self, page_number: int, groups: list[Item], boxes: list[pymupdf.Rect], rules: list[pymupdf.Rect]
    ) -> None:
        for box in boxes:
            col = self.column(box)
            probe = Item("region", page_number, col, pymupdf.Rect(box))
            hit = [
                group
                for group in groups
                if group.kind == "region"
                and group.col == col
                and (group.rect + (-6, -6, 6, 6)).intersects(box)
                and (
                    self.in_table(page_number, group) == self.in_table(page_number, probe)
                    or (group.rect & box).get_area() > 0.5 * max(1, box.get_area())
                )
            ]
            if hit:
                hit[0].rect |= box
                for other in hit[1:]:
                    hit[0].rect |= other.rect
                    hit[0].lines += other.lines
                    groups.remove(other)
            else:
                groups.append(Item("region", page_number, col, pymupdf.Rect(box)))
        for rule in rules:
            col = self.column(rule)
            hit = [
                group
                for group in groups
                if group.kind == "region" and group.col == col and (group.rect + (-2, -14, 2, 14)).intersects(rule)
            ]
            if hit:
                hit[0].rect |= rule

    def merge_regions(self, page_number: int, groups: list[Item]) -> None:
        changed = True
        while changed:
            changed = False
            regions = [group for group in groups if group.kind == "region"]
            for first in regions:
                for second in regions:
                    if first is second:
                        continue
                    if (
                        first.col != second.col
                        and 2 in (first.col, second.col)
                        and (first.rect & second.rect).get_area()
                        > 0.2 * max(1, min(first.rect.get_area(), second.rect.get_area()))
                    ):
                        first.rect |= second.rect
                        first.lines += second.lines
                        first.col = 2 if self.twocol else first.col
                        groups.remove(second)
                        changed = True
                        break
                    if first.col == second.col and self.in_table(page_number, first) != self.in_table(
                        page_number, second
                    ):
                        continue
                    if (
                        first.col == second.col
                        and first.rect.y0 - 0.8 * self.leading < second.rect.y1
                        and second.rect.y0 - 0.8 * self.leading < first.rect.y1
                        and not any(
                            group.kind == "body"
                            and group.col == first.col
                            and min(first.rect.y1, second.rect.y1) - 1
                            < center_y(group.rect)
                            < max(first.rect.y0, second.rect.y0) + 1
                            for group in groups
                        )
                    ):
                        first.rect |= second.rect
                        first.lines += second.lines
                        groups.remove(second)
                        changed = True
                        break
                if changed:
                    break

    def join_gutter_regions(self, groups: list[Item], boxes: list[pymupdf.Rect]) -> None:
        regions = [group for group in groups if group.kind == "region"]
        for first in regions:
            for second in regions:
                if not (
                    first in groups
                    and second in groups
                    and first.col == 0
                    and second.col == 1
                    and first.rect.x1 > self.col_right - 5
                    and second.rect.x0 < self.col2_left + 5
                    and min(first.rect.y1, second.rect.y1) > max(first.rect.y0, second.rect.y0)
                ):
                    continue
                gutter = pymupdf.Rect(
                    self.col_right + 1,
                    max(first.rect.y0, second.rect.y0),
                    self.col2_left - 1,
                    min(first.rect.y1, second.rect.y1),
                )
                inside = [pymupdf.Rect(box) for box in boxes] + [
                    pymupdf.Rect(line.bbox) for line in first.lines + second.lines
                ]
                if any(rect.intersects(gutter) for rect in inside):
                    first.rect |= second.rect
                    first.lines += second.lines
                    first.col = 2
                    groups.remove(second)
        for wide in [group for group in groups if group.kind == "region" and group.col == 2]:
            for narrow in [group for group in groups if group.kind == "region" and group.col != 2]:
                if narrow in groups and (wide.rect + (0, -6, 0, 6)).intersects(narrow.rect):
                    wide.rect |= narrow.rect
                    groups.remove(narrow)

    def attach_captions(self, groups: list[Item]) -> None:
        for group in groups:
            if group.kind != "caption":
                continue
            near = [
                region
                for region in groups
                if region.kind == "region" and region.col == group.col and not region.caption
            ]
            if not near:
                continue
            best = min(near, key=lambda region, caption=group: caption_distance(region, caption))
            if caption_distance(best, group) < 60:
                best.caption = group.lines
                best.caption_above = group.rect.y0 < best.rect.y0
                if best.rect.intersects(group.rect):
                    if best.caption_above:
                        best.rect.y0 = max(best.rect.y0, group.rect.y1 + 1)
                    else:
                        best.rect.y1 = min(best.rect.y1, group.rect.y0 - 1)
                group.kind = "drop"

    def join_side_by_side(self, groups: list[Item]) -> None:
        merged = True
        while merged:
            merged = False
            for first in groups:
                for second in groups:
                    if (
                        first is second
                        or first.kind != "region"
                        or second.kind != "region"
                        or first.caption
                        or second.caption
                        or first.col != 0
                        or second.col != 1
                    ):
                        continue
                    overlap = min(first.rect.y1, second.rect.y1) - max(first.rect.y0, second.rect.y0)
                    if (
                        overlap > 0.5 * min(first.rect.height, second.rect.height)
                        and second.rect.x0 - first.rect.x1 < 0.5 * self.body_size
                    ):
                        first.rect = first.rect | second.rect
                        first.lines += second.lines
                        first.col = 2
                        groups.remove(second)
                        merged = True
                        break
                if merged:
                    break

    def absorb_equation_fragments(self, groups: list[Item]) -> None:
        equations = [
            group
            for group in groups
            if group.kind == "region"
            and not group.caption
            and group.lines
            and any(line.kind == "display" for line in group.lines)
        ]
        for equation in equations:
            fragments = [
                group
                for group in groups
                if group is not equation
                and not group.caption
                and group.kind == "body"
                and group.lines
                and not any(PROSE_RE.search(line.text) for line in group.lines)
            ]
            for fragment in fragments:
                if (
                    min(fragment.rect.y1, equation.rect.y1) - max(fragment.rect.y0, equation.rect.y0) > -2
                    and fragment.rect.x0 > equation.rect.x0 - 2
                    and fragment.rect.x1 < equation.rect.x1 + 2
                ):
                    equation.rect = equation.rect | fragment.rect
                    equation.lines += fragment.lines
                    groups.remove(fragment)

    def in_table(self, page_number: int, group: Item) -> int | None:
        middle = center_y(group.rect)
        for index, (col, start, stop) in enumerate(self.table_spans.get(page_number, [])):
            if (col == group.col or col == 2 or group.col == 2) and start - 1 <= middle <= stop + 1:
                return index
        return None

    def order_twocol(self, groups: list[Item]) -> list[Item]:
        wides = sorted([group for group in groups if group.col == 2], key=lambda group: group.rect.y0)
        columns = [group for group in groups if group.col != 2]
        out: list[Item] = []
        cuts = [wide.rect.y0 for wide in wides] + [1e9]
        last_cut = -1.0
        for index, cut in enumerate(cuts):
            band = [group for group in columns if last_cut <= group.rect.y0 < cut]
            out += sorted(band, key=lambda group: (group.col, group.rect.y0))
            if index < len(wides):
                out.append(wides[index])
            last_cut = cut
        return out

    def continues(self, group: Item, line: Line) -> bool:
        previous = group.lines[-1]
        _low, high = self.col_bounds(line.col)
        gap = self.baseline(line) - self.baseline(previous)
        if gap > self.leading * 1.35 or gap <= 0:
            return False
        reference_x0 = min(other.bbox.x0 for other in group.lines[1:]) if len(group.lines) > 1 else previous.bbox.x0
        if line.bbox.x0 > reference_x0 + 0.8 * self.body_size and line.bbox.x0 - reference_x0 < 4 * self.body_size:
            return False
        right = max(other.bbox.x1 for other in group.lines)
        if previous.bbox.x1 < min(high, right) - 3 * self.body_size and not previous.text.endswith("-"):
            return False
        first = visible(line.chars)[:3]
        return not (first and all(char.bold for char in first) and THEOREM_RE.match(line.text))

    def join_split_paragraphs(self) -> None:
        out: list[Item] = []
        last = None
        for item in self.items:
            if item.kind == "body":
                first = item.lines[0].text[:1]
                if (
                    last is not None
                    and (first.islower() or first.isdigit() or first in "[(")
                    and not re.search(r"[.!?:]$", last.lines[-1].text.strip())
                    and abs(item.lines[0].size - last.lines[-1].size) < 0.6
                ):
                    last.lines += item.lines
                    continue
                last = item
                out.append(item)
                continue
            if item.kind in ("region", "refs", "title") and not item.caption or item.kind == "heading":
                last = None
            out.append(item)
        self.items = out

    def paragraph_style(self) -> None:
        indents = []
        for item in self.items:
            if item.kind == "body" and len(item.lines) > 1:
                low, _high = self.col_bounds(item.col)
                indents.append(item.lines[0].bbox.x0 - low)
        self.indented = sum(1 for indent in indents if indent > 3) > len(indents) * 0.3
        self.parindent = statistics.median([indent for indent in indents if indent > 3]) if self.indented else 0
        skips = []
        previous = None
        for item in self.items:
            if (
                previous is not None
                and previous.kind == "body"
                and item.kind == "body"
                and previous.page == item.page
                and previous.col == item.col
            ):
                skips.append(self.baseline(item.lines[0]) - self.baseline(previous.lines[-1]) - self.leading)
            previous = item
        skips = [skip for skip in skips if -1 < skip < self.leading]
        self.parskip = statistics.median(skips) if skips else 0
