from __future__ import annotations

import bisect
import collections
import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .. import fonts, processes
from ..artifacts.common import CompileReport
from ..artifacts.reconstruct import CheckReport, RegionsFile
from ..compiling import COMPILE_TIMEOUT_SECONDS
from .crops import crop_path
from .rebuild import MAIN_FILENAME

RECALL_MIN = 0.98

PRECISION_MIN = 0.98

ORDER_MIN = 0.99

PAGE_RATIO_RANGE = (0.85, 1.20)

ORDER_GRAM_WORDS = 6

FLOATING_MARGIN = 1

WORD_RE = re.compile(r"[A-Za-z]{3,}")

LINE_HYPHEN_RE = re.compile(r"([a-z])-\n([a-z])")

TEXT_FLAGS = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES

DRAFT_COMMAND: tuple[str, ...] = ("xelatex", "-interaction=nonstopmode", MAIN_FILENAME)

GRAPHICX_PACKAGE = r"\usepackage{graphicx}"

DRAFT_GRAPHICX_PACKAGE = r"\usepackage[draft]{graphicx}"

BEGIN_DOCUMENT = r"\begin{document}"

FLOATING_REMOVED = (
    r"\renewcommand\caption[2][]{}\renewcommand\footnote[1]{}\renewcommand\footnotetext[1]{}"
    r"\renewcommand\footnotemark[1][]{}\pagestyle{empty}"
)

DRAFT_NAME_MARKERS = ("crops", ".pdf")

INLINE_CROP_RE = re.compile(r"\\im\{(m-\d+)\}\{-?[0-9.]+bp\}\{((?:\\.|[^{}\\]|\{(?:\\.|[^{}\\])*\})*)\}")

HINT_COMMAND_RE = re.compile(r"\\[A-Za-z]+\s*")

HINT_ESCAPED_RE = re.compile(r"\\([{}&%$#_])")

TEXTBACKSLASH = "\\textbackslash{}"

DIFFERENCE_WORDS = 20

ORDER_RUNS = 5

ORDER_RUN_CHARS = 120

Word = tuple[str, int]


@dataclass(frozen=True)
class Order:
    score: float
    grams: int
    out_of_order: list[int]


@dataclass(frozen=True)
class CheckDetail:
    report: CheckReport
    missing: list[tuple[str, int]]
    extra: list[tuple[str, int]]
    out_of_order: list[str]


def text_words(text: str) -> collections.Counter[str]:
    joined = LINE_HYPHEN_RE.sub(r"\1\2", text)
    return collections.Counter(word.lower() for word in WORD_RE.findall(joined))


def pdf_words(pdf: Path) -> tuple[collections.Counter[str], int]:
    doc = pymupdf.open(pdf)
    counts: collections.Counter[str] = collections.Counter()
    for page in doc:
        counts += text_words(page.get_text("text", flags=TEXT_FLAGS))
    return counts, len(doc)


def recall_precision(original: collections.Counter[str], rebuilt: collections.Counter[str]) -> tuple[float, float]:
    common = sum((original & rebuilt).values())
    total = sum(original.values())
    return (common / total if total else 0.0), common / max(1, sum(rebuilt.values()))


def unique_grams(words: Sequence[str]) -> dict[tuple[str, ...], int]:
    positions: dict[tuple[str, ...], list[int]] = {}
    for index in range(len(words) - ORDER_GRAM_WORDS + 1):
        positions.setdefault(tuple(words[index : index + ORDER_GRAM_WORDS]), []).append(index)
    return {gram: found[0] for gram, found in positions.items() if len(found) == 1}


def longest_increasing(sequence: Sequence[int]) -> set[int]:
    tails: list[int] = []
    tail_index: list[int] = []
    previous = [-1] * len(sequence)
    for index, value in enumerate(sequence):
        slot = bisect.bisect_left(tails, value)
        if slot == len(tails):
            tails.append(value)
            tail_index.append(index)
        else:
            tails[slot] = value
            tail_index[slot] = index
        previous[index] = tail_index[slot - 1] if slot > 0 else -1
    members: set[int] = set()
    current = tail_index[-1] if tail_index else -1
    while current >= 0:
        members.add(current)
        current = previous[current]
    return members


