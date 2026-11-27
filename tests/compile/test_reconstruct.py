from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pymupdf
import pytest

import tongtu.model
from tongtu.artifacts.common import Session, StopReason
from tongtu.artifacts.fetch import FetchManifest, FetchStatus
from tongtu.artifacts.reconstruct import ReconstructStatus
from tongtu.manifests import write_manifest
from tongtu.model.work import WorkOutcome
from tongtu.pipeline import outputs_present
from tongtu.stages import reconstruct
from tongtu.workdir import Workdir

pytestmark = pytest.mark.compile

TEMPLATES = Path(__file__).resolve().parents[2] / "examples" / "papers"

NARROW_WORDS = "the quick brown fox jumps over lazy dogs while seven wizards quietly hex jovial judges".split()

TEXTWIDTH_RE = re.compile(r"textwidth=[0-9.]+bp")

FixAction = Callable[[Path], None]


def pdf_workdir(tmp_path: Path, pdf: Path) -> Workdir:
    workdir = Workdir(tmp_path / "paper")
    workdir.create()
    shutil.copyfile(pdf, workdir.original_pdf)
    manifest = FetchManifest(status=FetchStatus.OK, source=str(pdf), kind="local_pdf", route="pdf")
    write_manifest(workdir.manifest_path("fetch"), manifest)
    return workdir


def template_pdf(tmp_path: Path, name: str) -> Path:
    tree = tmp_path / "template"
    shutil.copytree(TEMPLATES / name, tree)
    subprocess.run(
        ["latexmk", "-pdf", "-interaction=nonstopmode", "main.tex"], cwd=tree, capture_output=True, check=True
    )
    return tree / "main.pdf"


def small_print_pdf(tmp_path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 30.0
    line_number = 0
    while y < 820:
        line = " ".join(
            NARROW_WORDS[(line_number + k) % len(NARROW_WORDS)] + "x" * ((line_number + k) % 7) for k in range(15)
        )
        page.insert_text((20, y), line, fontsize=4)
        y += 5
        line_number += 3
    path = tmp_path / "small.pdf"
    doc.save(path)
    return path


def no_session(*args: object, **kwargs: object) -> WorkOutcome:
    raise AssertionError("reconstruct started a fix session")


def fake_fix(calls: list[Path], action: FixAction) -> Callable[..., WorkOutcome]:
    def work(
        skill: str,
        session_dir: Path,
        *,
        mode: str,
        log_path: Path,
        report=None,
        extra_prompt: str = "",
        tongtu_python: bool = False,
    ) -> WorkOutcome:
        assert skill == "reconstruct_fix"
        assert tongtu_python
        calls.append(session_dir)
        action(session_dir)
        log_path.write_text("simulated fix\n")
        return WorkOutcome(session=Session(stop_reason=StopReason.FINISHED, model="test/offline", duration_seconds=1.0))

    return work


def widen_text(session_dir: Path) -> None:
    main = session_dir / "main.tex"
    main.write_text(TEXTWIDTH_RE.sub("textwidth=555bp", main.read_text(encoding="utf-8"), count=1), encoding="utf-8")


@pytest.mark.parametrize("name", ["article", "revtex"])
def test_template_pdf_is_rebuilt_by_the_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tongtu.model, "work", no_session)
    workdir = pdf_workdir(tmp_path, template_pdf(tmp_path, name))
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.OK, manifest.message
    assert manifest.rebuilt
    assert manifest.rule_check is not None and manifest.rule_check.passed
    assert manifest.final_check == manifest.rule_check
    assert manifest.fix_session is None
    assert sum(manifest.regions.values()) > 0
    assert sorted(path.name for path in workdir.src.iterdir()) == ["crops.pdf", "main.tex"]
    packed = subprocess.run(
        ["latexmk", "-xelatex", "-interaction=nonstopmode", "main.tex"], cwd=workdir.src, capture_output=True
    )
    assert (workdir.src / "main.pdf").is_file(), packed.stdout[-2000:]
    assert len(pymupdf.open(workdir.src / "main.pdf")) == manifest.final_check.pages
    assert (workdir.sandbox(reconstruct.STAGE_NAME) / "regions.json").is_file()
    assert outputs_present(workdir, "reconstruct")


def test_failed_rule_check_runs_the_fix_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    calls: list[Path] = []
    monkeypatch.setattr(tongtu.model, "work", fake_fix(calls, widen_text))
    workdir = pdf_workdir(tmp_path, small_print_pdf(tmp_path))
    manifest = reconstruct.run(workdir, mode="fast")
    assert calls == [workdir.sandbox(reconstruct.STAGE_NAME)]
    assert manifest.status is ReconstructStatus.OK, manifest.message
    assert manifest.rule_check is not None and not manifest.rule_check.passed
    assert manifest.final_check is not None and manifest.final_check.passed
    assert manifest.fix_session is not None and manifest.fix_session.model == "test/offline"
    assert (workdir.src / "main.tex").is_file()


def test_check_still_failing_after_the_fix_session_is_reconstruct_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    calls: list[Path] = []
    monkeypatch.setattr(tongtu.model, "work", fake_fix(calls, lambda session_dir: None))
    workdir = pdf_workdir(tmp_path, small_print_pdf(tmp_path))
    manifest = reconstruct.run(workdir, mode="fast")
    assert len(calls) == 1
    assert manifest.status is ReconstructStatus.RECONSTRUCT_FAILED
    assert "after the fix session" in manifest.message
    assert "page ratio" in manifest.message
    assert manifest.final_check is not None and not manifest.final_check.passed
    assert not (workdir.src / "main.tex").exists()
    assert not outputs_present(workdir, "reconstruct")


def test_crop_missing_after_the_fix_session_is_reconstruct_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))

    def widen_and_reference_a_lost_crop(session_dir: Path) -> None:
        widen_text(session_dir)
        main = session_dir / "main.tex"
        text = main.read_text(encoding="utf-8")
        main.write_text(
            text.replace(
                "\\end{document}",
                "\\IfFileExists{crops/r-9999.pdf}{\\includegraphics{crops/r-9999.pdf}}{}\n\\end{document}",
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(tongtu.model, "work", fake_fix([], widen_and_reference_a_lost_crop))
    workdir = pdf_workdir(tmp_path, small_print_pdf(tmp_path))
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.RECONSTRUCT_FAILED
    assert "r-9999" in manifest.message
