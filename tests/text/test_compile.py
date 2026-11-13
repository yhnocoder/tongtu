from __future__ import annotations

import gzip
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from pypdf import PdfWriter

import tongtu.model
from tongtu import cropping, processes
from tongtu.artifacts.common import CompileReport
from tongtu.artifacts.compile import AnchorKind, AnchorRect, AnchorsFile, CompileManifest, CompileStatus
from tongtu.artifacts.precompile import PrecompileManifest, PrecompileStatus
from tongtu.artifacts.survey import BriefFile, ChunkRecord, Part
from tongtu.manifests import write_manifest
from tongtu.model.work import StopReason, WorkOutcome
from tongtu.pipeline import outputs_present
from tongtu.processes import ProcessOutcome
from tongtu.stages import compile, mask
from tongtu.workdir import Workdir

from ..conftest import synctex_file, synctex_record

PAPER = """\\documentclass{article}
\\begin{document}
\\section{Intro}

Hello $x$ world with \\emph{stress}.

\\begin{equation}
x = 1
\\end{equation}

\\begin{figure}
\\centering
\\caption{A figure caption.}
\\end{figure}

Second paragraph here.

\\end{document}
"""

TRANSLATIONS: tuple[tuple[str, str], ...] = (
    ("Intro", "引言"),
    ("Hello $x$ world with \\emph{stress}.", "你好 $x$ 世界 \\emph{强调}。"),
    ("A figure caption.", "一个图题。"),
    ("First line. \\par Second line.", "第一行。 \\par 第二行。"),
    ("Second paragraph here.", "第二段。"),
)

SPLIT_MARKER = "\n\nSecond"

LOG_OK = "Output written on zh.xdv (5 pages, 12345 bytes).\n"

LOG_NOISY = """Output written on zh.xdv (5 pages, 12345 bytes).
Overfull \\hbox (10.0pt too wide) in paragraph
LaTeX Warning: Reference `fig:x' on page 1 undefined
LaTeX Warning: Citation `adam' on page 2 undefined
Missing character: There is no X in font
"""

LOG_ERROR = "! Undefined control sequence.\nl.42 \\pdfoutput\nOutput written on zh.xdv (0 pages, 8 bytes).\n"

PAGE_WIDTH = 612.0

PAGE_HEIGHT = 792.0

PAGES = 5

TEXT_LINES: tuple[tuple[int, float], ...] = ((3, 100.0), (5, 130.0), (8, 160.0), (13, 190.0), (16, 220.0))


def fake_synctex(main: Path) -> str:
    page = [
        synctex_record("[", 1, 2, 72, 720, 468, 648, 0),
        synctex_record("[", 1, 2, 72, 720, 468, 648, 0),
    ]
    for line, baseline in TEXT_LINES:
        page.extend(
            [
                synctex_record("(", 1, line, 72, baseline, 468, 10, 2),
                synctex_record("x", 1, line, 80, baseline),
                synctex_record("g", 1, line, 200, baseline),
                synctex_record("k", 1, line, 300, baseline, 1),
                ")",
            ]
        )
    page.extend(["]", "]"])
    return synctex_file({1: f"{main.parent}/./{main.name}"}, [page, *([[]] * (PAGES - 1))])


def fake_pdf(path: Path) -> None:
    writer = PdfWriter()
    for _ in range(PAGES):
        writer.add_blank_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    with path.open("wb") as stream:
        writer.write(stream)


def line_rect(baseline: float) -> AnchorRect:
    return AnchorRect(page=1, x0=72.0, y0=PAGE_HEIGHT - baseline - 2, x1=540.0, y1=PAGE_HEIGHT - baseline + 10)


BASELINE = CompileReport(
    pages=5,
    pdf_bytes=1000,
    overfull_hboxes=0,
    undefined_references=0,
    undefined_citations=0,
    missing_characters=0,
    duration_seconds=1.0,
)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))


def translate(text: str) -> str:
    for source, target in TRANSLATIONS:
        text = text.replace(source, target)
    return text


