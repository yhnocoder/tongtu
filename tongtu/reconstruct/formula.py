from __future__ import annotations

import difflib
import re
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from pydantic import BaseModel, RootModel

from .. import processes
from ..artifacts.reconstruct import RegionKind, RegionsFile
from ..compiling import COMPILE_TIMEOUT_SECONDS
from ..manifests import describe_error, write_manifest
from .checks import INLINE_CROP_RE
from .crops import CROPS_DIRNAME, crop_path, open_crop
from .recognition import HINTS_FILENAME, IMAGES_DIRNAME, SKIP, copy_crops, image_path, verify_each, with_packages

FORMULA_KINDS = (RegionKind.DISPLAY, RegionKind.INLINE)

DISPLAY_CROP_RE = re.compile(
    r"\\par\\smallskip\\noindent(?:\\hfil|\\hspace\*\{[0-9.]+bp\})\\includegraphics\{"
    + CROPS_DIRNAME
    + r"/(?P<key>e-\d+)\.pdf\}(?:\\hfil)?\\par\\smallskip"
)

PREAMBLE = (
    r"\documentclass[11pt]{article}\usepackage{amsmath,amssymb,bm,mathtools,mathrsfs,upgreek,amsfonts,xcolor}"
    r"\pagestyle{empty}\begin{document}"
)

COMPILE_COMMAND: tuple[str, ...] = ("pdflatex", "-interaction=nonstopmode", "-halt-on-error", "f.tex")

FORBIDDEN_RE = re.compile(
    r"\\(raisebox|hspace|vspace|kern|mkern|phantom|rlap|llap|mathrlap|mathllap|lower|raise"
    r"|tiny|scriptsize|footnotesize|small|normalsize|large|Large|LARGE|huge|Huge)(?![A-Za-z])"
)

CMEX: dict[str, str] = {
    "P": "∑",
    "X": "∑",
    "Q": "∏",
    "Y": "∏",
    "R": "∫",
    "Z": "∫",
    "H": "∮",
    "I": "∮",
    "S": "⋃",
    "[": "⋃",
    "T": "⋂",
    "\\": "⋂",
    "L": "⨁",
    "M": "⨁",
    "N": "⨂",
    "O": "⨂",
    "`": "∐",
    "a": "∐",
}

BOLD_RE = re.compile(r"(MIB|CMBX|CMBSY|BSY|bold|-Bd|Semibold|Black(?!board)|txb|pxb)", re.I)

QUOTES = str.maketrans({"’": "'", "′": "'", "‘": "'", "“": '"', "”": '"', "∗": "*", "−": "-", "–": "-"})

SQRT = "√"

BASELINE_TOLERANCE = 0.12

SIZE_TOLERANCE = 0.2

LARGEST_SIZE_SLACK = 0.2

IMAGE_ZOOM_RANGE = (3, 8)

IMAGE_TARGET_HEIGHT = 120

DIFF_CONTEXT = 8

DIFF_PARTS = 6

UNBOLD_PASSES = 3

BOLD_GROUP_RE = re.compile(r"\\(bm|boldsymbol)\s*\{((?:[^{}]|\{[^{}]*\})*)\}")

BOLD_TOKEN_RE = re.compile(r"\\(bm|boldsymbol)\s*(\\[A-Za-z]+|[A-Za-z0-9])")

MATHBF_RE = re.compile(r"\\mathbf\s*\{")

BOLD_SCOPE_RE = re.compile(
    r"\\(bm|boldsymbol)\{((?:\\[A-Za-z]+|[A-Za-z0-9]))(_|\^)(\{(?:[^{}]|\{[^{}]*\})*\}|[A-Za-z0-9])\}"
)

EQUATION_NUMBER_RE = re.compile(r"(\\q?quad|\\hfill|~|\s)*\(\s*([0-9A-Za-z.]+)\s*\)\s*$")

PROSE_RE = re.compile(r"\\(?:text|mathrm|textrm|mbox)\{[^{}]*[A-Za-z]{3,}[^{}]*\s[^{}]*\}")

