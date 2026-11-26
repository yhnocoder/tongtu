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
from tongtu.artifacts.common import StopReason, Usage
from tongtu.model.config import DEFAULT_TIMEOUT_SECONDS
from tongtu.model.work import PROMPT, result_cost, session_usage, skill_dir, work
from tongtu.processes import OUTPUT_EXCERPT_CHARS, ProcessOutcome

work_module = importlib.import_module("tongtu.model.work")

AGENTRUN = "/fake/bin/agentrun"

real_which = shutil.which

SKILLS = ("smoke", "translate")

END_USAGE = {
    "input_tokens": 4,
    "output_tokens": 95,
    "cache_read_tokens": 16648,
    "cache_write_tokens": 16778,
    "by_model": {"m1": {}},
}


def config_text(fast: str = 'model = "codex/m1"\neffort = "high"\ntimeout_seconds = 60', smart: str = "") -> str:
    smart = smart or 'model = "claude-code/m2"\neffort = "low"\ntimeout_seconds = 60.5'
    return f"[modes.fast]\n{fast}\n\n[modes.smart]\n{smart}\n"


def end_line(status: str, detail: str = "", usage: dict | None = None) -> bytes:
    event: dict = {"schema": 1, "type": "end", "status": status, "detail": detail}
    if usage is not None:
        event["usage"] = usage
    return json.dumps(event).encode("utf-8") + b"\n"


def tool_line(summary: str) -> bytes:
    return json.dumps({"schema": 1, "type": "tool", "name": "Bash", "summary": summary}).encode("utf-8") + b"\n"


def usage_line(parent: str | None) -> bytes:
    event = {"schema": 1, "type": "usage", "parent": parent, "input_tokens": 2, "output_tokens": 16}
    return json.dumps(event).encode("utf-8") + b"\n"


def write_config(tmp_path: Path, text: str) -> None:
    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    write_config(tmp_path, config_text())
    skill_root = tmp_path / "skill"
    for skill in SKILLS:
        (skill_root / skill).mkdir(parents=True, exist_ok=True)
        (skill_root / skill / "SKILL.md").write_text(f"{skill} 的做法", encoding="utf-8")
    monkeypatch.setattr(work_module, "SKILL_ROOT", skill_root)
    monkeypatch.setattr(shutil, "which", lambda name: AGENTRUN if name == "agentrun" else None)
    (tmp_path / "paper").mkdir()
    return tmp_path


