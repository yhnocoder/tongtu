from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tongtu.artifacts.common import NotesFile, StopReason
from tongtu.artifacts.precompile import PrecompileStatus
from tongtu.artifacts.translate import TranslateStatus
from tongtu.manifests import load_manifest
from tongtu.model.config import ModeName
from tongtu.stages import mask, precompile, translate
from tongtu.workdir import Workdir

from ..conftest import paper_dir

pytestmark = pytest.mark.llm

PAPER = "article"


def test_translate_article_in_fast_mode(tmp_path: Path) -> None:
    workdir = Workdir(tmp_path / PAPER)
    shutil.copytree(paper_dir(PAPER), workdir.src)
    workdir.create()
    assert precompile.run(workdir, mode=ModeName.FAST).status is PrecompileStatus.OK
    assert mask.run(workdir).status.value == "ok"
    manifest = translate.run(workdir, mode=ModeName.FAST)
    print(manifest.model_dump_json(indent=2))
    assert manifest.status is TranslateStatus.OK, manifest.message
    assert manifest.session is not None and manifest.session.stop_reason is StopReason.FINISHED
    assert manifest.paragraphs_translated == manifest.paragraphs_total > 0
    assert workdir.translated.read_text(encoding="utf-8").strip()
    assert workdir.translate_log.stat().st_size > 0
    assert load_manifest(workdir.notes, NotesFile) is not None