ALLOWED_ENVIRONMENT_RE = re.compile(r"\\begin\{(aligned|split|array|matrix|pmatrix|bmatrix|cases|gathered)")

ERROR_LINE_RE = re.compile(r"^!.*", re.M)


@dataclass(frozen=True)
class Glyph:
    char: str
    bold: bool
    color: int
    baseline: float
    size: float


@dataclass(frozen=True)
class Comparison:
    text: bool
    bold: bool
    color: bool
    baseline_shift: float
    size_change: float
    original: str
    rendered: str


@dataclass(frozen=True)
class FormulaVerdict:
    passed: bool
    latex: str
    problem: str


class FormulaHint(BaseModel):
    kind: RegionKind
    spans: list[str]


class FormulaHints(RootModel[dict[str, FormulaHint]]):
    pass


def normalized(char: str) -> str:
    char = unicodedata.normalize("NFKC", char.translate(QUOTES))
    return "".join(c for c in char if not c.isspace() and unicodedata.category(c)[0] not in "ZC")


def page_glyphs(page: pymupdf.Page) -> list[Glyph]:
    glyphs = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                font = span["font"]
                extension = "cmex" in font.lower()
                bold = bool(BOLD_RE.search(font.replace("BlackboardBold", "").replace("Blackboard", "")))
                for char in span["chars"]:
                    if not pymupdf.Rect(char["bbox"]).intersects(page.rect):
                        continue
                    text = CMEX.get(char["c"], "") if extension else char["c"]
                    if text == SQRT:
                        text = ""
                    glyphs += [
                        Glyph(piece, bold, span["color"], char["origin"][1], span["size"]) for piece in normalized(text)
                    ]
    return glyphs


def relative_positions(glyphs: Sequence[Glyph]) -> list[tuple[float, float]]:
    largest = max(glyph.size for glyph in glyphs)
    reference = min(glyph.baseline for glyph in glyphs if glyph.size >= largest - LARGEST_SIZE_SLACK)
    return [((glyph.baseline - reference) / largest, glyph.size / largest) for glyph in glyphs]


def compare(original: Sequence[Glyph], rendered: Sequence[Glyph]) -> Comparison:
    original_text = "".join(glyph.char for glyph in original)
    rendered_text = "".join(glyph.char for glyph in rendered)
    text = original_text == rendered_text
    if not text:
        return Comparison(False, False, False, 0.0, 0.0, original_text, rendered_text)
    pairs = list(zip(relative_positions(original), relative_positions(rendered), strict=True))
    return Comparison(
        text=True,
        bold=[glyph.bold for glyph in original] == [glyph.bold for glyph in rendered],
        color=[glyph.color for glyph in original] == [glyph.color for glyph in rendered],
        baseline_shift=round(max(abs(a[0] - b[0]) for a, b in pairs), 3),
        size_change=round(max(abs(a[1] - b[1]) / a[1] for a, b in pairs), 3),
        original=original_text,
        rendered=rendered_text,
    )


def within_tolerance(comparison: Comparison) -> bool:
    return comparison.baseline_shift <= BASELINE_TOLERANCE and comparison.size_change <= SIZE_TOLERANCE


def math_body(latex: str) -> str:
    latex = latex.strip()
    if latex.startswith(r"\[") and latex.endswith(r"\]"):
        latex = latex[2:-2].strip()
    return latex.strip("$").strip()


def unbold(latex: str) -> str:
    for _ in range(UNBOLD_PASSES):
        latex = BOLD_GROUP_RE.sub(r"\2", latex)
        latex = BOLD_TOKEN_RE.sub(r"\2", latex)
        latex = MATHBF_RE.sub(r"\\mathrm{", latex)
    return latex


def variants(body: str, original: Sequence[Glyph]) -> list[tuple[str, str]]:
    found = [("raw", body)]
    plain = unbold(body)
    if plain != body:
        found.append(("unbold", plain))
    scoped = BOLD_SCOPE_RE.sub(r"\\\1{\2}\3\4", body)
    if scoped != body:
        found.append(("scope", scoped))
    if all(glyph.bold for glyph in original):
        found.append(("allbold", r"\bm{" + plain + "}"))
    colors = {glyph.color for glyph in original}
    if len(colors) == 1 and 0 not in colors:
        color = f"{colors.pop():06X}"
        found += [(f"{name}+color", r"{\color[HTML]{" + color + "}" + latex + "}") for name, latex in list(found)]
    return found


