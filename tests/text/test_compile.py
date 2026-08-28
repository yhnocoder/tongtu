from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

import tongtu.model
from tongtu import processes
from tongtu.artifacts.common import CompileReport
from tongtu.artifacts.compile import CompileManifest, CompileStatus
from tongtu.artifacts.precompile import PrecompileManifest, PrecompileStatus
from tongtu.artifacts.survey import BriefFile, ChunkRecord, Part
from tongtu.manifests import write_manifest
from tongtu.model.work import StopReason, WorkOutcome
from tongtu.pipeline import outputs_present
from tongtu.processes import ProcessOutcome
from tongtu.stages import compile, mask
from tongtu.workdir import Workdir

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
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


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
    fonts = workdir.build / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "LXGWWenKai-Light.ttf").write_bytes(b"font")
    return workdir


def wire_latexmk(monkeypatch: pytest.MonkeyPatch, specs: list[dict]) -> dict[str, int]:
    calls = {"compile": 0, "clean": 0}

    def run(command: list[str], cwd: Path, timeout: float, **kwargs: object) -> ProcessOutcome:
        main = Path(command[-1])
        assert main.name == "zh.tex"
        if "-C" in command:
            calls["clean"] += 1
            (cwd / main.with_suffix(".pdf")).unlink(missing_ok=True)
            (cwd / main.with_suffix(".log")).unlink(missing_ok=True)
            return ProcessOutcome(returncode=0, stdout=b"", stderr=b"", timed_out=False, duration_seconds=0.1)
        spec = specs[min(calls["compile"], len(specs) - 1)]
        calls["compile"] += 1
        if spec.get("timeout"):
            return ProcessOutcome(returncode=-9, stdout=b"", stderr=b"", timed_out=True, duration_seconds=600.0)
        if spec.get("pdf", True):
            (cwd / main.with_suffix(".pdf")).write_bytes(b"%PDF-1.5 fake body")
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
        model: str | None = None,
        effort: str | None = None,
        report=None,
    ):
        calls.append({"role": role, "workdir": workdir, "trace_path": trace_path, "model": model, "effort": effort})
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
    assert not (workdir.out / "zh.pdf").exists()
    assert not outputs_present(workdir, "compile")


def test_ok_on_first_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert calls == {"compile": 1, "clean": 0}
    assert work_calls == []
    assert outputs_present(workdir, "compile")
    zh = (workdir.build / "zh.tex").read_text(encoding="utf-8")
    assert zh == translate(PAPER)
    assert (workdir.build / "sandbox" / "compile" / "figure.pdf").is_file()
    assert (workdir.build / "sandbox" / "compile" / "fonts" / "LXGWWenKai-Light.ttf").is_file()
    assert not (workdir.logs / "compile-fix.jsonl").exists()


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


def test_missing_precompile_fonts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    fonts = workdir.build / "fonts"
    (fonts / "LXGWWenKai-Light.ttf").unlink()
    fonts.rmdir()
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
    assert not (workdir.build / "sandbox" / "compile").exists()


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
        path = tree / "zh.tex"
        path.write_text(path.read_text(encoding="utf-8").replace("第二段。", "第二段。%"), encoding="utf-8")

    work_calls = wire_work(monkeypatch, edit=edit)
    manifest = compile.run(workdir, model_override="claude_code/claude-sonnet-5", effort="high")
    assert manifest.status is CompileStatus.OK
    assert manifest == read_manifest(workdir)
    assert manifest.fix_session is not None
    assert manifest.fix_session.stop_reason == "finished"
    assert manifest.fix_session.model == "claude_code/claude-sonnet-5"
    assert manifest.report is not None and manifest.baseline == BASELINE
    assert calls == {"compile": 2, "clean": 1}
    assert work_calls[0]["role"] == "compile_fix"
    assert work_calls[0]["workdir"] == workdir.build / "sandbox" / "compile"
    assert work_calls[0]["trace_path"] == workdir.logs / "compile-fix.jsonl"
    assert work_calls[0]["model"] == "claude_code/claude-sonnet-5"
    assert work_calls[0]["effort"] == "high"
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
    assert calls == {"compile": 2, "clean": 1}


def test_control_sequences_must_match_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])

    def edit(tree: Path) -> None:
        path = tree / "zh.tex"
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
    (workdir.build / "sandbox" / "compile").mkdir(parents=True)
    (workdir.build / "sandbox" / "compile" / "zh.pdf").write_bytes(b"stale")
    (workdir.build / "zh.tex").write_text("stale", encoding="utf-8")
    (workdir.out / "zh.pdf").write_bytes(b"stale")
    (workdir.logs / "compile-fix.jsonl").write_text("stale", encoding="utf-8")
    manifest = compile.run(workdir)
    assert_failed(workdir, manifest)
    assert not (workdir.build / "sandbox" / "compile").exists()
    assert not (workdir.logs / "compile-fix.jsonl").exists()


def test_report_traces_the_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])
    wire_work(monkeypatch)
    actions: list[tuple[str, str]] = []
    compile.run(workdir, report=lambda status, summary: actions.append((status, summary)))
    assert actions == [
        ("compiling", "zh.tex"),
        ("fix session", "running"),
        ("fix session", "Bash: ls"),
        ("verifying", "zh.tex"),
    ]