def order_score(original: Sequence[str], original_all: Sequence[str], rebuilt: Sequence[str]) -> Order:
    all_counts = collections.Counter(
        tuple(original_all[index : index + ORDER_GRAM_WORDS])
        for index in range(len(original_all) - ORDER_GRAM_WORDS + 1)
    )
    original_grams = {gram: at for gram, at in unique_grams(original).items() if all_counts[gram] == 1}
    rebuilt_grams = unique_grams(rebuilt)
    common = sorted((at, rebuilt_grams[gram]) for gram, at in original_grams.items() if gram in rebuilt_grams)
    if not common:
        return Order(score=1.0, grams=0, out_of_order=[])
    members = longest_increasing([position for _, position in common])
    out_of_order = [common[index][0] for index in range(len(common)) if index not in members]
    return Order(score=len(members) / len(common), grams=len(common), out_of_order=out_of_order)


def order_runs(words: Sequence[Word], out_of_order: Sequence[int]) -> list[str]:
    runs: list[list[int]] = []
    for position in out_of_order:
        if not runs or position - runs[-1][1] > ORDER_GRAM_WORDS:
            runs.append([position, position])
        else:
            runs[-1][1] = position
    return [
        f"p{words[start][1] + 1}: "
        + " ".join(word for word, _ in words[start : end + ORDER_GRAM_WORDS])[:ORDER_RUN_CHARS]
        for start, end in runs[:ORDER_RUNS]
    ]


def original_words(pdf: Path, regions: RegionsFile, masked: bool) -> list[Word]:
    covered: dict[int, list[pymupdf.Rect]] = collections.defaultdict(list)
    for region in regions.regions:
        covered[region.page].append(pymupdf.Rect(region.rect))
    for line in regions.floating:
        margin = FLOATING_MARGIN
        covered[line.page].append(pymupdf.Rect(line.rect) + (-margin, -margin, margin, margin))
    words: list[Word] = []
    for page_number, page in enumerate(pymupdf.open(pdf)):
        rects = covered.get(page_number + 1, [])
        for word in page.get_text("words"):
            rect = pymupdf.Rect(word[:4])
            middle = (rect.tl + rect.br) / 2
            if masked and any(cover.contains(middle) for cover in rects):
                continue
            words += [(found.lower(), page_number) for found in WORD_RE.findall(word[4])]
    return words


def draft_source(tex: str) -> str:
    tex = tex.replace(GRAPHICX_PACKAGE, DRAFT_GRAPHICX_PACKAGE, 1)
    return tex.replace(BEGIN_DOCUMENT, FLOATING_REMOVED + BEGIN_DOCUMENT, 1)


def rebuilt_words(tree: Path) -> list[Word]:
    with tempfile.TemporaryDirectory(prefix="tongtu-order-") as temporary:
        draft = Path(temporary) / "tree"
        shutil.copytree(tree, draft)
        main = draft / MAIN_FILENAME
        main.write_text(draft_source(main.read_text(encoding="utf-8")), encoding="utf-8")
        pdf = main.with_suffix(".pdf")
        pdf.unlink(missing_ok=True)
        processes.run_in_process_group(
            list(DRAFT_COMMAND),
            draft,
            COMPILE_TIMEOUT_SECONDS,
            env=fonts.environment(fonts.configured(), os.environ),
        )
        if not pdf.is_file():
            return []
        words: list[Word] = []
        for page_number, page in enumerate(pymupdf.open(pdf)):
            for word in page.get_text("words"):
                if any(marker in word[4] for marker in DRAFT_NAME_MARKERS):
                    continue
                words += [(found.lower(), page_number) for found in WORD_RE.findall(word[4])]
        return words


