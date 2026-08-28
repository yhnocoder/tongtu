from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import tongtu.model
from tongtu import compiling, processes
from tongtu.artifacts.common import FixSession
from tongtu.model.work import StopReason, WorkOutcome
from tongtu.processes import ProcessOutcome

LOG_OK = "Output written on zh.xdv (7 pages, 12345 bytes).\n"

LOG_ERROR = "! Undefined control sequence.\nl.42 \\pdfoutput\nOutput written on zh.xdv (0 pages, 8 bytes).\n"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def wire_latexmk(monkeypatch: pytest.MonkeyPatch, specs: list[dict]) -> list[list[str]]:
    commands: list[list[str]] = []
    compile_calls = {"n": 0}

    def run(command: list[str], cwd: Path, timeout: float, **kwargs: object) -> ProcessOutcome:
        commands.append(command)
        main = Path(command[-1])
        spec = specs[min(compile_calls["n"], len(specs) - 1)]
        if "-C" in command:
            if spec.get("clean_error"):
                raise OSError("latexmk vanished")
            return ProcessOutcome(
                returncode=spec.get("clean_returncode", 0),
                stdout=b"",
                stderr=b"",
                timed_out=spec.get("clean_timeout", False),
                duration_seconds=0.1,
            )
        compile_calls["n"] += 1
        if spec.get("error"):
            raise OSError("latexmk missing")
        if spec.get("timeout"):
            return ProcessOutcome(returncode=-9, stdout=b"", stderr=b"", timed_out=True, duration_seconds=600.0)
        if spec.get("pdf", True):
            (cwd / main.with_suffix(".pdf")).write_bytes(b"%PDF-1.5 fake body")
        (cwd / main.with_suffix(".log")).write_text(spec.get("log", LOG_OK), encoding="utf-8")
        return ProcessOutcome(
            returncode=spec.get("returncode", 0), stdout=b"", stderr=b"", timed_out=False, duration_seconds=2.5
        )

    monkeypatch.setattr(processes, "run_in_process_group", run)
    return commands


def test_attempt_compile_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = wire_latexmk(monkeypatch, [{}])
    attempt = compiling.attempt_compile(tmp_path, "zh.tex")
    assert attempt.passed
    assert attempt.report.pages == 7
    assert attempt.report.pdf_bytes > 0
    assert attempt.report.duration_seconds == 2.5
    assert attempt.log_path == tmp_path / "zh.log"
    assert attempt.pdf_name == "zh.pdf"
    assert commands == [["latexmk", "-xelatex", "-interaction=nonstopmode", "zh.tex"]]


def test_attempt_compile_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}])
    attempt = compiling.attempt_compile(tmp_path, "zh.tex")
    assert not attempt.passed
    message = compiling.failure_message(attempt)
    assert "exited with code 1" in message
    assert "! Undefined control sequence." in message


def test_attempt_compile_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_latexmk(monkeypatch, [{"timeout": True}])
    attempt = compiling.attempt_compile(tmp_path, "flat.tex")
    assert attempt.outcome.timed_out
    assert not attempt.passed
    assert str(compiling.COMPILE_TIMEOUT_SECONDS) in compiling.timeout_message(attempt)


def test_attempt_compile_without_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_latexmk(monkeypatch, [{"pdf": False}])
    attempt = compiling.attempt_compile(tmp_path, "zh.tex")
    assert not attempt.passed
    assert "zh.pdf is missing or empty" in compiling.failure_message(attempt)


@pytest.mark.parametrize("spec", [{"clean_error": True}, {"clean_timeout": True}, {"clean_returncode": 3}])
def test_clean_tree_reports_each_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: dict) -> None:
    commands = wire_latexmk(monkeypatch, [spec])
    warnings = compiling.clean_tree(tmp_path, "zh.tex")
    assert len(warnings) == 1
    assert commands == [["latexmk", "-C", "zh.tex"]]


def test_clean_tree_without_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_latexmk(monkeypatch, [{}])
    assert compiling.clean_tree(tmp_path, "zh.tex") == []


def wire_work(monkeypatch: pytest.MonkeyPatch, stop_reason: StopReason, edit=None) -> list[dict]:
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
        calls.append(
            {
                "role": role,
                "workdir": workdir,
                "trace_path": trace_path,
                "model": model,
                "effort": effort,
                "report": report,
            }
        )
        if edit is not None:
            edit(workdir)
        return WorkOutcome(stop_reason=stop_reason, detail="runtime missing", model="rt/m1")

    monkeypatch.setattr(tongtu.model, "work", fake_work)
    return calls


