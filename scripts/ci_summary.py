#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from tongtu.artifacts.common import Manifest
from tongtu.artifacts.compile import CompileManifest
from tongtu.artifacts.fetch import FetchManifest
from tongtu.artifacts.mask import MaskManifest
from tongtu.artifacts.precompile import PrecompileManifest
from tongtu.artifacts.review import ReviewManifest
from tongtu.artifacts.survey import SurveyManifest
from tongtu.artifacts.translate import TranslateManifest
from tongtu.cli import _stage_summary
from tongtu.manifests import load_manifest
from tongtu.pipeline import STAGES
from tongtu.workdir import Workdir

MANIFEST_CLASSES: dict[str, type[Manifest]] = {
    "fetch": FetchManifest,
    "precompile": PrecompileManifest,
    "mask": MaskManifest,
    "survey": SurveyManifest,
    "translate": TranslateManifest,
    "review": ReviewManifest,
    "compile": CompileManifest,
}

LOGS_PREFIX = "logs-"

PDF_PREFIX = "pdf-"

CI_FILENAME = "ci.json"

ABSENT_CELL = "—"


@dataclass(frozen=True)
class PaperRow:
    paper: str
    manifests: dict[str, Manifest | None]
    seconds: int | None


def read_rows(papers_dir: Path) -> list[PaperRow]:
    rows = []
    for entry in sorted(papers_dir.iterdir()):
        if not entry.is_dir() or not entry.name.startswith(LOGS_PREFIX):
            continue
        workdir = Workdir(entry)
        manifests = {stage: load_manifest(workdir.manifest_path(stage), MANIFEST_CLASSES[stage]) for stage in STAGES}
        rows.append(PaperRow(entry.name.removeprefix(LOGS_PREFIX), manifests, _read_seconds(entry / CI_FILENAME)))
    return rows


def _read_seconds(path: Path) -> int | None:
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["seconds"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def render(rows: list[PaperRow], artifacts: dict[str, int], repo: str, run_id: str) -> str:
    def link(text: str, name: str) -> str:
        artifact_id = artifacts.get(name)
        if artifact_id is None:
            return text
        return f"[{text}](https://github.com/{repo}/actions/runs/{run_id}/artifacts/{artifact_id})"

    header = ["paper", *STAGES, "summary", "logs", "total"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    notes = []
    for row in rows:
        cells = [link(row.paper, PDF_PREFIX + row.paper)]
        summaries = []
        for stage in STAGES:
            manifest = row.manifests[stage]
            cells.append(manifest.status if manifest is not None else ABSENT_CELL)
            if manifest is None:
                continue
            if manifest.ok:
                summaries.append(_stage_summary(manifest))
            else:
                notes.append(f"- **{row.paper} / {stage}**: {manifest.message}")
        cells.append("; ".join(summary for summary in summaries if summary))
        cells.append(link("logs", LOGS_PREFIX + row.paper))
        cells.append(_duration(row.seconds))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join([*lines, "", *notes]) + "\n"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return ABSENT_CELL
    return f"{seconds // 60}m{seconds % 60:02d}s"


def main(argv: list[str]) -> int:
    papers_dir, artifacts_json = Path(argv[0]), Path(argv[1])
    artifacts = {item["name"]: int(item["id"]) for item in json.loads(artifacts_json.read_text(encoding="utf-8"))}
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    sys.stdout.write(render(read_rows(papers_dir), artifacts, repo, run_id))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
