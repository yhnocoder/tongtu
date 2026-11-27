from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pymupdf
import pytest

import tongtu.model
from tongtu.artifacts.common import NotesFile, Session, StopReason
from tongtu.artifacts.fetch import FetchManifest, FetchStatus
from tongtu.artifacts.reconstruct import ReconstructStatus, RegionsFile
from tongtu.manifests import load_manifest, timeout_warning, write_manifest
from tongtu.model.work import WorkOutcome
from tongtu.pipeline import outputs_present
from tongtu.reconstruct import review
from tongtu.reconstruct.crops import crop_path
from tongtu.stages import reconstruct
from tongtu.workdir import Workdir

from .conftest import DISPLAY_LATEX, INLINE_LATEX, TABLE_LATEX

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


def recognition_without_output(skill: str, session_dir: Path, **kwargs: object) -> WorkOutcome:
    assert skill != "reconstruct_fix", "reconstruct started a fix session"
    return WorkOutcome(session=Session(stop_reason=StopReason.FINISHED, model="test/offline", duration_seconds=1.0))


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
    monkeypatch.setattr(tongtu.model, "work", recognition_without_output)
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
    assert packed.returncode == 0, packed.stdout[-2000:]
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


def append_before_end(session_dir: Path, lines: str) -> None:
    main = session_dir / "main.tex"
    text = main.read_text(encoding="utf-8")
    main.write_text(text.replace("\\end{document}", lines + "\n\\end{document}"), encoding="utf-8")


def test_packed_page_count_differing_from_the_unpacked_one_is_reconstruct_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))

    def widen_and_add_a_page_only_when_packed(session_dir: Path) -> None:
        widen_text(session_dir)
        append_before_end(session_dir, "\\IfFileExists{regions.json}{}{\\clearpage\\null\\clearpage}")

    monkeypatch.setattr(tongtu.model, "work", fake_fix([], widen_and_add_a_page_only_when_packed))
    workdir = pdf_workdir(tmp_path, small_print_pdf(tmp_path))
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.RECONSTRUCT_FAILED
    assert "the packed source compiles to 2 pages, the unpacked one to 1" in manifest.message
    assert manifest.final_check is not None and manifest.final_check.passed
    assert not (workdir.src / "main.tex").exists()


def test_unreadable_crop_is_reconstruct_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))

    def widen_and_reference_a_broken_crop(session_dir: Path) -> None:
        widen_text(session_dir)
        (session_dir / "crops").mkdir(exist_ok=True)
        (session_dir / "crops" / "r-9998.pdf").write_bytes(b"not a pdf")
        append_before_end(session_dir, "\\IfFileExists{regions.json}{}{\\includegraphics{crops/r-9998.pdf}}")

    monkeypatch.setattr(tongtu.model, "work", fake_fix([], widen_and_reference_a_broken_crop))
    workdir = pdf_workdir(tmp_path, small_print_pdf(tmp_path))
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.RECONSTRUCT_FAILED
    assert "cannot open the crop r-9998.pdf" in manifest.message
    assert not (workdir.src / "main.tex").exists()


def test_draft_compile_without_a_pdf_counts_the_order_as_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))

    def widen_and_stop_the_draft_compile(session_dir: Path) -> None:
        widen_text(session_dir)
        main = session_dir / "main.tex"
        stop = "\\makeatletter\\AtBeginDocument{\\@ifpackagewith{graphicx}{draft}{\\@@end}{}}\\makeatother\n"
        main.write_text(
            main.read_text(encoding="utf-8").replace("\\begin{document}", stop + "\\begin{document}", 1),
            encoding="utf-8",
        )

    monkeypatch.setattr(tongtu.model, "work", fake_fix([], widen_and_stop_the_draft_compile))
    workdir = pdf_workdir(tmp_path, small_print_pdf(tmp_path))
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.OK, manifest.message
    assert manifest.final_check is not None and manifest.final_check.order == 1.0
    assert review.DRAFT_FAILED_LINE in manifest.warnings


def fake_recognition(calls: list[tuple[str, Path, Path]], stop_reason: StopReason) -> Callable[..., WorkOutcome]:
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
        assert skill != "reconstruct_fix", "reconstruct started a fix session"
        assert tongtu_python
        calls.append((skill, session_dir, log_path))
        if stop_reason is not StopReason.ERROR:
            hints = json.loads((session_dir / "hints.json").read_text(encoding="utf-8"))
            regions = RegionsFile.model_validate_json(
                (session_dir.parent / reconstruct.STAGE_NAME / "regions.json").read_text(encoding="utf-8")
            )
            region_hints = {region.id: region.hint for region in regions.regions}
            transcriptions = {}
            for key in hints:
                if skill == "reconstruct_table":
                    transcriptions[key] = TABLE_LATEX
                elif key.startswith("e-"):
                    transcriptions[key] = DISPLAY_LATEX
                else:
                    transcriptions[key] = "z_i" if region_hints[key] == "yi" else INLINE_LATEX[region_hints[key]]
            (session_dir / "rec.json").write_text(json.dumps(transcriptions), encoding="utf-8")
            note = {"where": next(iter(hints)), "problem": "p", "resolution": "r", "suggestion": "s"}
            (session_dir / "notes.json").write_text(json.dumps({"notes": [note]}), encoding="utf-8")
        log_path.write_text("simulated recognition\n")
        return WorkOutcome(session=Session(stop_reason=stop_reason, model="test/offline", duration_seconds=1.0))

    return work