def glyph_counts(text: str) -> collections.Counter[str]:
    return collections.Counter(char for char in unicodedata.normalize("NFKC", text) if not char.isspace())


def hint_text(hint: str) -> str:
    hint = hint.replace(TEXTBACKSLASH, "\x00")
    hint = HINT_ESCAPED_RE.sub(lambda match: "\x01" + match.group(1), hint)
    hint = HINT_COMMAND_RE.sub("", hint)
    hint = re.sub(r"[{}]", "", hint)
    return hint.replace("\x00", "\\").replace("\x01", "")


def lost_glyphs(tree: Path) -> list[tuple[str, str]]:
    tex = (tree / MAIN_FILENAME).read_text(encoding="utf-8")
    lost = []
    for match in INLINE_CROP_RE.finditer(tex):
        name = match.group(1)
        expected = glyph_counts(hint_text(match.group(2)))
        path = tree / crop_path(name)
        found = glyph_counts(pymupdf.open(path)[0].get_text()) if path.is_file() else collections.Counter()
        missing = expected - found
        if missing:
            lost.append((name, "".join(missing.elements())))
    return lost


def failed_items(report: CheckReport) -> list[str]:
    low, high = PAGE_RATIO_RANGE
    items = []
    if report.pages <= 0:
        items.append("no compiled PDF with pages")
    if report.recall < RECALL_MIN:
        items.append(f"recall {report.recall:.4f} < {RECALL_MIN}")
    if report.precision < PRECISION_MIN:
        items.append(f"precision {report.precision:.4f} < {PRECISION_MIN}")
    if report.missing_characters:
        items.append(f"{report.missing_characters} missing characters")
    if report.order < ORDER_MIN:
        items.append(f"reading order {report.order:.4f} < {ORDER_MIN}")
    ratio = report.pages / report.source_pages if report.source_pages else 0.0
    if not low <= ratio <= high:
        items.append(f"page ratio {report.pages}/{report.source_pages} outside {low}-{high}")
    return items


def check(original: Path, tree: Path, compiled: CompileReport | None, regions: RegionsFile) -> CheckDetail:
    source_counts, source_pages = pdf_words(original)
    pdf = tree / Path(MAIN_FILENAME).with_suffix(".pdf")
    has_pdf = compiled is not None and compiled.pages > 0 and compiled.pdf_bytes > 0 and pdf.is_file()
    missing_characters = compiled.missing_characters if compiled is not None else 0
    if not has_pdf:
        report = CheckReport(
            source_pages=source_pages,
            pages=0,
            recall=0.0,
            precision=0.0,
            missing_characters=missing_characters,
            order=0.0,
            passed=False,
        )
        return CheckDetail(
            report=report, missing=source_counts.most_common(DIFFERENCE_WORDS), extra=[], out_of_order=[]
        )
    rebuilt_counts, pages = pdf_words(pdf)
    recall, precision = recall_precision(source_counts, rebuilt_counts)
    masked = original_words(original, regions, masked=True)
    order = order_score(
        [word for word, _ in masked],
        [word for word, _ in original_words(original, regions, masked=False)],
        [word for word, _ in rebuilt_words(tree)],
    )
    low, high = PAGE_RATIO_RANGE
    passed = (
        recall >= RECALL_MIN
        and precision >= PRECISION_MIN
        and missing_characters == 0
        and order.score >= ORDER_MIN
        and source_pages > 0
        and low <= pages / source_pages <= high
    )
    report = CheckReport(
        source_pages=source_pages,
        pages=pages,
        recall=recall,
        precision=precision,
        missing_characters=missing_characters,
        order=order.score,
        passed=passed,
    )
    return CheckDetail(
        report=report,
        missing=(source_counts - rebuilt_counts).most_common(DIFFERENCE_WORDS),
        extra=(rebuilt_counts - source_counts).most_common(DIFFERENCE_WORDS),
        out_of_order=order_runs(masked, order.out_of_order),
    )
