from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from pypdf import PdfReader

import tongtu.model
from tests.conftest import FIXTURE_PAPERS, paper_dir
from tongtu.artifacts.compile import AnchorKind, AnchorsFile, CompileStatus
from tongtu.artifacts.precompile import PrecompileStatus
from tongtu.artifacts.survey import BriefFile, ChunkRecord, Part
from tongtu.model.work import WorkOutcome
from tongtu.stages import compile, mask, precompile
from tongtu.workdir import Workdir

pytestmark = pytest.mark.compile

INVISIBLE_RE = re.compile(r"%[^\n]*|\\[A-Za-z]+\*?(?:\{[^{}]*\})*|\s")


def untranslated_workdir(tmp_path: Path, paper: str) -> Workdir:
    workdir = Workdir(tmp_path / paper)
    shutil.copytree(paper_dir(paper), workdir.src)
    workdir.create()
    assert precompile.run(workdir).status is PrecompileStatus.OK
    assert mask.run(workdir).status == "ok"
    masked = workdir.masked.read_text(encoding="utf-8")
    for area in (workdir.chunks, workdir.reviewed):
        area.mkdir()
        (area / "c000.tex").write_text(masked, encoding="utf-8")
    record = ChunkRecord(
        id="c000", start=0, end=len(masked), part=Part.BODY, tokens=1, paragraphs=1, translatable_chars=1
    )
    workdir.brief.write_text(BriefFile(chunks=[record]).model_dump_json(indent=2), encoding="utf-8")
    return workdir


@pytest.mark.parametrize("paper", FIXTURE_PAPERS)
def test_anchors_cover_the_compiled_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paper: str) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))

    def work(role: str, tree: Path, **kwargs: object) -> WorkOutcome:
        raise AssertionError(f"{paper} must compile without a fix session")

    monkeypatch.setattr(tongtu.model, "work", work)
    workdir = untranslated_workdir(tmp_path, paper)
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK, manifest.message
    assert not [warning for warning in manifest.warnings if workdir.zh_anchors.name in warning]
    assert workdir.zh_synctex.is_file()
    result = AnchorsFile.model_validate_json(workdir.zh_anchors.read_text(encoding="utf-8"))
    assert (result.pdf, result.tex) == (workdir.zh_pdf.name, workdir.zh_tex.name)
    pages = PdfReader(workdir.zh_pdf).pages
    assert len(result.pages) == len(pages)
    assert all(page.body is not None for page in result.pages)
    zh_lines = workdir.zh_tex.read_text(encoding="utf-8").split("\n")
    sizes = {page.page: page for page in result.pages}
    for block in result.blocks:
        assert 1 <= block.first_line <= block.last_line <= len(zh_lines)
        for rect in block.rects:
            assert 0 <= rect.x0 < rect.x1 <= sizes[rect.page].width
            assert 0 <= rect.y0 < rect.y1 <= sizes[rect.page].height
    ids = {block.id for block in result.blocks}
    assert all(block.parent in ids for block in result.blocks if block.parent is not None)
    assert any(block.kind is AnchorKind.HEADING for block in result.blocks)
    unplaced = {AnchorKind.TEXT, AnchorKind.METADATA}
    assert [block.id for block in result.blocks if block.kind not in unplaced and not block.rects] == []
    texts = [
        block
        for block in result.blocks
        if block.kind is AnchorKind.TEXT
        and INVISIBLE_RE.sub("", "\n".join(zh_lines[block.first_line - 1 : block.last_line]))
    ]
    assert texts and [block.id for block in texts if not block.rects] == []
