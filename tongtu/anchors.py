from __future__ import annotations

import bisect
import difflib
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from .artifacts.compile import AnchorBlock, AnchorKind, AnchorPage, AnchorRect, AnchorsFile
from .artifacts.mask import BlockCategory, BlockRecord, CaptionRecord
from .masking import MaskError, Placement, UnmaskOutcome, match_group, skip_optional_arguments
from .synctex import HBOX, Box, Record, Synctex
from .validation import BLANK_LINE_RE, BLOCK_ID_PREFIX, TOKEN_RE, read_control_sequence

SKIPPED_CATEGORIES = frozenset({BlockCategory.PREAMBLE, BlockCategory.POSTAMBLE})

INLINE_CATEGORIES = frozenset({BlockCategory.COMMENT})

HEADING_RE = re.compile(r"^[ \t]*\\(?:part|chapter|section|subsection|subsubsection)\*?(?![A-Za-z])", re.MULTILINE)

LABEL_LINE_RE = re.compile(r"\n[ \t]*\\label\{[^{}\n]*\}[ \t]*(?=\n|$)")

BIBITEM_RE = re.compile(r"\\bibitem(?![A-Za-z])")

BIBLIOGRAPHY_END = "\\end{thebibliography}"

CAPTION_OPENER = "\\caption"

CAPTION_CLOSER = "}"

ABSTRACT_OPENER = "\\begin{abstract}"

ABSTRACT_CLOSER = "\\end{abstract}"

MAKETITLE_RE = re.compile(r"\\maketitle(?![A-Za-z])")

DISPLAY_OPEN_RE = re.compile(
    r"[ \t]*(?:\\\[|\$\$|\\begin\{[A-Za-z*]+\}(?:\{[^}]*\})?(?:[ \t]*\\label\{[^}]*\})?)[ \t]*"
)

TEXT_KINDS = frozenset({AnchorKind.TEXT, AnchorKind.HEADING, AnchorKind.ABSTRACT})

GLUE_OR_KERN = frozenset({"g", "k"})

WIDE_RECORDS = frozenset({"k", "h", "v", "r"})

CONTAINER_HEIGHT_RATIO = 0.6

WRAPPER_TEXT_CHECK_MAX_HEIGHT = 30.0

LINE_END_TOLERANCE = 0.5

TEXT_GAP_MIN = 1.0

INTERIOR_GLUE_MIN = 2

LINE_GAP_RATIO = 1.02

LINE_GAP_SAMPLES = 4

MIN_RECT_SIZE = 0.2

CONTAIN_TOLERANCE = 0.5

MERGE_OVERLAP_RATIO = 0.5

MERGE_GAP = 6.0

COORDINATE_DIGITS = 2


class AnchorsError(ValueError):
    pass


@dataclass(frozen=True)
class Unit:
    id: str
    kind: AnchorKind
    start: int
    end: int
    parent: str | None = None


@dataclass(frozen=True)
class MediaBox:
    left: float
    bottom: float
    right: float
    top: float


@dataclass
class Rect:
    page: int
    x: float
    y: float
    width: float
    height: float
    lines: frozenset[int] = frozenset()
    box: int = -1
    graphic: bool = False

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


