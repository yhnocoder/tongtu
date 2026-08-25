from __future__ import annotations

import time
from pathlib import Path

import pytest

from tongtu.artifacts.survey import SurveyStatus
from tongtu.artifacts.translate import ChunkTranslateStatus, TranslateStatus
from tongtu.stages import mask, survey, translate
from tongtu.workdir import Workdir

from ...conftest import paper_dir

pytestmark = pytest.mark.llm

PAPER = "revtex"

JOBS = 4


def test_translate_revtex_with_a_real_model(tmp_path: Path) -> None:
    workdir = Workdir(tmp_path / PAPER)
    workdir.create()
    (workdir.build / mask.PRECOMPILE_FILENAME).write_text(
        (paper_dir(PAPER) / "main.tex").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert mask.run(workdir).status.value == "ok"
    assert survey.run(workdir).status is SurveyStatus.OK
    started = time.monotonic()
    manifest = translate.run(workdir, jobs=JOBS)
    duration = time.monotonic() - started
    print(f"model {manifest.model} effort {manifest.effort} prompt_version {manifest.prompt_version}")
    print(f"status {manifest.status} duration {duration:.1f}s")
    for chunk_id, record in manifest.chunks.items():
        print(f"  {chunk_id} {record.status} attempts {record.attempts} failures {record.failures}")
    print(f"warnings {manifest.warnings}")
    assert manifest.status is TranslateStatus.OK, manifest.message
    assert manifest.chunks
    assert all(record.status is not ChunkTranslateStatus.FALLBACK for record in manifest.chunks.values())
    for chunk_id in manifest.chunks:
        assert (workdir.build / translate.TRANSLATED_DIRNAME / f"{chunk_id}.tex").read_text(encoding="utf-8").strip()