def make_workdir(tmp_path: Path, paper: str = PAPER) -> Workdir:
    workdir = Workdir(tmp_path / "paper")
    workdir.create()
    (workdir.src / "main.tex").write_text(paper, encoding="utf-8")
    (workdir.src / "figure.pdf").write_bytes(b"%PDF")
    (workdir.build / "precompile.tex").write_text(paper, encoding="utf-8")
    assert mask.run(workdir).status == "ok"
    masked = (workdir.build / "masked.tex").read_text(encoding="utf-8")
    split = masked.index(SPLIT_MARKER)
    bodies = {"c000": masked[:split], "c001": masked[split:]}
    for dirname, mapping in (("chunks", bodies), ("reviewed", {k: translate(v) for k, v in bodies.items()})):
        (workdir.build / dirname).mkdir()
        for chunk_id, body in mapping.items():
            (workdir.build / dirname / f"{chunk_id}.tex").write_text(body, encoding="utf-8")
    records = [
        ChunkRecord(id="c000", start=0, end=split, part=Part.BODY, tokens=1, paragraphs=1, translatable_chars=1),
        ChunkRecord(
            id="c001", start=split, end=len(masked), part=Part.BODY, tokens=1, paragraphs=1, translatable_chars=1
        ),
    ]
    (workdir.build / "brief.json").write_text(BriefFile(chunks=records).model_dump_json(indent=2), encoding="utf-8")
    write_manifest(
        workdir.manifest_path("precompile"),
        PrecompileManifest(status=PrecompileStatus.OK, main_file="main.tex", report=BASELINE),
    )
    tree = workdir.build / "sandbox" / "tex"
    tree.mkdir(parents=True)
    (tree / "main.tex").write_text(paper, encoding="utf-8")
    (tree / "figure.pdf").write_bytes(b"%PDF")
    return workdir


def wire_latexmk(monkeypatch: pytest.MonkeyPatch, specs: list[dict]) -> dict[str, int]:
    calls = {"compile": 0, "clean": 0}

    def run(command: list[str], cwd: Path, timeout: float, **kwargs: object) -> ProcessOutcome:
        main = Path(command[-1])
        assert main.name == "main.tex"
        if "-C" in command:
            calls["clean"] += 1
            for suffix in (".pdf", ".log", ".synctex.gz"):
                (cwd / main.with_suffix(suffix)).unlink(missing_ok=True)
            return ProcessOutcome(returncode=0, stdout=b"", stderr=b"", timed_out=False, duration_seconds=0.1)
        spec = specs[min(calls["compile"], len(specs) - 1)]
        calls["compile"] += 1
        if spec.get("timeout"):
            return ProcessOutcome(returncode=-9, stdout=b"", stderr=b"", timed_out=True, duration_seconds=600.0)
        if spec.get("pdf", True):
            fake_pdf(cwd / main.with_suffix(".pdf"))
            if spec.get("synctex", True):
                synctex = fake_synctex(cwd / main).encode("utf-8")
                (cwd / main.with_suffix(".synctex.gz")).write_bytes(gzip.compress(synctex))
        (cwd / main.with_suffix(".log")).write_text(spec.get("log", LOG_OK), encoding="utf-8")
        return ProcessOutcome(
            returncode=spec.get("returncode", 0), stdout=b"", stderr=b"", timed_out=False, duration_seconds=2.5
        )

    monkeypatch.setattr(processes, "run_in_process_group", run)
    return calls


def wire_work(
    monkeypatch: pytest.MonkeyPatch,
    stop_reason: StopReason = StopReason.FINISHED,
    edit: Callable[[Path], None] | None = None,
) -> list[dict]:
    calls: list[dict] = []

    def fake_work(
        role: str,
        workdir: Path,
        *,
        trace_path: Path,
        report=None,
        prompt=None,
    ):
        calls.append({"role": role, "workdir": workdir, "trace_path": trace_path})
        (trace_path).write_text("{}\n", encoding="utf-8")
        if report is not None:
            report("Bash: ls")
        if edit is not None:
            edit(workdir)
        return WorkOutcome(stop_reason=stop_reason, detail="runtime missing", model="claude_code/claude-sonnet-5")

    monkeypatch.setattr(tongtu.model, "work", fake_work)
    return calls


def read_manifest(workdir: Workdir) -> CompileManifest:
    return CompileManifest.model_validate_json(workdir.manifest_path("compile").read_text(encoding="utf-8"))


def assert_failed(workdir: Workdir, manifest: CompileManifest) -> None:
    assert manifest.status is CompileStatus.COMPILE_FAILED
    assert manifest == read_manifest(workdir)
    assert not (workdir.build / "zh.tex").exists()
    assert not workdir.zh_pdf.exists()
    assert not outputs_present(workdir, "compile")


