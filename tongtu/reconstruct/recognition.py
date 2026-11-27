from __future__ import annotations

import re
import shutil
import tempfile
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from pydantic import RootModel, ValidationError

from .. import processes
from ..compiling import COMPILE_TIMEOUT_SECONDS
from ..manifests import describe_error
from .crops import SQRT, crop_path

TRANSCRIPTIONS_FILENAME = "rec.json"

HINTS_FILENAME = "hints.json"

IMAGES_DIRNAME = "img"

SKIP = "SKIP"

MISSING_TRANSCRIPTION = "no transcription in rec.json"

NO_TEXT_LAYER = "the crop has no text layer to compare with; it stays an image."

VERIFY_WORKERS = 4

SNIPPET_FILENAME = "f.tex"

FONTSPEC_PACKAGE = r"\usepackage{fontspec}"

RECOGNITION_PACKAGES = (
    r"\usepackage{mathtools,mathrsfs,upgreek}\usepackage[table]{xcolor}"
    r"\usepackage{booktabs,multirow,makecell,array,pifont}"
)

BOLD_RE = re.compile(r"MIB|BX|CMBSY|BSY|bold|-Bd|Medi|Black(?!board)|txb|pxb", re.I)

BLACKBOARD_RE = re.compile(r"Blackboard(Bold)?", re.I)

LAYOUT_COMMANDS = (
    "raisebox",
    "hspace",
    "vspace",
    "hskip",
    "vskip",
    "kern",
    "mkern",
    "mskip",
    "phantom",
    "hphantom",
    "vphantom",
    "rlap",
    "llap",
    "mathrlap",
    "mathllap",
    "resizebox",
    "scalebox",
    "lower",
    "raise",
)

LAYOUT_COMMAND_RE = re.compile(r"\\(" + "|".join(LAYOUT_COMMANDS) + r")(?![A-Za-z])")

ERROR_LINE_RE = re.compile(r"^!.*", re.M)

COMPILE_FAILED = "LaTeX failed to compile."


@dataclass(frozen=True)
class Verdict:
    passed: bool
    latex: str
    problem: str


@dataclass(frozen=True)
class PageChar:
    line: int
    direction: tuple[float, float]
    font: str
    color: int
    size: float
    text: str
    origin: tuple[float, float]
    bbox: tuple[float, float, float, float]


class Transcriptions(RootModel[dict[str, str]]):
    pass


def is_bold(font: str) -> bool:
    return bool(BOLD_RE.search(BLACKBOARD_RE.sub("", font)))


def normalized(char: str, quotes: Mapping[int, str]) -> str:
    char = unicodedata.normalize("NFKC", char.translate(quotes))
    return "".join(c for c in char if not c.isspace() and unicodedata.category(c)[0] not in "ZC")


def without_sqrt(text: str) -> str:
    return "" if text == SQRT else text


def page_chars(page: pymupdf.Page, clip: pymupdf.Rect | None = None) -> Iterator[PageChar]:
    line_number = 0
    for block in page.get_text("rawdict", clip=clip)["blocks"]:
        for line in block.get("lines", []):
            line_number += 1
            for span in line["spans"]:
                for char in span["chars"]:
                    if pymupdf.Rect(char["bbox"]).intersects(page.rect):
                        yield PageChar(
                            line_number,
                            tuple(line["dir"]),
                            span["font"],
                            span["color"],
                            span["size"],
                            char["c"],
                            tuple(char["origin"]),
                            tuple(char["bbox"]),
                        )


def page_spans(page: pymupdf.Page) -> list[tuple[dict, str]]:
    return [
        (span, text)
        for block in page.get_text("rawdict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        if (text := "".join(char["c"] for char in span["chars"])).strip()
    ]


def span_hint(span: dict, text: str) -> str:
    return f"{span['font']}#{span['color']:06X}:{text}"


def layout_command(latex: str) -> str:
    match = LAYOUT_COMMAND_RE.search(latex)
    return match.group(0) if match else ""


def compile_snippet[T](source: str, command: Sequence[str], read: Callable[[pymupdf.Page], T]) -> T | str:
    with tempfile.TemporaryDirectory(prefix="tongtu-recognition-") as temporary:
        folder = Path(temporary)
        (folder / SNIPPET_FILENAME).write_text(source, encoding="utf-8")
        try:
            outcome = processes.run_in_process_group(list(command), folder, COMPILE_TIMEOUT_SECONDS)
        except OSError as error:
            return f"failed to run {command[0]} ({describe_error(error)})"
        pdf = (folder / SNIPPET_FILENAME).with_suffix(".pdf")
        if outcome.returncode or outcome.timed_out or not pdf.is_file():
            errors = ERROR_LINE_RE.findall(outcome.stdout.decode("utf-8", errors="replace"))
            return errors[0] if errors else COMPILE_FAILED
        return read(pymupdf.open(pdf)[0])


def read_transcriptions(session_dir: Path, session: str) -> tuple[dict[str, str], str]:
    path = session_dir / TRANSCRIPTIONS_FILENAME
    if not path.is_file():
        return {}, f"the {session} session wrote no {TRANSCRIPTIONS_FILENAME}; every crop stays an image"
    try:
        return Transcriptions.model_validate_json(path.read_text(encoding="utf-8")).root, ""
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        return {}, (
            f"cannot read {TRANSCRIPTIONS_FILENAME} written by the {session} session ({describe_error(error)}); "
            "every crop stays an image"
        )


def copy_crops(tree: Path, session_dir: Path, keys: Sequence[str]) -> None:
    for key in keys:
        target = session_dir / crop_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tree / crop_path(key), target)


def image_path(session_dir: Path, key: str) -> Path:
    return session_dir / IMAGES_DIRNAME / f"{key}.png"


def verify_all(
    keys: Sequence[str], transcriptions: Mapping[str, str], verify: Callable[[str, str], Verdict]
) -> dict[str, Verdict]:
    def one(key: str) -> Verdict:
        if key not in transcriptions:
            return Verdict(False, "", MISSING_TRANSCRIPTION)
        return verify(key, transcriptions[key])

    with ThreadPoolExecutor(VERIFY_WORKERS) as pool:
        return dict(zip(keys, pool.map(one, keys), strict=True))


def failure_lines(verdicts: Mapping[str, Verdict], show_latex: bool) -> list[str]:
    passed = sum(verdict.passed for verdict in verdicts.values())
    lines = [f"passed {passed}/{len(verdicts)}"]
    lines += [
        f"{key}: {verdict.problem}" + (f"\n  yours: {verdict.latex}" if show_latex else "")
        for key, verdict in verdicts.items()
        if not verdict.passed and verdict.problem != SKIP
    ]
    skipped = [key for key, verdict in verdicts.items() if verdict.problem == SKIP]
    if skipped:
        lines.append(f"SKIP {len(skipped)}: {' '.join(skipped)}")
    return lines


def with_packages(tex: str) -> str:
    if RECOGNITION_PACKAGES in tex:
        return tex
    return tex.replace(FONTSPEC_PACKAGE, RECOGNITION_PACKAGES + FONTSPEC_PACKAGE, 1)
