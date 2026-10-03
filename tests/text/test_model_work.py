from __future__ import annotations

import importlib
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tongtu import fonts
from tongtu.model.work import PROMPT, StopReason, skill_path, work
from tongtu.processes import OUTPUT_EXCERPT_CHARS, ProcessOutcome

work_module = importlib.import_module("tongtu.model.work")

EXECUTABLES = {
    "codex": "/fake/bin/codex",
    "claude": "/fake/bin/claude",
    "pi": "/fake/npm/bin/pi",
    "node": "/fake/node/bin/node",
    "xelatex": "/tex/bin/xelatex",
}

TABLE = """
[provider.demo]
base_url = "https://demo.example"
api = "chat"

[roles]
smoke = { model = "codex/m1", effort = "high", timeout_seconds = 60 }
claude = { model = "claude-code/m1", effort = "low", max_turns = 4, timeout_seconds = 60 }
pi = { model = "pi/p/m1", effort = "low", timeout_seconds = 60 }
bare_pi = { model = "pi", timeout_seconds = 60 }
bare_codex = { model = "codex", timeout_seconds = 60 }
asker = { model = "demo/m1", effort = "low" }
halfway = { model = "claude-code/m1", effort = "low", timeout_seconds = 60 }
untimed = { model = "codex/m1", effort = "low" }
"""

