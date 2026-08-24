from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from typing import IO

import pytest

from tongtu.model.work import StopReason, work
from tongtu.processes import ProcessOutcome

work_module = importlib.import_module("tongtu.model.work")

EXECUTABLES = {"runner": "/fake/bin/runner", "other-runner": "/fake/bin/other-runner", "xelatex": "/tex/bin/xelatex"}

TABLE = """
[runtime.demo]
skill_path = ".agent/skills/{role}"
command = ["runner", "--model", "{model}", "--effort", "{effort}", "--max-turns", "{max_turns}", "--allowedTools", "Read,Edit,{bash_allow}", "--settings", "{settings}"]
settings = { sandbox = { enabled = true, network = { allowedDomains = [] } } }

[runtime.other]
skill_path = ".other/{role}"
command = ["other-runner", "--model", "{model}", "--effort", "{effort}"]

[provider.gateway]
base_url = "https://gateway.example"
api_key_env = "GATEWAY_KEY"

[runtime.demo_gateway]
provider = "gateway"
skill_path = ".agent/skills/{role}"
command = ["runner", "--model", "{model}", "--base-url", "{base_url}"]
env = { API_BASE = "{base_url}", API_KEY = "{api_key}", MODEL = "{model}" }

[runtime.temp_gateway]
provider = "gateway"
skill_path = ".agent/skills/{role}"
command = ["runner", "--home", "{tmp_dir}"]
env = { CODEX_HOME = "{tmp_dir}", API_KEY = "{api_key}" }

[runtime.ghost_gateway]
provider = "nowhere"
skill_path = ".agent/skills/{role}"
command = ["runner", "--base-url", "{base_url}"]

[runtime.unbound_gateway]
skill_path = ".agent/skills/{role}"
command = ["runner", "--base-url", "{base_url}"]

[runtime.bare_settings]
skill_path = ".bare/{role}"
command = ["runner", "--settings", "{settings}"]

[roles]
smoke = { runtime = "demo", model = "m1", effort = "high", max_turns = 4, timeout_seconds = 60, bash = ["latexmk", "xelatex"] }
bare = { runtime = "demo", model = "m1", effort = "low", max_turns = 2, timeout_seconds = 30, bash = [] }
lost = { runtime = "nowhere", model = "m1", effort = "low", max_turns = 2, timeout_seconds = 30, bash = [] }
unsettled = { runtime = "bare_settings", model = "m1", effort = "low", max_turns = 2, timeout_seconds = 30, bash = [] }
gated = { runtime = "demo_gateway", model = "m1", effort = "low", max_turns = 2, timeout_seconds = 30, bash = [] }
homed = { runtime = "temp_gateway", model = "m1", effort = "low", max_turns = 2, timeout_seconds = 30, bash = [] }
ghosted = { runtime = "ghost_gateway", model = "m1", effort = "low", max_turns = 2, timeout_seconds = 30, bash = [] }
unbound = { runtime = "unbound_gateway", model = "m1", effort = "low", max_turns = 2, timeout_seconds = 30, bash = [] }
asker = { provider = "demo", model = "m1", effort = "low" }
halfway = { runtime = "demo", model = "m1", effort = "low" }
"""


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = tmp_path / "config" / "tongtu" / "models.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TABLE, encoding="utf-8")
    skill_root = tmp_path / "skill"
    for role in ("smoke", "bare", "gated", "homed"):
        (skill_root / role).mkdir(parents=True, exist_ok=True)
        (skill_root / role / "SKILL.md").write_text(f"{role} 的做法", encoding="utf-8")
    monkeypatch.setattr(work_module, "SKILL_ROOT", skill_root)
    monkeypatch.setattr(shutil, "which", lambda name: EXECUTABLES.get(name))
    monkeypatch.delenv("TONGTU_NESTED_SANDBOX", raising=False)
    (tmp_path / "paper").mkdir()
    return tmp_path