def make_tree(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "src"
    tree = tmp_path / "tree"
    src.mkdir()
    (src / "macros.sty").write_text("\\newcommand{\\x}{1}\n", encoding="utf-8")
    (src / "zh.tex").write_text("stale main\n", encoding="utf-8")
    warnings = compiling.copy_src_tree(src, tree, "zh.tex")
    assert warnings and "zh.tex" in warnings[0]
    (tree / "zh.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
    return src, tree


def test_fix_passes_the_role_and_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src, tree = make_tree(tmp_path)
    calls = wire_work(monkeypatch, StopReason.FINISHED)
    warnings: list[str] = []
    session = compiling.fix("compile_fix", src, tree, tmp_path / "trace.jsonl", "zh.tex", warnings, "rt/m", "high")
    assert session.stop_reason == "finished"
    assert session.model == "rt/m1"
    assert session.duration_seconds >= 0
    assert warnings == []
    assert calls == [
        {
            "role": "compile_fix",
            "workdir": tree,
            "trace_path": tmp_path / "trace.jsonl",
            "model": "rt/m",
            "effort": "high",
            "report": None,
        }
    ]


def test_fix_forwards_the_report_callback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src, tree = make_tree(tmp_path)
    calls = wire_work(monkeypatch, StopReason.FINISHED)
    warnings: list[str] = []

    def report(action: str) -> None:
        return None

    compiling.fix("compile_fix", src, tree, tmp_path / "trace.jsonl", "zh.tex", warnings, None, None, report=report)
    assert calls[0]["report"] is report


@pytest.mark.parametrize("stop_reason", [StopReason.ERROR, StopReason.TIMEOUT])
def test_fix_warns_on_error_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_reason: StopReason
) -> None:
    src, tree = make_tree(tmp_path)
    wire_work(monkeypatch, stop_reason)
    warnings: list[str] = []
    session = compiling.fix("compile_fix", src, tree, tmp_path / "trace.jsonl", "zh.tex", warnings, None, None)
    assert session.stop_reason == str(stop_reason)
    assert session.model == "rt/m1"
    assert len(warnings) == 1
    assert str(stop_reason) in warnings[0]


def test_fix_reports_changes_outside_the_main_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src, tree = make_tree(tmp_path)

    def edit(workdir: Path) -> None:
        (workdir / "macros.sty").write_text("\\newcommand{\\x}{2}\n", encoding="utf-8")
        (workdir / "zh.tex").write_text("\\documentclass{article}\n% fixed\n", encoding="utf-8")

    wire_work(monkeypatch, StopReason.FINISHED, edit)
    warnings: list[str] = []
    compiling.fix("compile_fix", src, tree, tmp_path / "trace.jsonl", "zh.tex", warnings, None, None)
    assert len(warnings) == 1
    assert "macros.sty" in warnings[0]
    assert "zh.tex" not in warnings[0].split(":", 1)[1]


def test_copy_src_tree_without_collision(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.tex").write_text("x", encoding="utf-8")
    assert compiling.copy_src_tree(src, tmp_path / "tree", "zh.tex") == []
    assert (tmp_path / "tree" / "main.tex").is_file()


def call_compile_with_fix(
    tmp_path: Path, warnings: list[str], report: Callable[[str, str], None] | None = None
) -> tuple[compiling.CompileAttempt | None, FixSession | None, str]:
    src, tree = make_tree(tmp_path)
    return compiling.compile_with_fix(
        "compile_fix",
        src,
        tree,
        "zh.tex",
        tmp_path / "trace.jsonl",
        warnings,
        None,
        None,
        report or (lambda status, summary: None),
    )


def test_compile_with_fix_passes_without_a_fix_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = wire_latexmk(monkeypatch, [{}])
    warnings: list[str] = []
    events: list[tuple[str, str]] = []
    attempt, session, failure = call_compile_with_fix(
        tmp_path, warnings, lambda status, summary: events.append((status, summary))
    )
    assert failure == ""
    assert session is None
    assert attempt is not None and attempt.passed and attempt.report.pages == 7
    assert events == [("compiling", "zh.tex")]
    assert len(commands) == 1


def test_compile_with_fix_latexmk_not_runnable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_latexmk(monkeypatch, [{"error": True}])
    attempt, session, failure = call_compile_with_fix(tmp_path, [])
    assert attempt is None and session is None
    assert "failed to run latexmk" in failure
    assert "PATH" in failure


def test_compile_with_fix_first_timeout_skips_the_fix_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_latexmk(monkeypatch, [{"timeout": True}])
    calls = wire_work(monkeypatch, StopReason.FINISHED)
    attempt, session, failure = call_compile_with_fix(tmp_path, [])
    assert attempt is not None and session is None
    assert calls == []
    assert str(compiling.COMPILE_TIMEOUT_SECONDS) in failure


def test_compile_with_fix_fix_then_verify_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])
    calls = wire_work(monkeypatch, StopReason.FINISHED)
    warnings: list[str] = []
    events: list[tuple[str, str]] = []
    attempt, session, failure = call_compile_with_fix(
        tmp_path, warnings, lambda status, summary: events.append((status, summary))
    )
    assert failure == ""
    assert session is not None and session.stop_reason == "finished"
    assert attempt is not None and attempt.passed and attempt.report.pages == 7
    assert len(calls) == 1
    assert ["-C" in command for command in commands] == [False, True, False]
    assert events == [("compiling", "zh.tex"), ("fix session", "running"), ("verifying", "zh.tex")]


def test_compile_with_fix_verify_not_runnable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {"error": True}])
    wire_work(monkeypatch, StopReason.FINISHED)
    attempt, session, failure = call_compile_with_fix(tmp_path, [])
    assert attempt is None
    assert session is not None
    assert "verify compile" in failure


def test_compile_with_fix_verify_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {"timeout": True}])
    wire_work(monkeypatch, StopReason.FINISHED)
    attempt, session, failure = call_compile_with_fix(tmp_path, [])
    assert attempt is not None and session is not None
    assert str(compiling.COMPILE_TIMEOUT_SECONDS) in failure


def test_compile_with_fix_verify_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}])
    wire_work(monkeypatch, StopReason.FINISHED)
    warnings: list[str] = []
    attempt, session, failure = call_compile_with_fix(tmp_path, warnings)
    assert attempt is not None and not attempt.passed
    assert session is not None
    assert failure.startswith("after the fix session the verify compile still fails the exit checks:")
