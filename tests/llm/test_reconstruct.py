from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tongtu.artifacts.fetch import FetchManifest, FetchStatus
from tongtu.artifacts.reconstruct import ReconstructStatus
from tongtu.manifests import write_manifest
from tongtu.model.config import ModeName
from tongtu.stages import reconstruct
from tongtu.workdir import Workdir

pytestmark = pytest.mark.llm

PDF_URL = "https://arxiv.org/pdf/1706.03762v7"

FORMULA_PASS_RATE_MIN = 0.85


def test_attention_paper_formulas_and_tables_are_recognized(tmp_path: Path) -> None:
    workdir = Workdir(tmp_path / "1706.03762v7")
    workdir.create()
    response = httpx.get(PDF_URL, follow_redirects=True, timeout=120)
    response.raise_for_status()
    workdir.original_pdf.write_bytes(response.content)
    fetched = FetchManifest(status=FetchStatus.OK, source=PDF_URL, kind="local_pdf", route="pdf")
    write_manifest(workdir.manifest_path("fetch"), fetched)
    manifest = reconstruct.run(workdir, mode=ModeName.FAST)
    print(manifest.model_dump_json(indent=2))
    assert manifest.status is ReconstructStatus.OK, manifest.message
    assert manifest.final_check is not None and manifest.final_check.passed
    assert manifest.formulas is not None and manifest.formulas.total > 0
    assert manifest.formulas.passed / manifest.formulas.total >= FORMULA_PASS_RATE_MIN
    assert manifest.tables is not None and manifest.tables.passed >= 1
    assert workdir.reconstruct_formula_log.stat().st_size > 0
    assert workdir.reconstruct_table_log.stat().st_size > 0