def record_run(monkeypatch: pytest.MonkeyPatch, recorded: dict, outcome: ProcessOutcome | Exception) -> None:
    def fake_run(
        command: list[str],
        cwd: Path,
        timeout_seconds: float,
        *,
        stdout: IO[bytes],
        input_bytes: bytes,
        env: dict[str, str],
    ) -> ProcessOutcome:
        recorded.update(command=command, cwd=cwd, timeout_seconds=timeout_seconds, input_bytes=input_bytes, env=env)
        if isinstance(outcome, Exception):
            raise outcome
        stdout.write(b'{"type":"result"}\n')
        return outcome

    monkeypatch.setattr(work_module, "run_in_process_group", fake_run)


def finished() -> ProcessOutcome:
    return ProcessOutcome(returncode=0, stderr=b"", timed_out=False, duration_seconds=1.0)


def test_finished_session_copies_skill_and_fills_command(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    workdir = configured / "paper"
    trace_path = configured / "logs" / "smoke.jsonl"
    outcome = work("smoke", workdir, trace_path=trace_path)
    assert outcome.stop_reason == StopReason.FINISHED
    assert outcome.detail == ""
    assert (workdir / ".agent" / "skills" / "smoke" / "SKILL.md").read_text(encoding="utf-8") == "smoke 的做法"
    assert recorded["command"] == [
        "/fake/bin/runner",
        "--model",
        "m1",
        "--effort",
        "high",
        "--max-turns",
        "4",
        "--allowedTools",
        "Read,Edit,Bash(latexmk:*),Bash(xelatex:*)",
        "--settings",
        '{"sandbox":{"enabled":true,"network":{"allowedDomains":[]}}}',
    ]
    assert recorded["cwd"] == workdir
    assert recorded["timeout_seconds"] == 60
    assert (
        recorded["input_bytes"].decode("utf-8")
        == "读 .agent/skills/smoke/SKILL.md，按它做；现场是当前目录这棵树，只在其中读写。"
    )
    assert trace_path.read_bytes() == b'{"type":"result"}\n'


def test_empty_bash_list_leaves_no_trailing_comma(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    work("bare", configured / "paper", trace_path=configured / "trace.jsonl")
    assert recorded["command"][recorded["command"].index("--allowedTools") + 1] == "Read,Edit"


def test_settings_are_filled_as_json(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    filled = recorded["command"][recorded["command"].index("--settings") + 1]
    assert json.loads(filled) == {"sandbox": {"enabled": True, "network": {"allowedDomains": []}}}


def test_nested_sandbox_variable_weakens_sandbox_settings(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    monkeypatch.setenv("TONGTU_NESTED_SANDBOX", "1")
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    filled = recorded["command"][recorded["command"].index("--settings") + 1]
    assert json.loads(filled) == {
        "sandbox": {"enabled": True, "network": {"allowedDomains": []}, "enableWeakerNestedSandbox": True}
    }


def test_session_environment_is_narrowed(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert recorded["env"]["TONGTU_DISABLE"] == "1"
    assert recorded["env"]["PATH"] == "/tex/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def test_session_environment_without_tex(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "xelatex" else EXECUTABLES.get(name))
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert recorded["env"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"


def test_runtime_without_settings_table_is_error(configured: Path) -> None:
    outcome = work("unsettled", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "settings" in outcome.detail


def test_stale_skill_directory_is_replaced(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    workdir = configured / "paper"
    stale = workdir / ".agent" / "skills" / "smoke"
    stale.mkdir(parents=True)
    (stale / "old.md").write_text("上一轮的文件", encoding="utf-8")
    work("smoke", workdir, trace_path=configured / "trace.jsonl")
    assert not (stale / "old.md").exists()
    assert (stale / "SKILL.md").is_file()


def test_timeout_is_reported(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(
        monkeypatch,
        recorded,
        ProcessOutcome(returncode=-9, stderr=b"", timed_out=True, duration_seconds=60.0),
    )
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.TIMEOUT


def test_non_zero_exit_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(
        monkeypatch,
        recorded,
        ProcessOutcome(returncode=3, stderr="运行时报错".encode(), timed_out=False, duration_seconds=2.0),
    )
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "退出码 3" in outcome.detail
    assert "运行时报错" in outcome.detail


def test_runtime_not_on_path_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "PATH" in outcome.detail
    assert "runner" in outcome.detail


def test_process_start_failure_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, FileNotFoundError("现场不存在"))
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "/fake/bin/runner" in outcome.detail


def test_missing_config_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    outcome = work("smoke", tmp_path, trace_path=tmp_path / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "tongtu setup" in outcome.detail


def test_unknown_role_is_error(configured: Path) -> None:
    outcome = work("nobody", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "nobody" in outcome.detail


def test_role_without_runtime_is_error(configured: Path) -> None:
    outcome = work("asker", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "runtime" in outcome.detail


def test_role_without_session_limits_is_error(configured: Path) -> None:
    outcome = work("halfway", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "max_turns" in outcome.detail


def test_model_and_effort_overrides_are_applied(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    workdir = configured / "paper"
    outcome = work(
        "smoke",
        workdir,
        trace_path=configured / "trace.jsonl",
        model="other/m9",
        effort="low",
    )
    assert outcome.stop_reason == StopReason.FINISHED
    assert recorded["command"] == ["/fake/bin/other-runner", "--model", "m9", "--effort", "low"]
    assert (workdir / ".other" / "smoke" / "SKILL.md").is_file()


def test_model_override_without_slash_is_error(configured: Path) -> None:
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl", model="m9")
    assert outcome.stop_reason == StopReason.ERROR
    assert "runtime/模型名" in outcome.detail


def test_model_override_with_unknown_runtime_is_error(configured: Path) -> None:
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl", model="ghost/m9")
    assert outcome.stop_reason == StopReason.ERROR
    assert "ghost" in outcome.detail


def test_unknown_runtime_is_error(configured: Path) -> None:
    outcome = work("lost", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "nowhere" in outcome.detail


def test_missing_skill_directory_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(work_module, "SKILL_ROOT", configured / "empty")
    outcome = work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "skill" in outcome.detail


def test_provider_fills_command_and_session_environment(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_KEY", "gateway-key")
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    outcome = work("gated", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    assert recorded["command"] == ["/fake/bin/runner", "--model", "m1", "--base-url", "https://gateway.example"]
    assert recorded["env"]["API_BASE"] == "https://gateway.example"
    assert recorded["env"]["API_KEY"] == "gateway-key"
    assert recorded["env"]["MODEL"] == "m1"
    assert recorded["env"]["TONGTU_DISABLE"] == "1"
    assert recorded["env"]["PATH"] == "/tex/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def test_runtime_without_provider_adds_no_environment(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    record_run(monkeypatch, recorded, finished())
    work("smoke", configured / "paper", trace_path=configured / "trace.jsonl")
    assert "API_BASE" not in recorded["env"]
    assert "API_KEY" not in recorded["env"]


def test_runtime_provider_not_declared_is_error(configured: Path) -> None:
    outcome = work("ghosted", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "nowhere" in outcome.detail


def test_provider_without_key_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_KEY", raising=False)
    outcome = work("gated", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "GATEWAY_KEY" in outcome.detail


def test_placeholder_without_provider_is_error(configured: Path) -> None:
    outcome = work("unbound", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.ERROR
    assert "provider" in outcome.detail


def test_temporary_directory_is_filled_and_removed(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_KEY", "gateway-key")
    seen: dict = {}

    def fake_run(
        command: list[str],
        cwd: Path,
        timeout_seconds: float,
        *,
        stdout: IO[bytes],
        input_bytes: bytes,
        env: dict[str, str],
    ) -> ProcessOutcome:
        seen.update(command=command, env=env, existed=Path(env["CODEX_HOME"]).is_dir())
        return finished()

    monkeypatch.setattr(work_module, "run_in_process_group", fake_run)
    outcome = work("homed", configured / "paper", trace_path=configured / "trace.jsonl")
    assert outcome.stop_reason == StopReason.FINISHED
    tmp_dir = seen["env"]["CODEX_HOME"]
    assert Path(tmp_dir).is_absolute()
    assert seen["command"] == ["/fake/bin/runner", "--home", tmp_dir]
    assert seen["existed"]
    assert not Path(tmp_dir).exists()
