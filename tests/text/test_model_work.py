from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tongtu import fonts
from tongtu.model.work import PROMPT, StopReason, skill_dir, work
from tongtu.processes import OUTPUT_EXCERPT_CHARS, ProcessOutcome

work_module = importlib.import_module("tongtu.model.work")

AGENTRUN = "/fake/bin/agentrun"

real_which = shutil.which

TABLE = """
[provider.demo]
base_url = "https://demo.example"
api = "chat"

[roles]
smoke = { model = "codex/m1", effort = "high", timeout_seconds = 60 }
claude = { model = "claude-code/m1", effort = "low", max_turns = 4, timeout_seconds = 60.5 }
pi = { model = "pi/p/m1", effort = "low", max_turns = 9, timeout_seconds = 60 }
bare_pi = { model = "pi", timeout_seconds = 60 }
asker = { model = "demo/m1", effort = "low" }
halfway = { model = "claude-code/m1", effort = "low", timeout_seconds = 60 }
untimed = { model = "codex/m1", effort = "low" }
"""

ROLES = ("smoke", "claude", "pi", "bare_pi", "halfway", "untimed")


def end_line(status: str, detail: str = "") -> bytes:
    return json.dumps({"schema": 1, "type": "end", "status": status, "detail": detail}).encode("utf-8") + b"\n"


def tool_line(summary: str) -> bytes:
    return json.dumps({"schema": 1, "type": "tool", "name": "Bash", "summary": summary}).encode("utf-8") + b"\n"


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TABLE, encoding="utf-8")
    skill_root = tmp_path / "skill"
    for role in ROLES:
        (skill_root / role).mkdir(parents=True, exist_ok=True)
        (skill_root / role / "SKILL.md").write_text(f"{role} 的做法", encoding="utf-8")
    monkeypatch.setattr(work_module, "SKILL_ROOT", skill_root)
    monkeypatch.setattr(shutil, "which", lambda name: AGENTRUN if name == "agentrun" else None)
    (tmp_path / "paper").mkdir()
    return tmp_path


def record_run(
    monkeypatch: pytest.MonkeyPatch,
    recorded: dict,
    outcome: ProcessOutcome | Exception,
    lines: tuple[bytes, ...] = (end_line("finished"),),
) -> None:
    def fake_run(
        command: list[str],
        cwd: Path,
        timeout_seconds: float | None,
        *,
        input_bytes: bytes,
        env: dict[str, str],
        on_stdout_line: Callable[[bytes], None],
    ) -> ProcessOutcome:
        python_bin = Path(command[command.index("--path") + 1])
        recorded.update(
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            input_bytes=input_bytes,
            env=env,
            python_bin_entries=[entry.name for entry in python_bin.iterdir()],
            python3_target=(python_bin / "python3").resolve(),
        )
        if isinstance(outcome, Exception):
            raise outcome
        for line in lines:
            on_stdout_line(line)
        return outcome

    monkeypatch.setattr(work_module, "run_in_process_group", fake_run)


def exited(returncode: int = 0, stderr: bytes = b"") -> ProcessOutcome:
    return ProcessOutcome(returncode=returncode, stdout=b"", stderr=stderr, timed_out=False, duration_seconds=1.0)