def forbidden_problem(latex: str) -> str:
    match = FORBIDDEN_RE.search(latex)
    if match is None:
        return ""
    return (
        f"{match.group(0)} is not allowed: layout adjustment and size commands are rejected; "
        "reproduce the formula with ordinary math markup."
    )


def display_problem(body: str) -> str:
    if PROSE_RE.search(body):
        return "the display block is mostly prose in \\text{...}; such blocks stay images, write SKIP."
    if r"\begin{" in body and not ALLOWED_ENVIRONMENT_RE.search(body):
        return (
            "only the aligned, split, array, matrix, pmatrix, bmatrix, cases and gathered environments "
            "can replace a display crop."
        )
    return ""


def difference_problem(comparison: Comparison) -> str:
    if not comparison.text:
        a, b = comparison.original, comparison.rendered
        parts = [
            f"original '{a[max(0, i1 - DIFF_CONTEXT) : i2 + DIFF_CONTEXT]}' vs yours "
            f"'{b[max(0, j1 - DIFF_CONTEXT) : j2 + DIFF_CONTEXT]}'"
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes()
            if op != "equal"
        ]
        return "Glyph sequence differs: " + "; ".join(parts[:DIFF_PARTS])
    if not comparison.bold:
        return "Characters match but bold pattern differs; check which glyphs use bold fonts in hints."
    if not comparison.color:
        return "Characters match but colour differs; check colours in hints."
    if not within_tolerance(comparison):
        return (
            "Characters match but vertical placement or size of some glyphs differs "
            f"(dy={comparison.baseline_shift}, size={comparison.size_change}); check sub/superscripts, fraction "
            "style (\\frac vs \\tfrac or a/b), \\scriptstyle, text vs math symbols, and big-operator limits placement."
        )
    return ""


def rendered_glyphs(body: str, kind: RegionKind) -> list[Glyph] | str:
    wrapped = rf"\[{body}\]" if kind is RegionKind.DISPLAY else f"${body}$"
    with tempfile.TemporaryDirectory(prefix="tongtu-formula-") as temporary:
        folder = Path(temporary)
        (folder / "f.tex").write_text(PREAMBLE + wrapped + r"\end{document}", encoding="utf-8")
        try:
            outcome = processes.run_in_process_group(list(COMPILE_COMMAND), folder, COMPILE_TIMEOUT_SECONDS)
        except OSError as error:
            return f"failed to run {COMPILE_COMMAND[0]} ({describe_error(error)})."
        pdf = folder / "f.pdf"
        if outcome.returncode or outcome.timed_out or not pdf.is_file():
            errors = ERROR_LINE_RE.findall(outcome.stdout.decode("utf-8", errors="replace"))
            return "LaTeX failed to compile." + (f" {errors[0]}" if errors else "")
        return page_glyphs(pymupdf.open(pdf)[0])


def verify(crop: Path, latex: str, kind: RegionKind) -> FormulaVerdict:
    if latex.strip() == SKIP:
        return FormulaVerdict(False, latex, SKIP)
    problem = forbidden_problem(latex)
    if problem:
        return FormulaVerdict(False, latex, problem)
    original = page_glyphs(open_crop(crop)[0])
    if not original:
        return FormulaVerdict(False, latex, "the crop has no text layer to compare with; it stays an image.")
    body = math_body(latex)
    problem = display_problem(body) if kind is RegionKind.DISPLAY else ""
    if problem:
        return FormulaVerdict(False, latex, problem)
    best: tuple[bool, FormulaVerdict] | None = None
    for _name, candidate in variants(body, original):
        rendered = rendered_glyphs(candidate, kind)
        if isinstance(rendered, str):
            text, verdict = False, FormulaVerdict(False, candidate, rendered)
        else:
            comparison = compare(original, rendered)
            if comparison.bold and comparison.color:
                return FormulaVerdict(within_tolerance(comparison), candidate, difference_problem(comparison))
            text, verdict = comparison.text, FormulaVerdict(False, candidate, difference_problem(comparison))
        if best is None or (text and not best[0]):
            best = (text, verdict)
    return best[1]