ROLES = ("smoke", "claude", "pi", "bare_pi", "bare_codex", "halfway", "untimed")


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
    pi_script = tmp_path / "npm" / "bin" / "pi"
    pi_script.parent.mkdir(parents=True)
    pi_script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    monkeypatch.setitem(EXECUTABLES, "pi", str(pi_script))
    monkeypatch.setattr(shutil, "which", lambda name: EXECUTABLES.get(name))
    login = tmp_path / "codex"
    login.mkdir()
    (login / "auth.json").write_text('{"test":true}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(login))
    for variable in ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    (tmp_path / "paper").mkdir()
    return tmp_path


def record_run(
    monkeypatch: pytest.MonkeyPatch,
    recorded: dict,
    outcome: ProcessOutcome | Exception,
    lines: tuple[bytes, ...] = (b'{"type":"result"}\n',),
) -> None:
    def fake_run(
        command: list[str],
        cwd: Path,
        timeout_seconds: float,
        *,
        input_bytes: bytes,
        env: dict[str, str],
        on_stdout_line: Callable[[bytes], None],
    ) -> ProcessOutcome:
        python_bin = Path(env["PATH"].partition(":")[0])
        recorded.update(
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            input_bytes=input_bytes,
            env=env,
            python_bin_entries=[entry.name for entry in python_bin.iterdir()],
            python3_target=(python_bin / "python3").resolve(),
            codex_home_existed=Path(env["CODEX_HOME"]).is_dir() if "CODEX_HOME" in env else None,
        )
        if isinstance(outcome, Exception):
            raise outcome
        for line in lines:
            on_stdout_line(line)
        return outcome

    monkeypatch.setattr(work_module, "run_in_process_group", fake_run)


def finished() -> ProcessOutcome:
    return ProcessOutcome(returncode=0, stdout=b"", stderr=b"", timed_out=False, duration_seconds=1.0)


def test_finished_codex_session_copies_skill_and_builds_the_command(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    workdir = configured / "paper"
    trace_path = configured / "logs" / "smoke.jsonl"
    outcome = work("smoke", workdir, trace_path=trace_path)
    assert outcome.stop_reason == StopReason.FINISHED
    assert outcome.detail == ""
    assert outcome.model == "codex/m1"
    assert (workdir / ".codex" / "skills" / "smoke" / "SKILL.md").read_text(encoding="utf-8") == "smoke 的做法"
    command = recorded["command"]
    assert command[:3] == ["/fake/bin/codex", "exec", "--json"]
    assert command[command.index("-m") + 1] == "m1"
    assert 'model_reasoning_effort="high"' in command
    assert recorded["cwd"] == workdir
    assert recorded["timeout_seconds"] == 60
    assert recorded["input_bytes"].decode("utf-8") == PROMPT.format(skill_path=".codex/skills/smoke")
    assert recorded["input_bytes"].decode("utf-8").startswith("读 .codex/skills/smoke/SKILL.md")
    assert trace_path.read_bytes() == b'{"type":"result"}\n'


def test_bare_codex_passes_no_model(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    outcome = work("bare_codex", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    assert outcome.model == "codex"
    assert "-m" not in recorded["command"]
    assert not any("model_reasoning_effort" in part for part in recorded["command"])


def test_claude_session_builds_the_command(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    workdir = configured / "paper"
    outcome = work("claude", workdir, trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    assert outcome.model == "claude-code/m1"
    command = recorded["command"]
    assert command[:6] == ["/fake/bin/claude", "-p", "--model", "m1", "--effort", "low"]
    assert command[command.index("--max-turns") + 1] == "4"
    assert (workdir / ".claude" / "skills" / "claude" / "SKILL.md").is_file()
    assert recorded["input_bytes"].decode("utf-8") == PROMPT.format(skill_path=".claude/skills/claude")


def test_pi_session_puts_the_prompt_in_argv_and_node_on_path(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    workdir = configured / "paper"
    outcome = work("pi", workdir, trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    assert outcome.model == "pi/p/m1"
    command = recorded["command"]
    assert command[:4] == [EXECUTABLES["pi"], "-p", "--mode", "json"]
    assert command[command.index("--model") + 1] == "p/m1"
    assert command[command.index("--thinking") + 1] == "low"
    assert command[command.index("--skill") + 1] == str(workdir / ".pi" / "skills" / "pi")
    assert command[-2:] == ["--", PROMPT.format(skill_path=".pi/skills/pi")]
    assert recorded["input_bytes"] == b""
    assert recorded["env"]["PATH"].split(":")[1:] == [
        "/tex/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        "/fake/node/bin",
    ]
    assert (workdir / ".pi" / "skills" / "pi" / "SKILL.md").is_file()


def test_bare_pi_passes_no_model(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    outcome = work("bare_pi", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    assert outcome.model == "pi"
    assert "--model" not in recorded["command"]
    assert "--thinking" not in recorded["command"]


def assert_path_starts_with_the_base_interpreter(recorded: dict, rest: str) -> None:
    assert recorded["env"]["PATH"].partition(":")[2] == rest
    assert recorded["python_bin_entries"] == ["python3"]
    assert recorded["python3_target"] == (Path(sys.base_prefix) / "bin" / "python3").resolve()


def test_session_environment_is_narrowed(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert recorded["env"]["TONGTU_DISABLE"] == "1"
    assert_path_starts_with_the_base_interpreter(recorded, "/tex/bin:/usr/bin:/bin:/usr/sbin:/sbin")


def test_session_environment_carries_the_font_search_path(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TTFONTS", raising=False)
    monkeypatch.delenv("OPENTYPEFONTS", raising=False)
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    for variable in ("TTFONTS", "OPENTYPEFONTS"):
        assert recorded["env"][variable] == f"{fonts.FONTS_DIR}//:"


def test_session_environment_without_tex(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "xelatex" else EXECUTABLES.get(name))
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert_path_starts_with_the_base_interpreter(recorded, "/usr/bin:/bin:/usr/sbin:/sbin")


def test_codex_session_uses_a_temporary_home_with_the_login_link(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "must-not-inherit")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
    monkeypatch.setenv("OPENAI_BASE_URL", "must-not-inherit")
    (configured / "codex" / "config.toml").write_text('sandbox_mode="danger-full-access"', encoding="utf-8")
    seen: dict = {}

    def run_session(*args: object, **kwargs: object) -> ProcessOutcome:
        env = kwargs["env"]
        home = Path(env["CODEX_HOME"])
        seen["home"] = home
        assert home != configured / "codex"
        assert (home / "auth.json").resolve() == (configured / "codex" / "auth.json").resolve()
        assert not (home / "config.toml").exists()
        for variable in ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
            assert variable not in env
        assert kwargs["input_bytes"] == b"read task.md"
        scratch = Path(env["TMPDIR"])
        assert scratch.is_dir()
        assert scratch.parent == (configured / "paper").resolve()
        assert env["TMP"] == env["TEMP"] == env["TMPDIR"]
        return finished()

    monkeypatch.setattr(work_module, "run_in_process_group", run_session)
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl", prompt="read task.md")
    assert outcome.stop_reason is StopReason.FINISHED
    assert not seen["home"].exists()
    assert (configured / "codex" / "auth.json").read_text(encoding="utf-8") == '{"test":true}'


def test_codex_missing_login_does_not_start_session(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    monkeypatch.setenv("CODEX_HOME", str(configured / "missing-login"))
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason is StopReason.ERROR
    assert "codex login" in outcome.detail
    assert outcome.model == "codex/m1"
    assert recorded == {}
    assert not (configured / "trace.jsonl").exists()


def test_claude_session_drops_anthropic_variables(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-inherit")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    work("claude", configured / "paper", trace_path=configured / "trace.jsonl")
    assert "ANTHROPIC_API_KEY" not in recorded["env"]
    assert "CLAUDE_CODE_USE_BEDROCK" not in recorded["env"]
    assert recorded["env"]["CLAUDE_CODE_REMOTE"] == "true"
    assert recorded["codex_home_existed"] is True


def test_skill_copy_keeps_files_already_in_the_destination(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    workdir = configured / "paper"
    destination = workdir / ".codex" / "skills" / "smoke"
    destination.mkdir(parents=True)
    (destination / "validate.py").write_text("现场先放好的工具", encoding="utf-8")
    work("smoke", workdir, trace_path=configured / "trace.jsonl")
    assert (destination / "validate.py").read_text(encoding="utf-8") == "现场先放好的工具"
    assert (destination / "SKILL.md").is_file()


def test_timeout_is_reported(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(
        monkeypatch,
        recorded,
        ProcessOutcome(returncode=-9, stdout=b"", stderr=b"", timed_out=True, duration_seconds=60.0),
    )
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.TIMEOUT
    assert outcome.model == "codex/m1"


def test_non_zero_exit_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(
        monkeypatch,
        recorded,
        ProcessOutcome(
            returncode=3,
            stdout=b"",
            stderr=("[unrecognized_model] " * OUTPUT_EXCERPT_CHARS + "运行时报错").encode(),
            timed_out=False,
            duration_seconds=2.0,
        ),
    )
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "exited with code 3" in outcome.detail
    assert outcome.detail.endswith("运行时报错")


CODEX_FAILED = '{"type":"turn.failed","error":{"message":"codex 报错"}}\n'.encode()

CLAUDE_FAILED = '{"type":"result","subtype":"success","is_error":true,"result":"claude 报错"}\n'.encode()

PI_FAILED = (
    '{"type":"message_end","message":{"role":"assistant","stopReason":"error","errorMessage":"pi 报错"}}\n'.encode()
)


@pytest.mark.parametrize(
    ("role", "lines", "detail"),
    [
        ("smoke", (b'{"type":"turn.started"}\n', CODEX_FAILED), "codex 报错"),
        ("claude", (b'{"type":"system","subtype":"init"}\n', CLAUDE_FAILED), "claude 报错"),
        ("pi", (b'{"type":"agent_start"}\n', PI_FAILED, b'{"type":"agent_end"}\n'), "error: pi 报错"),
    ],
)
def test_a_failure_event_is_error_even_with_exit_zero(
    configured: Path, monkeypatch: pytest.MonkeyPatch, role: str, lines: tuple[bytes, ...], detail: str
) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished(), lines=lines)
    trace_path = configured / "trace.jsonl"
    outcome = work(role, configured / "paper", trace_path=trace_path)
    assert outcome.stop_reason == StopReason.ERROR
    assert outcome.detail == detail
    assert trace_path.read_bytes() == b"".join(lines)


def test_the_last_failure_event_wins(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    first = b'{"type":"turn.failed","error":{"message":"first"}}\n'
    second = b'{"type":"turn.failed","error":{"message":"second"}}\n'
    record_run(monkeypatch, recorded, finished(), lines=(first, second))
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert outcome.detail == "second"


def test_a_success_result_with_exit_zero_is_finished(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(
        monkeypatch,
        recorded,
        finished(),
        lines=(b'{"type":"result","subtype":"success","is_error":false,"result":"done"}\n',),
    )
    outcome = work("claude", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED


def test_runtime_not_on_path_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "PATH" in outcome.detail
    assert "codex" in outcome.detail
    assert outcome.model == "codex/m1"


def test_pi_without_node_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "node" else EXECUTABLES.get(name))
    outcome = work("pi", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "is a script for node, which is not in PATH" in outcome.detail
    assert "runtime pi cannot start" in outcome.detail


def test_pi_behind_a_shell_wrapper_still_gets_node_on_path(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pi_wrapper = configured / "local" / "bin" / "pi"
    pi_wrapper.parent.mkdir(parents=True)
    pi_wrapper.write_text('#!/bin/sh\nexec node "$HOME/.pi/app/cli.js" "$@"\n', encoding="utf-8")
    paths = EXECUTABLES | {"pi": str(pi_wrapper), "/bin/sh": "/bin/sh"}
    monkeypatch.setattr(shutil, "which", lambda name: paths.get(name))
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    outcome = work("pi", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    entries = recorded["env"]["PATH"].split(":")
    assert entries[-1] == "/fake/node/bin"
    assert str(pi_wrapper.parent) not in entries


def test_pi_behind_a_shell_wrapper_without_node_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pi_wrapper = configured / "local" / "bin" / "pi"
    pi_wrapper.parent.mkdir(parents=True)
    pi_wrapper.write_text('#!/bin/sh\nexec node "$HOME/.pi/app/cli.js" "$@"\n', encoding="utf-8")
    paths = {"pi": str(pi_wrapper), "/bin/sh": "/bin/sh", "xelatex": EXECUTABLES["xelatex"]}
    monkeypatch.setattr(shutil, "which", lambda name: paths.get(name))
    outcome = work("pi", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert outcome.detail == "runtime pi cannot start: node is not in PATH; runtime pi needs it."


def test_codex_installed_by_npm_gets_node_on_path(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_script = configured / "npm" / "bin" / "codex"
    codex_script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    monkeypatch.setitem(EXECUTABLES, "codex", str(codex_script))
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    entries = recorded["env"]["PATH"].split(":")
    assert entries[-1] == "/fake/node/bin"
    assert str(codex_script.parent) not in entries


def test_native_codex_adds_nothing_to_path(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert recorded["env"]["PATH"].split(":")[1:] == ["/tex/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def test_process_start_failure_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, FileNotFoundError("现场不存在"))
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "/fake/bin/codex" in outcome.detail


def test_missing_config_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    outcome = work("smoke", tmp_path, trace_path=tmp_path / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "tongtu setup" in outcome.detail
    assert outcome.model == ""


def test_unknown_role_is_error(configured: Path) -> None:
    outcome = work("nobody", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "nobody" in outcome.detail
    assert outcome.model == ""


def test_role_pointing_at_a_provider_is_error(configured: Path) -> None:
    outcome = work("asker", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "provider demo" in outcome.detail
    assert "runtime" in outcome.detail
    assert outcome.model == ""


def test_role_without_timeout_is_error(configured: Path) -> None:
    outcome = work("untimed", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "timeout_seconds" in outcome.detail
    assert "max_turns" not in outcome.detail


def test_claude_role_without_max_turns_is_error(configured: Path) -> None:
    outcome = work("halfway", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "max_turns" in outcome.detail
    assert "timeout_seconds" not in outcome.detail


def test_effort_argument_is_applied(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl", effort="low")
    assert outcome.stop_reason == StopReason.FINISHED
    assert 'model_reasoning_effort="low"' in recorded["command"]
    assert 'model_reasoning_effort="high"' not in recorded["command"]


def test_missing_skill_directory_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(work_module, "SKILL_ROOT", configured / "empty")
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "skill" in outcome.detail


ACTION_LINE = (
    b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}\n'
)


def test_report_receives_parsed_actions(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished(), lines=(ACTION_LINE, b'{"type":"result"}\n'))
    trace_path = configured / "trace.jsonl"
    actions: list[str] = []
    outcome = work("claude", configured / "paper", trace_path=trace_path, report=actions.append)
    assert outcome.stop_reason == StopReason.FINISHED
    assert actions == ["Bash: ls"]
    assert trace_path.read_bytes() == ACTION_LINE + b'{"type":"result"}\n'


def test_report_receives_pi_actions(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    line = b'{"type":"tool_execution_start","toolName":"bash","args":{"command":"ls"}}\n'
    record_run(monkeypatch, recorded, finished(), lines=(line, b"not json\n"))
    actions: list[str] = []
    outcome = work("pi", configured / "paper", trace_path=configured / "trace.jsonl", report=actions.append)
    assert outcome.stop_reason == StopReason.FINISHED
    assert actions == ["bash: ls"]


def test_without_report_the_trace_is_still_written(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished(), lines=(ACTION_LINE,))
    trace_path = configured / "trace.jsonl"
    outcome = work("claude", configured / "paper", trace_path=trace_path)
    assert outcome.stop_reason == StopReason.FINISHED
    assert trace_path.read_bytes() == ACTION_LINE


def test_skill_path_follows_the_runtime(configured: Path) -> None:
    assert skill_path("smoke") == (".codex/skills/smoke", "")
    assert skill_path("claude") == (".claude/skills/claude", "")
    assert skill_path("pi") == (".pi/skills/pi", "")


def test_skill_path_reports_a_provider_role(configured: Path) -> None:
    path, detail = skill_path("asker")
    assert path is None
    assert "provider demo" in detail


def test_skill_path_reports_an_unknown_role(configured: Path) -> None:
    path, detail = skill_path("nobody")
    assert path is None
    assert "nobody" in detail


def test_skill_path_reports_a_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    path, detail = skill_path("smoke")
    assert path is None
    assert "tongtu setup" in detail