@pytest.fixture(autouse=True)
def cropped(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def crop_margins(pdf: Path) -> list[str]:
        calls.append(pdf)
        return []

    monkeypatch.setattr(cropping, "crop_margins", crop_margins)
    monkeypatch.setattr(cropping, "enabled", lambda: False)
    return calls


def test_ok_on_first_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cropped: list[Path]) -> None:
    workdir = make_workdir(tmp_path)
    calls = wire_latexmk(monkeypatch, [{}])
    work_calls = wire_work(monkeypatch)
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK
    assert manifest == read_manifest(workdir)
    assert manifest.report is not None and manifest.report.pages == 5
    assert manifest.baseline == BASELINE
    assert manifest.fix_session is None
    assert manifest.warnings == []
    assert calls == {"compile": 1, "clean": 1}
    assert work_calls == []
    assert outputs_present(workdir, "compile")
    assert sorted(path.name for path in workdir.out.iterdir()) == ["paper.zh.anchors.json", "paper.zh.pdf"]
    zh = (workdir.build / "zh.tex").read_text(encoding="utf-8")
    assert zh == translate(PAPER)
    assert workdir.zh_synctex.is_file()
    anchors = AnchorsFile.model_validate_json(workdir.zh_anchors.read_text(encoding="utf-8"))
    assert (anchors.pdf, anchors.tex) == ("paper.zh.pdf", "zh.tex")
    assert [(page.page, page.width, page.height) for page in anchors.pages] == [
        (number, PAGE_WIDTH, PAGE_HEIGHT) for number in range(1, PAGES + 1)
    ]
    assert anchors.pages[0].body == AnchorRect(page=1, x0=72.0, y0=72.0, x1=540.0, y1=720.0)
    assert all(page.body is None for page in anchors.pages[1:])
    blocks = {block.id: block for block in anchors.blocks}
    assert [(block.id, block.kind, block.parent, block.first_line, block.last_line) for block in anchors.blocks] == [
        ("PAR-0-0", AnchorKind.HEADING, None, 3, 3),
        ("PAR-1-0", AnchorKind.TEXT, None, 5, 5),
        ("BLK-1", AnchorKind.MATH, None, 7, 9),
        ("BLK-2", AnchorKind.FIGURE, None, 11, 14),
        ("CAP-0", AnchorKind.CAPTION, "BLK-2", 13, 13),
        ("PAR-4-0", AnchorKind.TEXT, None, 16, 16),
    ]
    for block_id, (_, baseline) in zip(("PAR-0-0", "PAR-1-0", "BLK-1", "CAP-0", "PAR-4-0"), TEXT_LINES, strict=True):
        assert blocks[block_id].rects == [line_rect(baseline)]
    assert blocks["BLK-2"].rects == blocks["CAP-0"].rects
    tree = workdir.build / "sandbox" / "tex"
    assert (tree / "main.tex").read_text(encoding="utf-8") == zh
    assert not (tree / "main.pdf").exists()
    assert sorted(path.name for path in tree.iterdir()) == ["figure.pdf", "main.tex"]
    assert not (workdir.logs / "compile-fix.jsonl").exists()
    assert cropped == []


def test_crop_margins_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cropped: list[Path]) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{}])
    wire_work(monkeypatch)
    monkeypatch.setattr(cropping, "enabled", lambda: True)
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK
    assert manifest.warnings == []
    assert cropped == [workdir.zh_pdf]


def test_crop_failure_becomes_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{}])
    wire_work(monkeypatch)
    monkeypatch.setattr(cropping, "enabled", lambda: True)
    monkeypatch.setattr(cropping, "crop_margins", lambda pdf: ["Ghostscript reported no page content"])
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK
    assert manifest.warnings == ["Ghostscript reported no page content"]
    assert workdir.zh_pdf.is_file()


def test_missing_synctex_becomes_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{"synctex": False}])
    wire_work(monkeypatch)
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK
    assert len(manifest.warnings) == 1
    assert manifest.warnings[0].startswith("cannot build paper.zh.anchors.json (")
    assert workdir.zh_pdf.is_file()
    assert not workdir.zh_anchors.exists()
    assert not workdir.zh_synctex.exists()
    assert outputs_present(workdir, "compile")


