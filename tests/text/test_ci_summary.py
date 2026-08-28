from __future__ import annotations

import json
from pathlib import Path

from scripts import ci_summary
from tongtu.artifacts.compile import CompileManifest, CompileStatus
from tongtu.artifacts.fetch import FetchManifest, FetchStatus
from tongtu.artifacts.translate import ChunkTranslateRecord, ChunkTranslateStatus, TranslateManifest, TranslateStatus
from tongtu.manifests import write_manifest
from tongtu.workdir import Workdir

ARTIFACTS = {"paper-article": 12, "paper-2002.05202": 13}


def write_paper(papers_dir: Path, paper: str, seconds: int | None) -> Workdir:
    workdir = Workdir(papers_dir / f"paper-{paper}")
    workdir.create()
    if seconds is not None:
        (workdir.path / "ci.json").write_text(json.dumps({"paper": paper, "seconds": seconds, "exit_code": 0}))
        workdir.zh_pdf.write_bytes(b"%PDF")
    return workdir


def test_rows_and_render(tmp_path: Path) -> None:
    good = write_paper(tmp_path, "article", 38 * 60 + 12)
    write_manifest(
        good.manifest_path("fetch"), FetchManifest(status=FetchStatus.OK, source="x", kind="local", tex_files=["a"])
    )
    write_manifest(
        good.manifest_path("translate"),
        TranslateManifest(
            status=TranslateStatus.OK,
            chunks={
                "c1": ChunkTranslateRecord(status=ChunkTranslateStatus.TRANSLATED, attempts=1),
                "c2": ChunkTranslateRecord(status=ChunkTranslateStatus.FALLBACK, attempts=2),
            },
        ),
    )
    bad = write_paper(tmp_path, "2002.05202", None)
    write_manifest(
        bad.manifest_path("compile"),
        CompileManifest(status=CompileStatus.COMPILE_FAILED, message="xelatex exited 1"),
    )

    rows = ci_summary.read_rows(tmp_path)

    assert [row.paper for row in rows] == ["2002.05202", "article"]
    assert rows[1].seconds == 2292
    assert rows[0].seconds is None
    assert rows[0].manifests["fetch"] is None

    text = ci_summary.render(rows, ARTIFACTS, "yhnocoder/tongtu", "99")

    assert "[article](https://github.com/yhnocoder/tongtu/releases/download/papers-ci/99-article.pdf)" in text
    assert rows[0].has_pdf is False
    assert "[artifact](https://github.com/yhnocoder/tongtu/actions/runs/99/artifacts/12)" in text
    assert "[artifact](https://github.com/yhnocoder/tongtu/actions/runs/99/artifacts/13)" in text
    assert "| 2002.05202 |" in text
    assert "| ok | — | — | — | ok | — | — | local, 1 tex files; 2 chunks, 1 fallback |" in text
    assert "| 38m12s |" in text
    assert "| compile_failed |" in text
    assert "- **2002.05202 / compile**: xelatex exited 1" in text
    assert "article / " not in text