def recognizable(tex: str, regions: RegionsFile) -> dict[str, RegionKind]:
    kinds = {region.id: region.kind for region in regions.regions if region.kind in FORMULA_KINDS}
    keys = [match.group("key") for match in DISPLAY_CROP_RE.finditer(tex)]
    keys += [match.group(1) for match in INLINE_CROP_RE.finditer(tex)]
    return {key: kinds[key] for key in dict.fromkeys(keys) if key in kinds}


def span_hints(page: pymupdf.Page) -> list[str]:
    return [
        f"{span['font']}#{span['color']:06X}:{text}"
        for block in page.get_text("rawdict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        if (text := "".join(char["c"] for char in span["chars"])).strip()
    ]


def prepare(tree: Path, session_dir: Path, keys: Mapping[str, RegionKind]) -> None:
    (session_dir / IMAGES_DIRNAME).mkdir(parents=True, exist_ok=True)
    copy_crops(tree, session_dir, list(keys))
    hints = {}
    low, high = IMAGE_ZOOM_RANGE
    for key, kind in keys.items():
        page = open_crop(tree / crop_path(key))[0]
        zoom = max(low, min(high, IMAGE_TARGET_HEIGHT / max(1, page.rect.height)))
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(image_path(session_dir, key))
        hints[key] = FormulaHint(kind=kind, spans=span_hints(page))
    write_manifest(session_dir / HINTS_FILENAME, FormulaHints(hints))


def verify_all(
    tree: Path, transcriptions: Mapping[str, str], keys: Mapping[str, RegionKind]
) -> dict[str, FormulaVerdict]:
    def one(key: str) -> FormulaVerdict:
        if key not in transcriptions:
            return FormulaVerdict(False, "", "no transcription in rec.json")
        return verify(tree / crop_path(key), transcriptions[key], keys[key])

    return verify_each(list(keys), one)


def display_latex(body: str) -> str:
    body = math_body(body)
    number = EQUATION_NUMBER_RE.search(body)
    tag = ""
    if number:
        body = body[: number.start()].rstrip()
        tag = r"\tag*{(" + number.group(2) + ")}"
    return r"\begin{equation*}" + body + tag + r"\end{equation*}"


def write_back(tex: str, verdicts: Mapping[str, FormulaVerdict]) -> tuple[str, int]:
    accepted = {key: verdict.latex for key, verdict in verdicts.items() if verdict.passed}
    written = 0

    def display(match: re.Match[str]) -> str:
        nonlocal written
        key = match.group("key")
        if key not in accepted:
            return match.group(0)
        written += 1
        return display_latex(accepted[key])

    def inline(match: re.Match[str]) -> str:
        nonlocal written
        key = match.group(1)
        if key not in accepted:
            return match.group(0)
        written += 1
        before = match.string[match.start() - 1 : match.start()]
        after = match.string[match.end() : match.end() + 1]
        return (" " if before.isalnum() else "") + f"${math_body(accepted[key])}$" + (" " if after.isalpha() else "")

    tex = DISPLAY_CROP_RE.sub(display, tex)
    tex = INLINE_CROP_RE.sub(inline, tex)
    return (with_packages(tex) if written else tex), written


def failure_lines(verdicts: Mapping[str, FormulaVerdict]) -> list[str]:
    passed = sum(verdict.passed for verdict in verdicts.values())
    lines = [f"passed {passed}/{len(verdicts)}"]
    lines += [
        f"{key}: {verdict.problem}\n  yours: {verdict.latex}"
        for key, verdict in verdicts.items()
        if not verdict.passed and verdict.problem != SKIP
    ]
    skipped = [key for key, verdict in verdicts.items() if verdict.problem == SKIP]
    if skipped:
        lines.append(f"SKIP {len(skipped)}: {' '.join(skipped)}")
    return lines