def test_unreadable_synctex_becomes_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{"synctex": False}])
    wire_work(monkeypatch)
    original = processes.run_in_process_group

    def run(command: list[str], cwd: Path, timeout: float, **kwargs: object) -> ProcessOutcome:
        outcome = original(command, cwd, timeout, **kwargs)
        if "-C" not in command:
            (cwd / "main.synctex.gz").write_bytes(b"not gzip")
        return outcome

    monkeypatch.setattr(processes, "run_in_process_group", run)
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK
    assert len(manifest.warnings) == 1
    assert manifest.warnings[0].startswith("cannot build paper.zh.anchors.json (")
    assert not workdir.zh_anchors.exists()


def test_caption_paragraph_break_is_not_a_control_sequence_difference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper = PAPER.replace("\\caption{A figure caption.}", "\\caption{First line.\n\nSecond line.}")
    workdir = make_workdir(tmp_path, paper)
    wire_latexmk(monkeypatch, [{}])
    wire_work(monkeypatch)
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK
    assert "\\par 第二行" in (workdir.build / "zh.tex").read_text(encoding="utf-8")


def test_manifest_fields_match_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{}])
    compile.run(workdir)
    keys = set(json.loads(workdir.manifest_path("compile").read_text(encoding="utf-8")))
    assert keys == {"status", "report", "baseline", "fix_session", "warnings", "message"}


def test_missing_precompile_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    workdir.manifest_path("precompile").unlink()
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert manifest.baseline is None
    assert "precompile.json" in manifest.message


def test_precompile_manifest_without_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    write_manifest(workdir.manifest_path("precompile"), PrecompileManifest(status=PrecompileStatus.COMPILE_FAILED))
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert "report" in manifest.message


def test_missing_compile_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    shutil.rmtree(workdir.build / "sandbox" / "tex")
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert manifest.baseline == BASELINE
    assert "--from precompile" in manifest.message


def test_empty_reviewed_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    for path in (workdir.build / "reviewed").glob("*.tex"):
        path.unlink()
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert "reviewed" in manifest.message


@pytest.mark.parametrize("relative", ["brief.json", "blocks.json", "masked.tex", "chunks/c001.tex"])
def test_missing_input_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str) -> None:
    workdir = make_workdir(tmp_path)
    (workdir.build / relative).unlink()
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert Path(relative).name in manifest.message


def test_reviewed_chunk_listed_in_brief_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    (workdir.build / "reviewed" / "c001.tex").unlink()
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert "c001.tex" in manifest.message


def test_unreadable_brief(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    (workdir.build / "brief.json").write_text("{not json", encoding="utf-8")
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert "ValidationError" in manifest.message


def test_chunks_do_not_add_up_to_masked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    (workdir.build / "masked.tex").write_text("something else", encoding="utf-8")
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert "masked.tex" in manifest.message
    assert "第一" not in (workdir.build / "sandbox" / "tex" / "main.tex").read_text(encoding="utf-8")


def test_unmask_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    path = workdir.build / "reviewed" / "c000.tex"
    path.write_text(path.read_text(encoding="utf-8").replace("⟦BLK-0⟧", "⟦BLK-0⟧⟦BLK-0⟧"), encoding="utf-8")
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert manifest.message.startswith("unmask failed")


def test_latexmk_not_runnable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)

    def run(command: list[str], cwd: Path, timeout: float, **kwargs: object) -> ProcessOutcome:
        raise FileNotFoundError("latexmk")

    monkeypatch.setattr(processes, "run_in_process_group", run)
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert "latexmk" in manifest.message


def test_first_compile_timeout_skips_the_fix_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{"timeout": True}])
    work_calls = wire_work(monkeypatch)
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert manifest.fix_session is None
    assert work_calls == []
    assert "timeout" in manifest.message


def test_fix_session_then_verify_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    calls = wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])

    def edit(tree: Path) -> None:
        path = tree / "main.tex"
        path.write_text(path.read_text(encoding="utf-8").replace("第二段。", "第二段。%"), encoding="utf-8")

    work_calls = wire_work(monkeypatch, edit=edit)
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK
    assert manifest == read_manifest(workdir)
    assert manifest.fix_session is not None
    assert manifest.fix_session.stop_reason == "finished"
    assert manifest.fix_session.model == "claude_code/claude-sonnet-5"
    assert manifest.report is not None and manifest.baseline == BASELINE
    assert calls == {"compile": 2, "clean": 2}
    assert work_calls[0]["role"] == "compile_fix"
    assert work_calls[0]["workdir"] == workdir.build / "sandbox" / "tex"
    assert work_calls[0]["trace_path"] == workdir.logs / "compile-fix.jsonl"
    assert "第二段。%" in (workdir.build / "zh.tex").read_text(encoding="utf-8")
    assert outputs_present(workdir, "compile")