def build_units(outcome: UnmaskOutcome, blocks: Sequence[BlockRecord], captions: Sequence[CaptionRecord]) -> list[Unit]:
    categories = {block.id: block.category for block in blocks}
    caption_kinds = {caption.id: AnchorKind(caption.kind.value) for caption in captions}
    stream = outcome.stream
    tokens = list(TOKEN_RE.finditer(stream))
    block_places = [place for place in outcome.placements if place.token_id.startswith(BLOCK_ID_PREFIX)]
    if len(tokens) != len(block_places):
        raise MaskError(f"the stream holds {len(tokens)} block tokens but unmask placed {len(block_places)}")
    caption_places = [place for place in outcome.placements if not place.token_id.startswith(BLOCK_ID_PREFIX)]
    token_ends = [token.end() for token in tokens]
    shifts = [0]
    for token, place in zip(tokens, block_places, strict=True):
        shifts.append(shifts[-1] + (place.end - place.start) - (token.end() - token.start()))

    def to_text(position: int) -> int:
        return position + shifts[bisect.bisect_right(token_ends, position)]

    token_starts = [token.start() for token in tokens]
    units: list[Unit] = []
    pieces: dict[int, str] = {}
    for paragraph, (start, end) in enumerate(_paragraph_spans(stream)):
        number = 0

        def add_text(piece_start: int, piece_end: int, paragraph: int = paragraph) -> None:
            nonlocal number
            for kind, unit_start, unit_end in _text_pieces(stream, piece_start, piece_end):
                pieces[len(units)] = stream[unit_start:unit_end]
                units.append(Unit(f"PAR-{paragraph}-{number}", kind, to_text(unit_start), to_text(unit_end)))
                number += 1

        cursor = start
        for index in range(bisect.bisect_left(token_starts, start), bisect.bisect_left(token_starts, end)):
            place = block_places[index]
            category = categories[place.token_id]
            if category in INLINE_CATEGORIES:
                continue
            add_text(cursor, tokens[index].start())
            units.extend(_object_units(place, category, outcome.text, caption_places, caption_kinds))
            cursor = tokens[index].end()
        add_text(cursor, end)
    return _title_area(units, pieces)


def _title_area(units: list[Unit], pieces: Mapping[int, str]) -> list[Unit]:
    title = next(
        (
            index
            for index, piece in pieces.items()
            if units[index].kind is AnchorKind.TEXT and MAKETITLE_RE.search(piece)
        ),
        None,
    )
    if title is None:
        return units
    start = title
    while start > 0 and _in_title_area(units[start - 1], pieces.get(start - 1)):
        start -= 1
    head = replace(units[title], kind=AnchorKind.METADATA)
    members = [unit if unit.parent is not None else replace(unit, parent=head.id) for unit in units[start:title]]
    return [*units[:start], head, *members, *units[title + 1 :]]


def _in_title_area(unit: Unit, piece: str | None) -> bool:
    return piece is None or (unit.kind is AnchorKind.TEXT and _only_front_matter(piece))


def _only_front_matter(piece: str) -> bool:
    cursor = 0
    try:
        while cursor < len(piece):
            token = TOKEN_RE.match(piece, cursor)
            if token is not None:
                cursor = token.end()
            elif piece[cursor].isspace():
                cursor += 1
            elif piece.startswith(ABSTRACT_OPENER, cursor):
                closing = piece.find(ABSTRACT_CLOSER, cursor)
                if closing < 0:
                    return False
                cursor = closing + len(ABSTRACT_CLOSER)
            elif piece[cursor] == "\\":
                cursor = _skip_command(piece, cursor)
            else:
                return False
    except MaskError:
        return False
    return True


def _skip_command(piece: str, start: int) -> int:
    _, cursor = read_control_sequence(piece, start)
    if piece.startswith("*", cursor):
        cursor += 1
    while (cursor := skip_optional_arguments(piece, cursor)) < len(piece) and piece[cursor] == "{":
        cursor = match_group(piece, cursor)
    return cursor


