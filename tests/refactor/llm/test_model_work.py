from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

from tongtu.model.work import StopReason, work

pytestmark = pytest.mark.llm

work_module = importlib.import_module("tongtu.model.work")

OPENCODE_KEY_ABSENT = not os.environ.get("OPENCODE_API_KEY")

TABLE = """
[provider.opencode]
base_url = "https://opencode.ai/zen/go"
api_key_env = "OPENCODE_API_KEY"

[provider.opencode.models]
"deepseek-v4-flash" = "chat"

[runtime.claude_code]
skill_path = ".claude/skills/{role}"
command = ["claude", "-p", "--model", "{model}", "--effort", "{effort}", "--max-turns", "{max_turns}",
           "--output-format", "stream-json", "--verbose",
           "--setting-sources", "", "--strict-mcp-config",
           "--allowedTools", "Read,Edit,Write,Glob,Grep,{bash_allow}", "--permission-mode", "acceptEdits",
           "--disallowedTools", "Edit(.claude/skills/**)",
           "--settings", "{settings}"]
settings = { sandbox = { enabled = true, autoAllowBashIfSandboxed = true, allowUnsandboxedCommands = false, failIfUnavailable = true, network = { allowedDomains = [] } } }

[runtime.claude_code_opencode]
provider = "opencode"
skill_path = ".claude/skills/{role}"
command = ["claude", "-p", "--model", "{model}", "--effort", "{effort}", "--max-turns", "{max_turns}",
           "--output-format", "stream-json", "--verbose",
           "--setting-sources", "", "--strict-mcp-config",
           "--allowedTools", "Read,Edit,Write,Glob,Grep,{bash_allow}", "--permission-mode", "acceptEdits",
           "--disallowedTools", "Edit(.claude/skills/**)",
           "--settings", "{settings}"]
settings = { sandbox = { enabled = true, autoAllowBashIfSandboxed = true, allowUnsandboxedCommands = false, failIfUnavailable = true, network = { allowedDomains = [] } } }
env = { ANTHROPIC_BASE_URL = "{base_url}", ANTHROPIC_API_KEY = "{api_key}", ANTHROPIC_DEFAULT_HAIKU_MODEL = "{model}", ANTHROPIC_DEFAULT_SONNET_MODEL = "{model}", ANTHROPIC_DEFAULT_OPUS_MODEL = "{model}", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1", DISABLE_TELEMETRY = "1" }

[runtime.codex_opencode]
provider   = "opencode"
skill_path = ".codex/skills/{role}"
command = ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
           "-s", "workspace-write", "-c", 'approval_policy="never"',
           "-m", "{model}", "-c", 'model_reasoning_effort="{effort}"',
           "-c", 'model_provider="opencode"', "-c", 'model_providers.opencode.name="opencode"',
           "-c", 'model_providers.opencode.base_url="{base_url}/v1"',
           "-c", 'model_providers.opencode.env_key="OPENCODE_API_KEY"',
           "-c", 'model_providers.opencode.wire_api="responses"']
env = { OPENCODE_API_KEY = "{api_key}", CODEX_HOME = "{tmp_dir}" }

[roles]
smoke = { runtime = "claude_code", model = "claude-haiku-4-5-20251001", effort = "low", max_turns = 5, timeout_seconds = 300, bash = [] }
sandbox_probe = { runtime = "claude_code", model = "claude-haiku-4-5-20251001", effort = "low", max_turns = 8, timeout_seconds = 300, bash = ["touch"] }
smoke_opencode = { runtime = "claude_code_opencode", model = "deepseek-v4-flash", effort = "low", max_turns = 5, timeout_seconds = 300, bash = [] }
sandbox_probe_opencode = { runtime = "claude_code_opencode", model = "deepseek-v4-flash", effort = "low", max_turns = 8, timeout_seconds = 300, bash = ["touch"] }
smoke_codex = { runtime = "codex_opencode", model = "deepseek-v4-flash", effort = "low", max_turns = 5, timeout_seconds = 300, bash = [] }
sandbox_probe_codex = { runtime = "codex_opencode", model = "deepseek-v4-flash", effort = "low", max_turns = 8, timeout_seconds = 300, bash = ["touch"] }
"""

SMOKE_SKILL = """---
name: smoke
description: 在现场写一个 hello.txt
---

在当前目录写一个文件 hello.txt，文件内容只有一行、正好是五个小写字母 hello，不要加标点、不要改写、不要再做别的事，写完就结束。
"""

PROBE_SKILL = """---
name: sandbox_probe
description: 在现场外与现场内各建一个文件
---

用 shell 依次执行三条命令：`touch ../outside.txt`、`touch "$HOME/tongtu-sandbox-probe.txt"`、`touch inside.txt`；前面的失败也继续执行后面的；三条都跑过就结束，不做别的。
"""


HOME_PROBE = Path.home() / "tongtu-sandbox-probe.txt"


def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, skill: str) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = tmp_path / "config" / "tongtu" / "models.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(TABLE, encoding="utf-8")
    skill_root = tmp_path / "skill"
    (skill_root / role).mkdir(parents=True)
    (skill_root / role / "SKILL.md").write_text(skill, encoding="utf-8")
    monkeypatch.setattr(work_module, "SKILL_ROOT", skill_root)


