from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from tongtu import masking, synctex

REPO_ROOT = Path(__file__).resolve().parent.parent

PAPERS_DIR = REPO_ROOT / "examples" / "papers"

FIXTURE_PAPERS: tuple[str, ...] = ("article", "revtex", "conference")


@pytest.fixture(scope="session")
def environments_table() -> Mapping[str, masking.TableEntry]:
    content = masking.ENVIRONMENTS_TABLE_PATH.read_text(encoding="utf-8")
    return masking.parse_environment_table(content)


def paper_dir(name: str) -> Path:
    return PAPERS_DIR / name


def tex_sources(name: str) -> list[Path]:
    return sorted(paper_dir(name).rglob("*.tex"))


def synctex_raw(points: float) -> int:
    return round(points * synctex.TEX_POINTS_PER_INCH / synctex.PDF_POINTS_PER_INCH * synctex.SCALED_POINTS_PER_POINT)


def synctex_record(kind: str, tag: int, line: int, x: float, y: float, *size: float) -> str:
    record = f"{kind}{tag},{line}:{synctex_raw(x)},{synctex_raw(y)}"
    if size:
        record += ":" + ",".join(str(synctex_raw(value)) for value in size)
    return record


def synctex_file(inputs: Mapping[int, str], pages: Sequence[Sequence[str]]) -> str:
    lines = [
        "SyncTeX Version:1",
        *(f"Input:{tag}:{path}" for tag, path in inputs.items()),
        "Output:pdf",
        "Magnification:1000",
        "Unit:1",
        "X Offset:0",
        "Y Offset:0",
        "Content:",
    ]
    for number, page in enumerate(pages, start=1):
        lines.extend([f"{{{number}", *page, f"}}{number}"])
    lines.append("Postamble:")
    return "\n".join(lines) + "\n"