def test_fix_session_then_verify_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    calls = wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}])
    wire_work(monkeypatch)
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert manifest.fix_session is not None
    assert manifest.report is None
    assert manifest.message.startswith("after the fix session the verify compile still fails the exit checks:")
    assert "! Undefined control sequence." in manifest.message
    assert calls == {"compile": 2, "clean": 1}
    assert (workdir.logs / "compile-fix.jsonl").is_file()


@pytest.mark.parametrize("stop_reason", [StopReason.ERROR, StopReason.TIMEOUT])
def test_fix_session_error_or_timeout_still_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_reason: StopReason
) -> None:
    workdir = make_workdir(tmp_path)
    calls = wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])
    wire_work(monkeypatch, stop_reason=stop_reason)
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK
    assert manifest.fix_session is not None
    assert manifest.fix_session.stop_reason == str(stop_reason)
    assert any(str(stop_reason) in line for line in manifest.warnings)
    assert calls == {"compile": 2, "clean": 2}


def test_control_sequences_must_match_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])

    def edit(tree: Path) -> None:
        path = tree / "main.tex"
        path.write_text(path.read_text(encoding="utf-8").replace("\\emph{强调}", "强调"), encoding="utf-8")

    wire_work(monkeypatch, edit=edit)
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert manifest.report is not None and manifest.report.pages == 5
    assert manifest.fix_session is not None
    assert "\\emph appears 1 times in source, 0 in translation" in manifest.message


@pytest.mark.parametrize("pages", [3, 7])
def test_page_ratio_outside_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pages: int) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{"log": f"Output written on zh.xdv ({pages} pages, 1 bytes).\n"}])
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert manifest.report is not None and manifest.report.pages == pages
    assert manifest.baseline == BASELINE
    assert f"[{compile.PAGE_RATIO_MIN}, {compile.PAGE_RATIO_MAX}]" in manifest.message


@pytest.mark.parametrize("pages", [4, 6])
def test_page_ratio_inside_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pages: int) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{"log": f"Output written on zh.xdv ({pages} pages, 1 bytes).\n"}])
    assert compile.run(workdir).status is CompileStatus.OK


def test_count_increases_become_warnings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{"log": LOG_NOISY}])
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK
    assert len(manifest.warnings) == 4
    for name in ("overfull_hboxes", "undefined_references", "undefined_citations", "missing_characters"):
        assert any(line.startswith(f"{name} rose from 0") for line in manifest.warnings)


def test_caption_fallback_becomes_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{}])
    path = workdir.build / "reviewed" / "c000.tex"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines(keepends=True) if "⟦CAP-" not in line]
    path.write_text("".join(lines), encoding="utf-8")
    manifest = compile.run(workdir)
    assert manifest.status is CompileStatus.OK
    assert len(manifest.warnings) == 1
    assert "CAP-0" in manifest.warnings[0]
    assert "A figure caption." in (workdir.build / "zh.tex").read_text(encoding="utf-8")


def test_rerun_clears_previous_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    workdir.manifest_path("precompile").unlink()
    (workdir.build / "zh.tex").write_text("stale", encoding="utf-8")
    workdir.zh_pdf.write_bytes(b"stale")
    workdir.zh_anchors.write_text("stale", encoding="utf-8")
    workdir.zh_synctex.write_bytes(b"stale")
    (workdir.logs / "compile-fix.jsonl").write_text("stale", encoding="utf-8")
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert not workdir.zh_anchors.exists()
    assert not workdir.zh_synctex.exists()
    assert (workdir.build / "sandbox" / "tex" / "main.tex").is_file()
    assert not (workdir.logs / "compile-fix.jsonl").exists()


def test_report_traces_the_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])
    wire_work(monkeypatch)
    actions: list[tuple[str, str]] = []
    compile.run(workdir, report=lambda status, summary: actions.append((status, summary)))
    assert actions == [
        ("compiling", "main.tex"),
        ("fix session", "running"),
        ("fix session", "Bash: ls"),
        ("verifying", "main.tex"),
    ]