def test_recognized_formulas_and_tables_replace_their_crops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recognition_pdf: Path
) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    calls: list[tuple[str, Path, Path]] = []
    monkeypatch.setattr(tongtu.model, "work", fake_recognition(calls, StopReason.FINISHED))
    workdir = pdf_workdir(tmp_path, recognition_pdf)
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.OK, manifest.message
    assert calls == [
        ("reconstruct_formula", workdir.sandbox("reconstruct_formula"), workdir.reconstruct_formula_log),
        ("reconstruct_table", workdir.sandbox("reconstruct_table"), workdir.reconstruct_table_log),
    ]
    assert manifest.formulas is not None and (manifest.formulas.total, manifest.formulas.passed) == (4, 3)
    assert manifest.tables is not None and (manifest.tables.total, manifest.tables.passed) == (1, 1)
    assert manifest.formulas.session is not None and manifest.formulas.session.model == "test/offline"
    assert manifest.rule_check is not None and manifest.rule_check.passed
    assert manifest.fix_session is None
    assert manifest.warnings == []
    tex = (workdir.src / "main.tex").read_text(encoding="utf-8")
    assert "\\begin{equation*}\\mathrm{Attention}" in tex and "\\tag*{(1)}\\end{equation*}" in tex
    assert "$x_i^2$ + " in tex and "$/\\sqrt{d_k}$ to" in tex
    assert tex.count("\\im{") == 1
    assert "\\resizebox{" in tex and "\\begin{tabular}{lcc}" in tex
    notes_file = load_manifest(workdir.notes, NotesFile)
    assert notes_file is not None
    assert [note.session for note in notes_file.notes] == ["reconstruct_formula", "reconstruct_table"]
    packed = subprocess.run(
        ["latexmk", "-xelatex", "-interaction=nonstopmode", "main.tex"], cwd=workdir.src, capture_output=True
    )
    assert packed.returncode == 0, packed.stdout[-2000:]
    assert len(pymupdf.open(workdir.src / "main.pdf")) == manifest.final_check.pages


def test_failed_recognition_sessions_keep_every_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recognition_pdf: Path
) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    calls: list[tuple[str, Path, Path]] = []
    monkeypatch.setattr(tongtu.model, "work", fake_recognition(calls, StopReason.ERROR))
    workdir = pdf_workdir(tmp_path, recognition_pdf)
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.OK, manifest.message
    assert len(calls) == 2
    assert manifest.formulas is not None and (manifest.formulas.total, manifest.formulas.passed) == (4, 0)
    assert manifest.tables is not None and (manifest.tables.total, manifest.tables.passed) == (1, 0)
    assert manifest.tables.session is not None and manifest.tables.session.stop_reason is StopReason.ERROR
    assert "the reconstruct_formula session ended with error" in manifest.warnings[0]
    assert "the reconstruct_table session wrote no rec.json; every crop stays an image" in manifest.warnings
    tex = (workdir.src / "main.tex").read_text(encoding="utf-8")
    assert tex.count("\\im{") == 3
    assert "\\resizebox" not in tex and "equation*" not in tex


def test_rebuild_without_formulas_or_tables_starts_no_recognition_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    calls: list[Path] = []
    monkeypatch.setattr(tongtu.model, "work", fake_fix(calls, widen_text))
    workdir = pdf_workdir(tmp_path, small_print_pdf(tmp_path))
    manifest = reconstruct.run(workdir, mode="fast")
    assert (manifest.formulas, manifest.tables) == (None, None)
    assert calls == [workdir.sandbox(reconstruct.STAGE_NAME)]


def test_timed_out_recognition_sessions_are_still_verified_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recognition_pdf: Path
) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tongtu.model, "work", fake_recognition([], StopReason.TIMEOUT))
    workdir = pdf_workdir(tmp_path, recognition_pdf)
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.OK, manifest.message
    assert manifest.formulas is not None and (manifest.formulas.total, manifest.formulas.passed) == (4, 3)
    assert manifest.formulas.session is not None and manifest.formulas.session.stop_reason is StopReason.TIMEOUT
    assert manifest.tables is not None and manifest.tables.passed == 1
    assert manifest.warnings == [timeout_warning("reconstruct_formula"), timeout_warning("reconstruct_table")]


def test_crop_broken_during_recognition_is_reconstruct_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recognition_pdf: Path
) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    recognize = fake_recognition([], StopReason.TIMEOUT)
    broken: list[str] = []

    def work(skill: str, session_dir: Path, **kwargs: object) -> WorkOutcome:
        outcome = recognize(skill, session_dir, **kwargs)
        key = next(iter(json.loads((session_dir / "hints.json").read_text(encoding="utf-8"))))
        (session_dir.parent / reconstruct.STAGE_NAME / crop_path(key)).write_bytes(b"not a pdf")
        broken.append(key)
        return outcome

    monkeypatch.setattr(tongtu.model, "work", work)
    workdir = pdf_workdir(tmp_path, recognition_pdf)
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.RECONSTRUCT_FAILED
    assert manifest.message.startswith(f"cannot open the crop {broken[0]}.pdf")
    assert manifest.warnings == [timeout_warning("reconstruct_formula")]
    assert manifest.formulas is not None and manifest.formulas.session is not None
    assert (manifest.formulas.total, manifest.formulas.passed) == (4, 0)
    assert manifest.tables is None
    assert not outputs_present(workdir, "reconstruct")