def tool_result_for(trace_path: Path, needle: str) -> str:
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    wanted = ""
    for event in events:
        message = event.get("message")
        for block in (message.get("content") if isinstance(message, dict) else None) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and needle in json.dumps(block.get("input", {})):
                wanted = block.get("id", "")
            if block.get("type") == "tool_result" and block.get("tool_use_id") == wanted:
                return json.dumps(block.get("content"), ensure_ascii=False)
    return ""


def codex_home_state() -> tuple[bool, list[tuple[str, float]]]:
    home = Path.home() / ".codex"
    if not home.is_dir():
        return False, []
    return True, sorted((str(path), path.stat().st_mtime) for path in home.rglob("*"))


def print_trace_lines(trace_path: Path, needle: str) -> None:
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if needle in line:
            print(f"写文件的事件： {line}")


def test_work_runs_claude_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared(tmp_path, monkeypatch, "smoke", SMOKE_SKILL)
    workdir = tmp_path / "paper"
    workdir.mkdir()
    trace_path = tmp_path / "logs" / "smoke.jsonl"
    outcome = work("smoke", workdir, trace_path=trace_path)
    assert outcome.stop_reason == StopReason.FINISHED, outcome.detail
    assert "hello" in (workdir / "hello.txt").read_text(encoding="utf-8").strip().lower()
    assert trace_path.stat().st_size > 0
    print(f"trace： {trace_path} ")
    print(f"现场： {workdir} ")


def test_sandbox_keeps_writes_inside_the_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared(tmp_path, monkeypatch, "sandbox_probe", PROBE_SKILL)
    workdir = tmp_path / "probe" / "paper"
    workdir.mkdir(parents=True)
    trace_path = tmp_path / "logs" / "sandbox_probe.jsonl"
    outcome = work("sandbox_probe", workdir, trace_path=trace_path)
    print(f"trace： {trace_path} ")
    print(f"现场： {workdir} ")
    print(f"越界写的 tool_result： {tool_result_for(trace_path, 'outside.txt')}")
    assert outcome.stop_reason == StopReason.FINISHED, outcome.detail
    assert (workdir / "inside.txt").exists()
    assert not (workdir.parent / "outside.txt").exists()
    assert not HOME_PROBE.exists()


@pytest.mark.skipif(OPENCODE_KEY_ABSENT, reason="没有 OPENCODE_API_KEY")
def test_work_runs_claude_code_on_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared(tmp_path, monkeypatch, "smoke_opencode", SMOKE_SKILL)
    workdir = tmp_path / "paper"
    workdir.mkdir()
    trace_path = tmp_path / "logs" / "smoke_opencode.jsonl"
    outcome = work("smoke_opencode", workdir, trace_path=trace_path)
    assert outcome.stop_reason == StopReason.FINISHED, outcome.detail
    assert "hello" in (workdir / "hello.txt").read_text(encoding="utf-8").strip().lower()
    assert trace_path.stat().st_size > 0
    print(f"trace： {trace_path} ")
    print(f"现场： {workdir} ")


@pytest.mark.skipif(OPENCODE_KEY_ABSENT, reason="没有 OPENCODE_API_KEY")
def test_opencode_sandbox_keeps_writes_inside_the_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared(tmp_path, monkeypatch, "sandbox_probe_opencode", PROBE_SKILL)
    workdir = tmp_path / "probe" / "paper"
    workdir.mkdir(parents=True)
    trace_path = tmp_path / "logs" / "sandbox_probe_opencode.jsonl"
    outcome = work("sandbox_probe_opencode", workdir, trace_path=trace_path)
    print(f"trace： {trace_path} ")
    print(f"现场： {workdir} ")
    print(f"越界写的 tool_result： {tool_result_for(trace_path, 'outside.txt')}")
    assert outcome.stop_reason == StopReason.FINISHED, outcome.detail
    assert (workdir / "inside.txt").exists()
    assert not (workdir.parent / "outside.txt").exists()
    assert not HOME_PROBE.exists()


@pytest.mark.skipif(OPENCODE_KEY_ABSENT, reason="没有 OPENCODE_API_KEY")
def test_work_runs_codex_on_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared(tmp_path, monkeypatch, "smoke_codex", SMOKE_SKILL)
    workdir = tmp_path / "paper"
    workdir.mkdir()
    trace_path = tmp_path / "logs" / "smoke_codex.jsonl"
    outcome = work("smoke_codex", workdir, trace_path=trace_path)
    assert outcome.stop_reason == StopReason.FINISHED, outcome.detail
    assert "hello" in (workdir / "hello.txt").read_text(encoding="utf-8").strip().lower()
    assert trace_path.stat().st_size > 0
    print(f"trace： {trace_path} ")
    print(f"现场： {workdir} ")


@pytest.mark.skipif(OPENCODE_KEY_ABSENT, reason="没有 OPENCODE_API_KEY")
def test_codex_sandbox_keeps_writes_inside_the_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared(tmp_path, monkeypatch, "sandbox_probe_codex", PROBE_SKILL)
    workdir = tmp_path / "probe" / "paper"
    workdir.mkdir(parents=True)
    trace_path = tmp_path / "logs" / "sandbox_probe_codex.jsonl"
    before = codex_home_state()
    outcome = work("sandbox_probe_codex", workdir, trace_path=trace_path)
    print(f"trace： {trace_path} ")
    print(f"现场： {workdir} ")
    print_trace_lines(trace_path, "touch")
    assert outcome.stop_reason == StopReason.FINISHED, outcome.detail
    assert (workdir / "inside.txt").exists()
    assert not HOME_PROBE.exists()
    assert codex_home_state() == before
