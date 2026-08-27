from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from tongtu import masking

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
