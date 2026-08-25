from __future__ import annotations

import time

import pytest
from typer.testing import CliRunner

from tongtu.artifacts.review import ReviewManifest, ReviewStatus
from tongtu.cli import app
from tongtu.stages import review
from tongtu.workdir import Workdir, resolve

from ...conftest import paper_dir

pytestmark = pytest.mark.llm

PAPER = "revtex"

runner = CliRunner()


def test_review_revtex_with_a_real_session() -> None:
    paper_workdir = Workdir(resolve(PAPER))
    translated = paper_workdir.build / review.TRANSLATED_DIRNAME
    if not translated.is_dir() or not any(translated.glob("*.tex")):
        pytest.skip(f"{translated} 里没有译文，先跑 tongtu run {paper_dir(PAPER)}")
    started = time.monotonic()
    result = runner.invoke(app, ["stage", "review", str(paper_dir(PAPER))])
    duration = time.monotonic() - started
    manifest = ReviewManifest.model_validate_json(
        paper_workdir.manifest_path(review.STAGE_NAME).read_text(encoding="utf-8")
    )
    print(f"status {manifest.status} duration {duration:.1f}s session {manifest.session}")
    print(f"changed {manifest.changed}")
    print(f"reverted {manifest.reverted}")
    print(f"warnings {manifest.warnings} message {manifest.message}")
    assert result.exit_code == 0, manifest.message
    assert manifest.status is ReviewStatus.OK
    assert manifest.session.stop_reason == "finished"
    assert set(manifest.reverted) <= set(manifest.changed)
    for path in sorted((paper_workdir.build / review.CHUNKS_DIRNAME).glob("*.tex")):
        assert (paper_workdir.build / review.REVIEWED_DIRNAME / path.name).read_text(encoding="utf-8").strip()