def _paragraph_spans(stream: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in BLANK_LINE_RE.finditer(stream):
        if stream[cursor : match.start()].strip():
            spans.append((cursor, match.start()))
        cursor = match.end()
    if stream[cursor:].strip():
        spans.append((cursor, len(stream)))
    return spans


def _text_pieces(stream: str, start: int, end: int) -> list[tuple[AnchorKind, int, int]]:
    pieces: list[tuple[AnchorKind, int, int]] = []
    cursor = start
    for match in HEADING_RE.finditer(stream, start, end):
        if match.start() < cursor:
            continue
        try:
            group = skip_optional_arguments(stream, match.end())
            if group >= end or stream[group] != "{":
                continue
            heading_end = match_group(stream, group)
        except MaskError:
            continue
        if heading_end > end:
            continue
        pieces.extend(_trimmed(stream, AnchorKind.TEXT, cursor, match.start()))
        line_end = stream.find("\n", heading_end, end)
        heading_end = end if line_end < 0 else line_end
        while (label := LABEL_LINE_RE.match(stream, heading_end, end)) is not None:
            heading_end = label.end()
        pieces.extend(_trimmed(stream, AnchorKind.HEADING, match.start(), heading_end))
        cursor = heading_end
    pieces.extend(_trimmed(stream, AnchorKind.TEXT, cursor, end))
    return pieces


def _trimmed(text: str, kind: AnchorKind, start: int, end: int) -> list[tuple[AnchorKind, int, int]]:
    piece = text[start:end]
    if not TOKEN_RE.sub("", piece).strip():
        return []
    leading = len(piece) - len(piece.lstrip())
    trailing = len(piece) - len(piece.rstrip())
    return [(kind, start + leading, end - trailing)]


def _object_units(
    place: Placement,
    category: BlockCategory,
    text: str,
    caption_places: Sequence[Placement],
    caption_kinds: Mapping[str, AnchorKind],
) -> list[Unit]:
    block_id, start, end = place.token_id, place.start, place.end
    units: list[Unit] = []
    parent: str | None = None
    if category not in SKIPPED_CATEGORIES:
        units.append(Unit(block_id, AnchorKind(category.value), start, end))
        parent = block_id
    for place in caption_places:
        if start <= place.start and place.end <= end and _trimmed(text, AnchorKind.TEXT, place.start, place.end):
            kind = caption_kinds[place.token_id]
            units.append(Unit(place.token_id, kind, *_caption_span(text, start, place, kind), parent))
    if category is BlockCategory.BIBLIOGRAPHY:
        units.extend(_bibitem_units(block_id, start, end, text))
    return units


def _caption_span(text: str, block_start: int, place: Placement, kind: AnchorKind) -> tuple[int, int]:
    opener, closer = (
        (CAPTION_OPENER, CAPTION_CLOSER) if kind is AnchorKind.CAPTION else (ABSTRACT_OPENER, ABSTRACT_CLOSER)
    )
    command = text.rfind(opener, block_start, place.start)
    start = place.start if command < 0 else command
    end = place.end + len(closer) if text.startswith(closer, place.end) else place.end
    return start, end


def _bibitem_units(block_id: str, start: int, end: int, text: str) -> list[Unit]:
    closing = text.rfind(BIBLIOGRAPHY_END, start, end)
    stop = closing if closing >= 0 else end
    starts = [match.start() for match in BIBITEM_RE.finditer(text, start, stop)]
    units: list[Unit] = []
    for number, item_start in enumerate(starts):
        item_end = starts[number + 1] if number + 1 < len(starts) else stop
        units.extend(
            Unit(f"{block_id}.{number}", AnchorKind.BIBITEM, piece_start, piece_end, block_id)
            for _, piece_start, piece_end in _trimmed(text, AnchorKind.BIBITEM, item_start, item_end)
        )
    return units


def line_ranges(units: Sequence[Unit], text: str, final: str) -> list[tuple[int, int]]:
    starts = [0, *(index + 1 for index, character in enumerate(text) if character == "\n")]
    mapping = _line_mapping(text, final)
    ranges: list[tuple[int, int]] = []
    for unit in units:
        first = bisect.bisect_right(starts, unit.start)
        last = bisect.bisect_right(starts, max(unit.start, unit.end - 1))
        ranges.append((mapping[first - 1], mapping[last - 1]))
    return ranges


def _line_mapping(text: str, final: str) -> list[int]:
    source = text.split("\n")
    if text == final:
        return list(range(1, len(source) + 1))
    target = final.split("\n")
    mapping = [1] * len(source)
    matcher = difflib.SequenceMatcher(None, source, target, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        for offset, line in enumerate(range(i1, i2)):
            if tag == "equal":
                mapping[line] = j1 + offset + 1
            else:
                mapping[line] = min(j1 + min(offset, max(j2 - j1 - 1, 0)), len(target) - 1) + 1
    return mapping


def display_openings(units: Sequence[Unit], ranges: Sequence[tuple[int, int]], final: str) -> dict[int, int]:
    lines = final.split("\n")
    order = _line_order(ranges)
    openings: dict[int, int] = {}
    for position, index in enumerate(order):
        unit = units[index]
        first, last = ranges[index]
        if position == 0 or unit.kind is not AnchorKind.MATH or first == last:
            continue
        previous = order[position - 1]
        if units[previous].kind not in TEXT_KINDS or ranges[previous][1] != first - 1:
            continue
        if DISPLAY_OPEN_RE.fullmatch(lines[first - 1]):
            openings[index] = first
    return openings


def _line_order(ranges: Sequence[tuple[int, int]]) -> list[int]:
    return sorted(range(len(ranges)), key=lambda index: (ranges[index][0], -ranges[index][1], index))


def line_owners(units: Sequence[Unit], ranges: Sequence[tuple[int, int]]) -> dict[int, int]:
    owners: dict[int, int] = {}
    for index in sorted(range(len(units)), key=lambda index: units[index].parent is not None):
        first, last = ranges[index]
        for line in range(first, last + 1):
            owners[line] = index
    if not ranges:
        return owners
    parents = {unit.id: index for index, unit in enumerate(units)}
    items = [index for index, unit in enumerate(units) if unit.kind is AnchorKind.BIBITEM]
    for position, index in enumerate(items):
        parent = parents.get(units[index].parent or "")
        if parent is None:
            continue
        following = items[position + 1] if position + 1 < len(items) else None
        stop = (
            ranges[following][0]
            if following is not None and units[following].parent == units[index].parent
            else ranges[parent][1]
        )
        for line in range(ranges[index][1] + 1, stop):
            if owners.get(line) == parent:
                owners[line] = index
    for line in range(min(first for first, _ in ranges), max(last for _, last in ranges) + 2):
        if line not in owners and line - 1 in owners:
            owners[line] = owners[line - 1]
    return owners


def locate(
    sync: Synctex,
    main_tags: set[int],
    owners: Mapping[int, int],
    openings: Mapping[int, int],
    previous: Mapping[int, int],
    ranges: Sequence[tuple[int, int]],
) -> dict[int, list[Rect]]:
    body = sync.body_boxes()
    has_records = [False] * len(sync.boxes)
    for box in reversed(sync.boxes):
        has_records[box.index] = has_records[box.index] or any(record.kind != "x" for record in box.records)
        if box.parent is not None and has_records[box.index]:
            has_records[box.parent] = True
    memo: dict[int, set[int]] = {}
    seen_graphic: set[int] = set()
    half_point = sync.raw(LINE_END_TOLERANCE)
    found: list[tuple[int, Rect]] = []
    for box in sync.boxes:
        if box.kind != HBOX:
            continue
        frame = body.get(box.page)
        if frame is None:
            continue
        records = [record for record in box.records if record.tag in main_tags and record.kind != "r"]
        wrapper = bool(box.child_count > 0 and records and all(record.kind in GLUE_OR_KERN for record in records))
        if (
            wrapper
            and sync.points(box.extent) <= WRAPPER_TEXT_CHECK_MAX_HEIGHT
            and _has_own_text(sync, box, sync.raw(TEXT_GAP_MIN), half_point)
        ):
            wrapper = False
        if records:
            records = _settle_records(records, box, half_point, owners)
        groups: dict[int, list] = {}
        for record in [] if wrapper else records:
            unit = owners.get(record.line)
            if unit is None:
                continue
            left, right = _record_extent(record)
            group = groups.setdefault(unit, [left, right, set()])
            group[0] = min(group[0], left)
            group[1] = max(group[1], right)
            group[2].add(record.line)
        graphic_box = None
        if not groups and box.child_count == 0 and not has_records[box.index] and box.tag in main_tags:
            top = _graphic_top(sync, box)
            if top.width > 0 and top.extent > 0 and top.index not in seen_graphic:
                seen_graphic.add(top.index)
                unit = owners.get(box.line)
                if unit is not None:
                    groups[unit] = [top.x, top.x + top.width, {box.line}]
                    graphic_box = top
        if not groups:
            continue
        if len(groups) == 1 and graphic_box is None:
            ((unit, group),) = groups.items()
            opening = openings.get(unit)
            if (
                opening is not None
                and group[2] == {opening}
                and unit in previous
                and any(record.kind == "x" for record in box.records)
            ):
                groups = {previous[unit]: group}
        if not _inside(sync, box, frame) or _is_container(sync, box, frame, main_tags, owners, memo):
            continue
        holder = graphic_box or box
        for unit, left, right, lines in _segments(box, records, groups, owners, graphic_box):
            x0, y0, x1, y1 = sync.page_bounds(holder, (left, holder.y - holder.height, right, holder.y + holder.depth))
            found.append(
                (
                    unit,
                    Rect(
                        page=box.page,
                        x=sync.x_points(x0),
                        y=sync.y_points(y0),
                        width=sync.points(x1 - x0),
                        height=sync.points(y1 - y0),
                        lines=frozenset(lines),
                        box=box.index,
                        graphic=graphic_box is not None,
                    ),
                )
            )
    _fix_tails(sync, found, previous, ranges)
    rects: dict[int, list[Rect]] = {}
    for unit, rect in found:
        rects.setdefault(unit, []).append(rect)
    return rects


def _inside(sync: Synctex, box: Box, frame: Box) -> bool:
    return sync.is_descendant(box, frame)


def _is_container(
    sync: Synctex, box: Box, frame: Box, main_tags: set[int], owners: Mapping[int, int], memo: dict[int, set[int]]
) -> bool:
    if sync.page_extent(box) <= CONTAINER_HEIGHT_RATIO * frame.extent:
        return False
    if box.index not in memo:
        units: set[int] = set()
        stack = [box.index]
        while stack:
            current = sync.boxes[stack.pop()]
            for record in current.records:
                if record.tag in main_tags and record.kind not in "xr":
                    unit = owners.get(record.line)
                    if unit is not None:
                        units.add(unit)
            stack.extend(current.children)
        memo[box.index] = units
    return len(memo[box.index]) >= 2


def _has_own_text(sync: Synctex, box: Box, gap_min: int, tolerance: int) -> bool:
    spans = [
        (min(child.x, child.x + child.width), max(child.x, child.x + child.width)) for child in _children(sync, box)
    ]
    glues: list[int] = []
    for record in box.records:
        if record.kind == "g":
            glues.append(record.x)
        elif record.kind in WIDE_RECORDS:
            spans.append((min(record.x, record.x + record.width), max(record.x, record.x + record.width)))
        else:
            spans.append((record.x, record.x))
    left, right = box.x, box.x + box.width
    interior = [
        x
        for x in glues
        if left + tolerance < x < right - tolerance
        and not any(start - tolerance <= x <= stop + tolerance for start, stop in spans)
    ]
    if len(interior) >= INTERIOR_GLUE_MIN:
        return True
    cursor = left
    for start, stop in sorted(spans) + [(right, right)]:
        if start - cursor > gap_min and not any(abs(x - cursor) <= tolerance for x in glues):
            return True
        cursor = max(cursor, stop)
    return False


def _children(sync: Synctex, box: Box) -> list[Box]:
    return [sync.boxes[index] for index in box.children]


def _settle_records(records: list[Record], box: Box, tolerance: int, owners: Mapping[int, int]) -> list[Record]:
    reliable = [record for record in records if record.kind != "x"]
    if not reliable:
        return []
    order = sorted(range(len(records)), key=lambda index: records[index].x)
    settled: list[Record] = []
    last: Record | None = None
    for index in order:
        record = records[index]
        if record.kind != "x":
            last = record
            settled.append(record)
            continue
        reference = last
        if reference is None:
            reference = next(
                (records[j] for j in order if records[j].kind != "x" and records[j].x >= record.x), reliable[0]
            )
        settled.append(replace(record, line=reference.line, tag=reference.tag))
    if len(settled) >= 2:
        edge = box.x + box.width - tolerance
        inner = [record for record in settled if not (record.kind in GLUE_OR_KERN and record.x >= edge)]
        if inner and len(inner) < len(settled):
            settled = inner
    if len(settled) >= 3:
        owned = [owners.get(record.line) for record in settled]
        dropped: set[int] = set()
        if owned[0] != owned[1] and owned[1] == owned[2] and owned[1] is not None:
            dropped.add(0)
        if owned[-1] != owned[-2] and owned[-2] == owned[-3] and owned[-2] is not None:
            dropped.add(len(settled) - 1)
        settled = [record for index, record in enumerate(settled) if index not in dropped]
    return settled


def _record_extent(record: Record) -> tuple[int, int]:
    width = record.width if record.kind in WIDE_RECORDS else 0
    return record.x, record.x + max(width, 0)


def _graphic_top(sync: Synctex, box: Box) -> Box:
    top = box
    while top.parent is not None:
        parent = sync.boxes[top.parent]
        if (
            parent.kind != HBOX
            or parent.child_count != 1
            or parent.line != box.line
            or any(record.kind in "$x" for record in parent.records)
        ):
            break
        top = parent
    return top


def _segments(
    box: Box, records: Sequence[Record], groups: Mapping[int, list], owners: Mapping[int, int], graphic_box: Box | None
) -> list[tuple[int, int, int, set[int]]]:
    if len(groups) == 1:
        ((unit, group),) = groups.items()
        holder = graphic_box or box
        return [(unit, holder.x, holder.x + holder.width, group[2])]
    ordered = sorted((record for record in records if owners.get(record.line) is not None), key=lambda r: r.x)
    segments: list[list] = []
    for number, record in enumerate(ordered):
        unit = owners[record.line]
        left = box.x if number == 0 else record.x
        if segments and segments[-1][0] == unit:
            segments[-1][3].add(record.line)
            continue
        if segments:
            segments[-1][2] = left
        segments.append([unit, left, None, {record.line}])
    segments[-1][2] = box.x + box.width
    return [(unit, left, right, lines) for unit, left, right, lines in segments]


def _fix_tails(
    sync: Synctex, found: list[tuple[int, Rect]], previous: Mapping[int, int], ranges: Sequence[tuple[int, int]]
) -> None:
    by_box: dict[int, list[int]] = {}
    for position, (_, rect) in enumerate(found):
        by_box.setdefault(rect.box, []).append(position)

    def unit_of(box_index: int) -> int | None:
        positions = by_box.get(box_index)
        if not positions or len({found[position][0] for position in positions}) != 1:
            return None
        return found[positions[-1]][0]

    for position, (unit, rect) in enumerate(found):
        before = previous.get(unit)
        if rect.graphic or len(by_box[rect.box]) != 1 or before is None:
            continue
        if ranges[before][1] != ranges[unit][0] - 1 or rect.lines != {ranges[unit][0]}:
            continue
        box = sync.boxes[rect.box]
        if box.parent is None:
            continue
        siblings = [child.index for child in _children(sync, sync.boxes[box.parent]) if child.kind == HBOX]
        index = siblings.index(box.index)
        if index < 2 or unit_of(siblings[index - 1]) != before:
            continue
        gaps = [
            sync.boxes[siblings[j]].y - sync.boxes[siblings[j - 1]].y
            for j in range(max(1, index - LINE_GAP_SAMPLES), index)
            if unit_of(siblings[j]) == before and unit_of(siblings[j - 1]) == before
        ]
        if not gaps:
            continue
        gap = box.y - sync.boxes[siblings[index - 1]].y
        if 0 < gap <= statistics.median(gaps) * LINE_GAP_RATIO:
            found[position] = (before, rect)


def merge(rects: Sequence[Rect]) -> list[Rect]:
    rects = [replace(rect) for rect in rects if rect.width > MIN_RECT_SIZE and rect.height > MIN_RECT_SIZE]
    kept: list[Rect] = []
    for index, rect in enumerate(rects):
        dominated = False
        for other_index, other in enumerate(rects):
            if other_index == index or not _contains(other, rect):
                continue
            if (
                (other_index < index)
                if _contains(rect, other)
                else other.width * other.height > rect.width * rect.height
            ):
                dominated = True
                break
        if not dominated:
            kept.append(rect)
    changed = True
    while changed:
        changed = False
        for first in range(len(kept)):
            for second in range(first + 1, len(kept)):
                a, b = kept[first], kept[second]
                if a.page != b.page:
                    continue
                overlap = min(a.bottom, b.bottom) - max(a.y, b.y)
                gap = max(a.x, b.x) - min(a.right, b.right)
                if overlap > MERGE_OVERLAP_RATIO * min(a.height, b.height) and gap < MERGE_GAP:
                    left, top = min(a.x, b.x), min(a.y, b.y)
                    kept[first] = Rect(
                        page=a.page,
                        x=left,
                        y=top,
                        width=max(a.right, b.right) - left,
                        height=max(a.bottom, b.bottom) - top,
                        lines=a.lines | b.lines,
                    )
                    del kept[second]
                    changed = True
                    break
            if changed:
                break
    return kept


def _contains(outer: Rect, inner: Rect) -> bool:
    return (
        outer.page == inner.page
        and outer.x - CONTAIN_TOLERANCE <= inner.x
        and outer.y - CONTAIN_TOLERANCE <= inner.y
        and outer.right + CONTAIN_TOLERANCE >= inner.right
        and outer.bottom + CONTAIN_TOLERANCE >= inner.bottom
    )


def anchors_file(
    sync: Synctex,
    main_paths: Sequence[str],
    units: Sequence[Unit],
    text: str,
    final: str,
    media_boxes: Sequence[MediaBox],
    pdf_name: str,
    tex_name: str,
) -> AnchorsFile:
    if len(media_boxes) != len(sync.pages):
        raise AnchorsError(f"the SyncTeX file has {len(sync.pages)} pages but {pdf_name} has {len(media_boxes)}")
    main_tags = {tag for path in main_paths for tag in sync.tags_of(path)}
    if not main_tags:
        raise AnchorsError(f"the SyncTeX file does not list {tex_name} among its inputs")
    ranges = line_ranges(units, text, final)
    owners = line_owners(units, ranges)
    openings = display_openings(units, ranges, final)
    order = _line_order(ranges)
    previous = {order[position]: order[position - 1] for position in range(1, len(order))}
    located = locate(sync, main_tags, owners, openings, previous, ranges)
    merged = {unit: merge(rects) for unit, rects in located.items()}
    children: dict[str, list[int]] = {}
    for index, unit in enumerate(units):
        if unit.parent is not None:
            children.setdefault(unit.parent, []).append(index)
    blocks: list[AnchorBlock] = []
    for index, unit in enumerate(units):
        own = merged.get(index, [])
        family = [rect for child in children.get(unit.id, []) for rect in merged.get(child, [])]
        rects = merge([*own, *family]) if family else own
        members = [index, *children.get(unit.id, [])]
        first = min(ranges[member][0] for member in members)
        last = max(ranges[member][1] for member in members)
        blocks.append(
            AnchorBlock(
                id=unit.id,
                kind=unit.kind,
                parent=unit.parent,
                first_line=first,
                last_line=last,
                rects=[_pdf_rect(rect, media_boxes) for rect in _reading_order(rects)],
            )
        )
    body = sync.body_boxes()
    pages: list[AnchorPage] = []
    for number, media in enumerate(media_boxes, start=1):
        frame = body.get(number)
        pages.append(
            AnchorPage(
                page=number,
                width=round(media.right - media.left, COORDINATE_DIGITS),
                height=round(media.top - media.bottom, COORDINATE_DIGITS),
                body=None
                if frame is None
                else _pdf_rect(
                    Rect(
                        page=number,
                        x=sync.x_points(frame.x),
                        y=sync.y_points(frame.y - frame.height),
                        width=sync.points(frame.width),
                        height=sync.points(frame.extent),
                    ),
                    media_boxes,
                ),
            )
        )
    return AnchorsFile(pdf=pdf_name, tex=tex_name, pages=pages, blocks=blocks)


def _reading_order(rects: Sequence[Rect]) -> list[Rect]:
    return sorted(rects, key=lambda rect: (rect.page, rect.y, rect.x))


def _pdf_rect(rect: Rect, media_boxes: Sequence[MediaBox]) -> AnchorRect:
    media = media_boxes[rect.page - 1]
    return AnchorRect(
        page=rect.page,
        x0=round(media.left + rect.x, COORDINATE_DIGITS),
        y0=round(media.top - rect.bottom, COORDINATE_DIGITS),
        x1=round(media.left + rect.right, COORDINATE_DIGITS),
        y1=round(media.top - rect.y, COORDINATE_DIGITS),
    )
