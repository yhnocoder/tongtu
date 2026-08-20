from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tongtu.model.work import StopReason, work

pytestmark = pytest.mark.llm

work_module = importlib.import_module("tongtu.model.work")

TABLE = """
[runtime.claude_code]
skill_path = ".claude/skills/{role}"
command = ["claude", "-p", "--model", "{model}", "--effort", "{effort}", "--max-turns", "{max_turns}",
           "--output-format", "stream-json", "--verbose",
           "--setting-sources", "", "--strict-mcp-config",
           "--allowedTools", "Read,Edit,Write,Glob,Grep,{bash_allow}", "--permission-mode", "acceptEdits",
           "--disallowedTools", "Edit(.claude/skills/**)",
           "--settings", "{settings}"]
settings = { sandbox = { enabled = true, autoAllowBashIfSandboxed = true, allowUnsandboxedCommands = false, failIfUnavailable = true, network = { allowedDomains = [] } } }

[roles]
smoke = { runtime = "claude_code", model = "claude-haiku-4-5-20251001", effort = "low", max_turns = 5, timeout_seconds = 300, bash = [] }
"""

SKILL = """---
name: smoke
description: 在现场写一个 hello.txt
---

在当前目录写一个文件 hello.txt，文件内容只有一行、正好是五个小写字母 hello，不要加标点、不要改写、不要再做别的事，写完就结束。
"""


def test_work_runs_claude_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = tmp_path / "config" / "tongtu" / "models.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(TABLE, encoding="utf-8")
    skill_root = tmp_path / "skill"
    (skill_root / "smoke").mkdir(parents=True)
    (skill_root / "smoke" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    monkeypatch.setattr(work_module, "SKILL_ROOT", skill_root)

    workdir = tmp_path / "paper"
    workdir.mkdir()
    trace_path = tmp_path / "logs" / "smoke.jsonl"
    outcome = work("smoke", workdir, trace_path=trace_path)
    assert outcome.stop_reason == StopReason.FINISHED, outcome.detail
    assert "hello" in (workdir / "hello.txt").read_text(encoding="utf-8").strip().lower()
    assert trace_path.stat().st_size > 0
    print(f"trace： {trace_path} ")
    print(f"现场： {workdir} ")
