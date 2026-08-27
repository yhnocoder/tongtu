from __future__ import annotations

import re
from pathlib import Path

PAGES_RE = re.compile(r"Output written on .*?\((\d+) pages?")

OVERFULL_HBOX_PREFIX = "Overfull \\hbox"
UNDEFINED_REFERENCE_PREFIX = "LaTeX Warning: Reference"
UNDEFINED_CITATION_PREFIX = "LaTeX Warning: Citation"
MISSING_CHARACTER_MARKER = "Missing character"

ERROR_LINE_PREFIX = "!"


def read_log(log_path: Path) -> str | None:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def parse_counts(log_text: str | None) -> dict[str, int]:
    pages_matches = PAGES_RE.findall(log_text) if log_text is not None else []
    lines = log_text.splitlines() if log_text is not None else []
    return {
        "pages": int(pages_matches[-1]) if pages_matches else 0,
        "overfull_hboxes": sum(1 for line in lines if line.startswith(OVERFULL_HBOX_PREFIX)),
        "undefined_references": sum(1 for line in lines if line.startswith(UNDEFINED_REFERENCE_PREFIX)),
        "undefined_citations": sum(1 for line in lines if line.startswith(UNDEFINED_CITATION_PREFIX)),
        "missing_characters": sum(1 for line in lines if MISSING_CHARACTER_MARKER in line),
    }


def error_lines(log_text: str, limit: int) -> list[str]:
    return [line for line in log_text.splitlines() if line.startswith(ERROR_LINE_PREFIX)][:limit]