def option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def test_codex_session_copies_skill_and_runs_agentrun(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    workdir = configured / "paper"
    trace_path = configured / "logs" / "smoke.jsonl"
    outcome = work("smoke", workdir, trace_path=trace_path)
    assert outcome.stop_reason == StopReason.FINISHED
    assert outcome.detail == ""
    assert outcome.model == "codex/m1"
    assert (workdir / ".skills" / "smoke" / "SKILL.md").read_text(encoding="utf-8") == "smoke 的做法"
    command = recorded["command"]
    assert command[:2] == [AGENTRUN, "codex"]
    assert option(command, "--cwd") == str(workdir)
    assert option(command, "--format") == "jsonl"
    assert option(command, "--timeout") == "60"
    assert option(command, "--raw") == str(trace_path)
    assert option(command, "--model") == "m1"
    assert option(command, "--effort") == "high"
    assert "--max-turns" not in command
    assert recorded["cwd"] == workdir
    assert "--no-subagents" in command
    assert recorded["timeout_seconds"] == 60 + work_module.AGENTRUN_GRACE_SECONDS
    assert recorded["input_bytes"].decode("utf-8") == PROMPT.format(skill_path=".skills/smoke")
    assert (configured / "logs" / "smoke.events.jsonl").read_bytes() == end_line("finished")


def test_claude_session_passes_max_turns_and_rounds_the_timeout_up(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    outcome = work("claude", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    command = recorded["command"]
    assert command[:2] == [AGENTRUN, "claude-code"]
    assert option(command, "--max-turns") == "4"
    assert option(command, "--timeout") == "61"


def test_pi_session_passes_provider_model_and_no_max_turns(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    outcome = work("pi", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    command = recorded["command"]
    assert command[:2] == [AGENTRUN, "pi"]
    assert option(command, "--model") == "p/m1"
    assert "--max-turns" not in command


def test_bare_runtime_passes_no_model_or_effort(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    outcome = work("bare_pi", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    assert outcome.model == "pi"
    assert "--model" not in recorded["command"]
    assert "--effort" not in recorded["command"]


def test_session_path_starts_with_the_base_python(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert recorded["python_bin_entries"] == ["python3"]
    assert recorded["python3_target"] == (Path(sys.base_prefix) / "bin" / "python3").resolve()


def test_session_environment_disables_tongtu_and_carries_fonts(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    monkeypatch.setenv("KEEP_ME", "1")
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    env = recorded["env"]
    assert env["TONGTU_DISABLE"] == "1"
    assert env["KEEP_ME"] == "1"
    assert env["TTFONTS"].startswith(f"{fonts.FONTS_DIR}//")


def test_skill_copy_keeps_files_already_in_the_destination(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record_run(monkeypatch, {}, exited())
    workdir = configured / "paper"
    staged = workdir / skill_dir("smoke") / "validate.py"
    staged.parent.mkdir(parents=True)
    staged.write_text("print('ok')", encoding="utf-8")
    work("smoke", workdir, trace_path=configured / "trace.jsonl")
    assert staged.read_text(encoding="utf-8") == "print('ok')"
    assert (staged.parent / "SKILL.md").is_file()


@pytest.mark.parametrize(
    ("status", "stop_reason"),
    [("finished", StopReason.FINISHED), ("timeout", StopReason.TIMEOUT)],
)
def test_end_status_decides_the_stop_reason(
    configured: Path, monkeypatch: pytest.MonkeyPatch, status: str, stop_reason: StopReason
) -> None:
    record_run(monkeypatch, {}, exited(3 if status == "timeout" else 0), (end_line(status),))
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == stop_reason
    assert outcome.detail == ""


@pytest.mark.parametrize(("status", "returncode"), [("failed", 1), ("rejected", 2), ("interrupted", 130)])
def test_other_end_statuses_are_errors_with_the_detail(
    configured: Path, monkeypatch: pytest.MonkeyPatch, status: str, returncode: int
) -> None:
    record_run(monkeypatch, {}, exited(returncode), (end_line(status, "why it stopped"),))
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert outcome.detail == f"agentrun {status}: why it stopped"


def test_missing_end_event_is_error_with_stderr(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = b"x" * OUTPUT_EXCERPT_CHARS + b"tail"
    record_run(monkeypatch, {}, exited(101, stderr), (b"not json\n",))
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "exited with code 101 without an end event" in outcome.detail
    assert outcome.detail.endswith("tail")
    assert "x" * (OUTPUT_EXCERPT_CHARS - 4) in outcome.detail


def test_report_receives_tool_summaries(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = (tool_line("Bash: latexmk -xelatex flat.tex"), b"[1, 2]\n", tool_line("Read: a.tex"), end_line("finished"))
    record_run(monkeypatch, {}, exited(), lines)
    actions: list[str] = []
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl", report=actions.append)
    assert outcome.stop_reason == StopReason.FINISHED
    assert actions == ["Bash: latexmk -xelatex flat.tex", "Read: a.tex"]


def test_agentrun_not_on_path_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "agentrun is not in PATH" in outcome.detail


def test_process_start_failure_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record_run(monkeypatch, {}, FileNotFoundError(2, "No such file or directory"))
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "or launch" in outcome.detail


def test_missing_config_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "absent"))
    outcome = work("smoke", tmp_path, trace_path=tmp_path / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "tongtu setup" in outcome.detail


def test_unknown_role_is_error(configured: Path) -> None:
    outcome = work("nobody", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert outcome.model == ""


def test_role_pointing_at_a_provider_is_error(configured: Path) -> None:
    outcome = work("asker", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "work needs a runtime" in outcome.detail


def test_role_without_timeout_is_error(configured: Path) -> None:
    outcome = work("untimed", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "timeout_seconds" in outcome.detail


def test_claude_role_without_max_turns_is_error(configured: Path) -> None:
    outcome = work("halfway", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "max_turns" in outcome.detail


def test_effort_argument_is_applied(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl", effort="xhigh")
    assert option(recorded["command"], "--effort") == "xhigh"


def test_missing_skill_directory_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(work_module, "SKILL_ROOT", configured / "nowhere")
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "skill directory" in outcome.detail


def test_skill_dir_does_not_depend_on_the_runtime() -> None:
    assert skill_dir("review") == ".skills/review"


@pytest.mark.parametrize("role", ["smoke", "claude", "pi", "bare_pi"])
def test_the_command_passes_agentrun_dry_run(configured: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    agentrun = real_which("agentrun")
    if agentrun is None:
        pytest.skip("PATH 里没有 agentrun")
    runtime = {"smoke": "codex", "claude": "claude"}.get(role, "pi")
    if real_which(runtime) is None:
        pytest.skip(f"PATH 里没有 {runtime}")
    monkeypatch.setattr(shutil, "which", lambda name: agentrun if name == "agentrun" else real_which(name))
    codex_home = configured / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    checked: dict = {}

    def dry_run(command: list[str], cwd: Path, timeout_seconds: float | None, **kwargs: object) -> ProcessOutcome:
        completed = subprocess.run(
            [*command, "--dry-run", "--sandbox", "off"],
            cwd=cwd,
            input=kwargs["input_bytes"],
            env=kwargs["env"],
            capture_output=True,
        )
        checked.update(returncode=completed.returncode, stderr=completed.stderr.decode("utf-8", "replace"))
        return exited(completed.returncode)

    monkeypatch.setattr(work_module, "run_in_process_group", dry_run)
    work(role, configured / "paper", trace_path=configured / "trace.jsonl")
    assert checked["returncode"] == 0, checked["stderr"]


def test_events_file_holds_every_agentrun_line(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = (b'{"type":"start"}\n', tool_line("Read: a.tex"), end_line("finished"))
    record_run(monkeypatch, {}, exited(), lines)
    trace_path = configured / "logs" / "compile_fix.jsonl"
    work("smoke", configured / "paper", trace_path=trace_path)
    assert work_module.events_path(trace_path).read_bytes() == b"".join(lines)


def test_agentrun_killed_after_the_grace_period_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    killed = ProcessOutcome(returncode=-9, stdout=b"", stderr=b"", timed_out=True, duration_seconds=180.0)
    record_run(monkeypatch, {}, killed, ())
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "did not stop within" in outcome.detail
