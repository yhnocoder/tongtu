from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tongtu.artifacts.common import StopReason
from tongtu.model.work import work
from tongtu.workdir import raw_log_path

pytestmark = pytest.mark.llm

work_module = importlib.import_module("tongtu.model.work")

RUNTIME = "claude-code"

TABLE = """
[modes.fast]
model = "claude-code/claude-haiku-5-5"
effort = "low"
timeout_seconds = 300

[modes.smart]
model = "claude-code/claude-haiku-5-5"
effort = "low"
timeout_seconds = 300
"""

SKILL = "smoke"

SMOKE_SKILL = """---
name: smoke
description: 在现场写一个 hello.txt
---

在当前目录写一个文件 hello.txt，文件内容只有一行、正好是五个小写字母 hello，不要加标点、不要改写、不要再做别的事，写完就结束。
"""


def require_login(name: str) -> None:
    agentrun = shutil.which("agentrun")
    if agentrun is None:
        pytest.skip("PATH 里没有 agentrun")
    completed = subprocess.run([agentrun, "doctor", name, "--json"], capture_output=True, text=True)
    try:
        checks = {check["check"]: check for check in json.loads(completed.stdout)}
    except (ValueError, TypeError, KeyError):
        pytest.skip(f"agentrun doctor {name} 没有输出检查结果：{completed.stderr.strip()}")
    login = checks.get("login", {})
    if login.get("status") != "ok":
        pytest.skip(f"{name} 没有登录：{login.get('detail')}")


def test_work_runs_a_minimal_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    require_login(RUNTIME)
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.toml").write_text(TABLE, encoding="utf-8")
    skill_root = tmp_path / "skill"
    (skill_root / SKILL).mkdir(parents=True)
    (skill_root / SKILL / "SKILL.md").write_text(SMOKE_SKILL, encoding="utf-8")
    monkeypatch.setattr(work_module, "SKILL_ROOT", skill_root)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    log_path = tmp_path / "logs" / f"{SKILL}.jsonl"
    outcome = work(SKILL, session_dir, mode="fast", log_path=log_path)
    print(outcome.session.model_dump_json(indent=2))
    assert outcome.session.stop_reason is StopReason.FINISHED, outcome.detail
    assert outcome.session.model == "claude-code/claude-haiku-5-5"
    assert outcome.session.effort == "low"
    assert (session_dir / "hello.txt").read_text(encoding="utf-8").strip().lower() == "hello"
    assert log_path.stat().st_size > 0
    assert raw_log_path(log_path).stat().st_size > 0
    usage = outcome.session.usage
    assert usage is not None and usage.turns >= 1 and usage.output_tokens > 0
    assert usage.cost_usd is not None and usage.cost_usd > 0
