from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

RECORD_RE = re.compile(r"([(\[hvxkg$r])(\d+),(\d+):(-?\d+),(-?\d+)(?::(-?\d+)(?:,(-?\d+),(-?\d+))?)?")

HBOX = "("

VBOX = "["

BOX_OPENERS = frozenset({HBOX, VBOX})

BOX_CLOSERS = frozenset({")", "]"})

VOID_BOXES = frozenset({"h", "v"})

SCALED_POINTS_PER_POINT = 65536

TEX_POINTS_PER_INCH = 72.27

PDF_POINTS_PER_INCH = 72.0

MAGNIFICATION_BASE = 1000

BODY_MIN_HEIGHT_RATIO = 0.6

BODY_MIN_WIDTH_RATIO = 0.9

BODY_SIBLING_MAX_RATIO = 0.15

BODY_SEARCH_DEPTH = 12


class SynctexError(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    kind: str
    tag: int
    line: int
    x: int
    y: int
    width: int
    height: int
    depth: int


@dataclass
class Box:
    record: Record
    page: int
    index: int
    parent: int | None
    records: list[Record] = field(default_factory=list)
    children: list[int] = field(default_factory=list)
    child_count: int = 0

    @property
    def kind(self) -> str:
        return self.record.kind

    @property
    def tag(self) -> int:
        return self.record.tag

    @property
    def line(self) -> int:
        return self.record.line

    @property
    def x(self) -> int:
        return self.record.x

    @property
    def y(self) -> int:
        return self.record.y

    @property
    def width(self) -> int:
        return self.record.width

    @property
    def height(self) -> int:
        return self.record.height

    @property
    def depth(self) -> int:
        return self.record.depth

    @property
    def extent(self) -> int:
        return self.height + self.depth


@dataclass
class Synctex:
    inputs: dict[int, str]
    unit: int
    x_offset: int
    y_offset: int
    magnification: int
    boxes: list[Box]
    pages: dict[int, list[int]]

    def points(self, value: int) -> float:
        scaled = value * self.unit * self.magnification / MAGNIFICATION_BASE
        return scaled / SCALED_POINTS_PER_POINT * PDF_POINTS_PER_INCH / TEX_POINTS_PER_INCH

    def raw(self, points: float) -> int:
        scaled = points * TEX_POINTS_PER_INCH / PDF_POINTS_PER_INCH * SCALED_POINTS_PER_POINT
        return round(scaled * MAGNIFICATION_BASE / (self.unit * self.magnification))

    def x_points(self, value: int) -> float:
        return self.points(value) + self.points_offset(self.x_offset)

    def y_points(self, value: int) -> float:
        return self.points(value) + self.points_offset(self.y_offset)

    def points_offset(self, offset: int) -> float:
        return offset / SCALED_POINTS_PER_POINT * PDF_POINTS_PER_INCH / TEX_POINTS_PER_INCH

    def tags_of(self, path: str) -> set[int]:
        target = _normalize(path)
        return {tag for tag, name in self.inputs.items() if _normalize(name) == target}

    def is_descendant(self, box: Box, ancestor: Box) -> bool:
        parent = box.parent
        while parent is not None:
            if parent == ancestor.index:
                return True
            parent = self.boxes[parent].parent
        return False

    def body_boxes(self) -> dict[int, Box]:
        body: dict[int, Box] = {}
        for page, indices in self.pages.items():
            roots = [self.boxes[i] for i in indices if self.boxes[i].parent is None and self.boxes[i].extent > 0]
            if not roots:
                continue
            node = max(roots, key=lambda box: box.extent)
            for _ in range(BODY_SEARCH_DEPTH):
                kids = [self.boxes[i] for i in node.children]
                kids = [kid for kid in kids if kid.extent > 0 and kid.width > 0]
                if not kids:
                    break
                tall = max(kids, key=lambda box: box.extent)
                if tall.extent < BODY_MIN_HEIGHT_RATIO * node.extent or tall.width < BODY_MIN_WIDTH_RATIO * node.width:
                    break
                others = [kid for kid in kids if kid is not tall]
                node = tall
                if others and all(kid.extent <= BODY_SIBLING_MAX_RATIO * tall.extent for kid in others):
                    break
            body[page] = node
        return body


def parse(text: str) -> Synctex:
    inputs: dict[int, str] = {}
    header = {"Unit": 1, "X Offset": 0, "Y Offset": 0, "Magnification": MAGNIFICATION_BASE}
    boxes: list[Box] = []
    pages: dict[int, list[int]] = {}
    stack: list[Box] = []
    page: int | None = None
    for line in text.split("\n"):
        if not line:
            continue
        head = line[0]
        if line.startswith("Input:"):
            _, tag, path = line.split(":", 2)
            inputs[int(tag)] = path
            continue
        name, separator, value = line.partition(":")
        if separator and name in header and page is None:
            header[name] = _integer(value, name)
            continue
        if head == "{":
            page = _integer(line[1:], "page")
            pages[page] = []
            stack = []
            continue
        if head == "}":
            page = None
            continue
        if head in BOX_CLOSERS:
            if stack:
                stack.pop()
            continue
        match = RECORD_RE.match(line)
        if match is None or page is None:
            continue
        kind, tag, number, x, y, width, height, depth = match.groups()
        record = Record(
            kind=kind,
            tag=int(tag),
            line=int(number),
            x=int(x),
            y=int(y),
            width=int(width or 0),
            height=int(height or 0),
            depth=int(depth or 0),
        )
        if kind in BOX_OPENERS:
            parent = stack[-1] if stack else None
            box = Box(record=record, page=page, index=len(boxes), parent=parent.index if parent else None)
            if parent is not None:
                parent.children.append(box.index)
                parent.child_count += 1
            boxes.append(box)
            pages[page].append(box.index)
            stack.append(box)
        elif stack:
            stack[-1].records.append(record)
            if kind in VOID_BOXES:
                stack[-1].child_count += 1
    if not pages:
        raise SynctexError("the SyncTeX file has no page records")
    return Synctex(
        inputs=inputs,
        unit=header["Unit"],
        x_offset=header["X Offset"],
        y_offset=header["Y Offset"],
        magnification=header["Magnification"],
        boxes=boxes,
        pages=pages,
    )


def _integer(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise SynctexError(f"the SyncTeX header field {name} is not an integer: {value!r}") from error


def _normalize(path: str) -> str:
    return str(PurePosixPath(*[part for part in PurePosixPath(path).parts if part != "."]))