def record_run(
    monkeypatch: pytest.MonkeyPatch,
    recorded: dict,
    outcome: ProcessOutcome | Exception,
    lines: tuple[bytes, ...] = (end_line("finished"),),
    raw: bytes | None = None,
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
        if raw is not None:
            Path(command[command.index("--raw") + 1]).write_bytes(raw)
        for line in lines:
            on_stdout_line(line)
        return outcome

    monkeypatch.setattr(work_module, "run_in_process_group", fake_run)


def exited(returncode: int = 0, stderr: bytes = b"") -> ProcessOutcome:
    return ProcessOutcome(returncode=returncode, stdout=b"", stderr=stderr, timed_out=False, duration_seconds=1.0)


def option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def run_work(configured: Path, mode: str = "fast", **kwargs: object):
    return work("smoke", configured / "paper", mode=mode, log_path=configured / "logs" / "smoke.jsonl", **kwargs)


def test_session_copies_the_skill_and_runs_agentrun(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    workdir = configured / "paper"
    log_path = configured / "logs" / "smoke.jsonl"
    outcome = work("smoke", workdir, mode="fast", log_path=log_path)
    assert outcome.session.stop_reason is StopReason.FINISHED
    assert outcome.detail == ""
    assert outcome.session.model == "codex/m1"
    assert outcome.session.effort == "high"
    assert (workdir / ".skills" / "smoke" / "SKILL.md").read_text(encoding="utf-8") == "smoke 的做法"
    command = recorded["command"]
    assert command[:2] == [AGENTRUN, "codex"]
    assert option(command, "--cwd") == str(workdir)
    assert option(command, "--format") == "jsonl"
    assert option(command, "--timeout") == "60"
    assert option(command, "--raw") == str(configured / "logs" / "smoke.raw.jsonl")
    assert option(command, "--model") == "m1"
    assert option(command, "--effort") == "high"
    assert recorded["cwd"] == workdir
    assert "--no-subagents" in command
    assert recorded["timeout_seconds"] == 60 + work_module.AGENTRUN_GRACE_SECONDS
    assert recorded["input_bytes"].decode("utf-8") == PROMPT.format(skill_path=".skills/smoke")
    assert log_path.read_bytes() == end_line("finished")


def test_the_mode_picks_the_runtime_model_effort_and_timeout(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    outcome = run_work(configured, "smart")
    assert outcome.session.model == "claude-code/m2"
    assert outcome.session.effort == "low"
    command = recorded["command"]
    assert command[:2] == [AGENTRUN, "claude-code"]
    assert option(command, "--model") == "m2"
    assert option(command, "--effort") == "low"
    assert option(command, "--timeout") == "61"


def test_extra_prompt_follows_the_fixed_prompt(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    run_work(configured, extra_prompt="主文件是 main.tex。")
    assert recorded["input_bytes"].decode("utf-8") == PROMPT.format(skill_path=".skills/smoke") + "主文件是 main.tex。"


def test_provider_model_of_pi_is_passed_whole(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(configured, config_text(fast='model = "pi/p/m1"'))
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    run_work(configured)
    assert recorded["command"][:2] == [AGENTRUN, "pi"]
    assert option(recorded["command"], "--model") == "p/m1"


def test_bare_runtime_passes_no_model_or_effort(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(configured, config_text(fast='model = "pi"'))
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    outcome = run_work(configured)
    assert outcome.session.model == "pi"
    assert outcome.session.effort is None
    assert "--model" not in recorded["command"]
    assert "--effort" not in recorded["command"]


def test_mode_without_timeout_uses_the_default(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(configured, config_text(fast='model = "codex/m1"'))
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    run_work(configured)
    assert option(recorded["command"], "--timeout") == str(DEFAULT_TIMEOUT_SECONDS)


def test_session_path_starts_with_the_base_python(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    run_work(configured)
    assert recorded["python_bin_entries"] == ["python3"]
    assert recorded["python3_target"] == (Path(sys.base_prefix) / "bin" / "python3").resolve()


def test_session_environment_disables_tongtu_and_carries_fonts(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, exited())
    monkeypatch.setenv("KEEP_ME", "1")
    run_work(configured)
    env = recorded["env"]
    assert env["TONGTU_DISABLE"] == "1"
    assert env["KEEP_ME"] == "1"
    assert env["TTFONTS"].startswith(f"{fonts.FONTS_DIR}//")


def test_skill_copy_keeps_files_already_in_the_destination(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record_run(monkeypatch, {}, exited())
    staged = configured / "paper" / skill_dir("smoke") / "validate.py"
    staged.parent.mkdir(parents=True)
    staged.write_text("print('ok')", encoding="utf-8")
    run_work(configured)
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
    outcome = run_work(configured)
    assert outcome.session.stop_reason is stop_reason
    assert outcome.detail == ""


@pytest.mark.parametrize(("status", "returncode"), [("failed", 1), ("rejected", 2), ("interrupted", 130)])
def test_other_end_statuses_are_errors_with_the_detail(
    configured: Path, monkeypatch: pytest.MonkeyPatch, status: str, returncode: int
) -> None:
    record_run(monkeypatch, {}, exited(returncode), (end_line(status, "why it stopped"),))
    outcome = run_work(configured)
    assert outcome.session.stop_reason is StopReason.ERROR
    assert outcome.detail == f"agentrun {status}: why it stopped"


def test_missing_end_event_is_error_with_stderr(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = b"x" * OUTPUT_EXCERPT_CHARS + b"tail"
    record_run(monkeypatch, {}, exited(101, stderr), (b"not json\n",))
    outcome = run_work(configured)
    assert outcome.session.stop_reason is StopReason.ERROR
    assert outcome.session.usage is None
    assert "exited with code 101 without an end event" in outcome.detail
    assert outcome.detail.endswith("tail")
    assert "x" * (OUTPUT_EXCERPT_CHARS - 4) in outcome.detail


def test_report_receives_tool_summaries(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = (tool_line("Bash: latexmk -xelatex flat.tex"), b"[1, 2]\n", tool_line("Read: a.tex"), end_line("finished"))
    record_run(monkeypatch, {}, exited(), lines)
    actions: list[str] = []
    run_work(configured, report=actions.append)
    assert actions == ["Bash: latexmk -xelatex flat.tex", "Read: a.tex"]


def test_agentrun_not_on_path_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    outcome = run_work(configured)
    assert outcome.session.stop_reason is StopReason.ERROR
    assert outcome.session.model == "codex/m1"
    assert "agentrun is not in PATH" in outcome.detail


def test_process_start_failure_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record_run(monkeypatch, {}, FileNotFoundError(2, "No such file or directory"))
    outcome = run_work(configured)
    assert outcome.session.stop_reason is StopReason.ERROR
    assert "or launch" in outcome.detail


def test_broken_config_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[modes\n", encoding="utf-8")
    outcome = work("smoke", tmp_path, mode="fast", log_path=tmp_path / "smoke.jsonl")
    assert outcome.session.stop_reason is StopReason.ERROR
    assert outcome.session.model == ""
    assert outcome.session.usage is None
    assert "not valid TOML" in outcome.detail


def test_unknown_mode_is_error(configured: Path) -> None:
    outcome = run_work(configured, "turbo")
    assert outcome.session.stop_reason is StopReason.ERROR
    assert outcome.session.model == ""
    assert "turbo" in outcome.detail


def test_missing_skill_directory_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(work_module, "SKILL_ROOT", configured / "nowhere")
    outcome = run_work(configured)
    assert outcome.session.stop_reason is StopReason.ERROR
    assert "skill directory" in outcome.detail


def test_skill_dir_names_the_skill() -> None:
    assert skill_dir("translate") == ".skills/translate"


def test_log_holds_every_agentrun_line(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = (b'{"type":"start"}\n', tool_line("Read: a.tex"), end_line("finished"))
    record_run(monkeypatch, {}, exited(), lines)
    run_work(configured)
    assert (configured / "logs" / "smoke.jsonl").read_bytes() == b"".join(lines)


def test_agentrun_killed_after_the_grace_period_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    killed = ProcessOutcome(returncode=-9, stdout=b"", stderr=b"", timed_out=True, duration_seconds=180.0)
    record_run(monkeypatch, {}, killed, ())
    outcome = run_work(configured)
    assert outcome.session.stop_reason is StopReason.ERROR
    assert "did not stop within" in outcome.detail


def test_usage_counts_top_level_requests_and_takes_the_end_tokens(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = (usage_line(None), usage_line("toolu_1"), usage_line(None), end_line("finished", usage=END_USAGE))
    record_run(monkeypatch, {}, exited(), lines)
    outcome = run_work(configured)
    assert outcome.session.usage == Usage(
        turns=2,
        input_tokens=4,
        output_tokens=95,
        cache_read_tokens=16648,
        cache_write_tokens=16778,
        cost_usd=None,
    )


def test_usage_of_claude_code_takes_the_cost_of_the_last_result(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b'{"type":"result","total_cost_usd":0.5}\n{"type":"assistant"}\n{"type":"result","total_cost_usd":0.00357}\n'
    record_run(monkeypatch, {}, exited(), (usage_line(None), end_line("finished", usage=END_USAGE)), raw=raw)
    outcome = run_work(configured, "smart")
    assert outcome.session.usage is not None
    assert outcome.session.usage.cost_usd == 0.00357
    assert outcome.session.usage.turns == 1


def test_usage_of_claude_code_without_a_result_line_has_no_cost(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_run(monkeypatch, {}, exited(), (end_line("finished", usage=END_USAGE),), raw=b'{"type":"assistant"}\n')
    outcome = run_work(configured, "smart")
    assert outcome.session.usage is not None
    assert outcome.session.usage.cost_usd is None


def test_usage_of_another_runtime_ignores_a_result_line(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b'{"type":"result","total_cost_usd":0.5}\n'
    record_run(monkeypatch, {}, exited(), (end_line("finished", usage=END_USAGE),), raw=raw)
    outcome = run_work(configured, "fast")
    assert outcome.session.usage is not None
    assert outcome.session.usage.cost_usd is None


def test_an_end_event_without_usage_gives_no_usage(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record_run(monkeypatch, {}, exited(), (usage_line(None), end_line("finished")))
    assert run_work(configured).session.usage is None


@pytest.mark.parametrize("reported", [None, "4 tokens", {"input_tokens": 4}, {**END_USAGE, "output_tokens": "x"}])
def test_session_usage_needs_every_token_count(reported: object) -> None:
    end = {"type": "end", "status": "finished"} if reported is None else {"type": "end", "usage": reported}
    assert session_usage(end, 3, None) is None


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ((), None),
        ((b"not json", b'{"type":"assistant"}'), None),
        ((b'{"type":"result","total_cost_usd":1}',), 1.0),
        ((b'{"type":"result","total_cost_usd":"1"}',), None),
        ((b'{"type":"result","total_cost_usd":true}',), None),
        ((b'{"type":"result","total_cost_usd":2.5}', b'{"type":"result"}'), None),
    ],
)
def test_result_cost_reads_the_last_result_line(lines: tuple[bytes, ...], expected: float | None) -> None:
    assert result_cost(lines) == expected


@pytest.mark.parametrize("model", ["codex/m1", "claude-code/m1", "pi/p/m1", "pi"])
def test_the_command_passes_agentrun_dry_run(configured: Path, monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    agentrun = real_which("agentrun")
    if agentrun is None:
        pytest.skip("PATH 里没有 agentrun")
    runtime = {"codex": "codex", "claude-code": "claude"}.get(model.split("/")[0], "pi")
    if real_which(runtime) is None:
        pytest.skip(f"PATH 里没有 {runtime}")
    write_config(configured, config_text(fast=f'model = "{model}"\neffort = "low"'))
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
    run_work(configured)
    assert checked["returncode"] == 0, checked["stderr"]
