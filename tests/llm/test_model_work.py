from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tongtu.model.work import StopReason, work

pytestmark = pytest.mark.llm

work_module = importlib.import_module("tongtu.model.work")

TABLE = """
[roles]
smoke_codex = { model = "codex/gpt-6-astra", effort = "low", timeout_seconds = 300 }
smoke_claude = { model = "claude-code/claude-haiku-4-5-20251001", effort = "low", max_turns = 5, timeout_seconds = 300 }
smoke_pi = { model = "pi", timeout_seconds = 300 }
probe_codex = { model = "codex/gpt-6-astra", effort = "low", timeout_seconds = 300 }
probe_claude = { model = "claude-code/claude-haiku-4-5-20251001", effort = "low", max_turns = 8, timeout_seconds = 300 }
probe_pi = { model = "pi", timeout_seconds = 300 }
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


def require_login(name: str) -> None:
    agentrun = shutil.which("agentrun")
    if agentrun is None:
        pytest.skip("PATH 里没有 agentrun")
    completed = subprocess.run([agentrun, "doctor", name, "--json"], capture_output=True, text=True)
    login = next(check for check in json.loads(completed.stdout) if check["check"] == "login")
    if login["status"] != "ok":
        pytest.skip(f"{name} 没有登录：{login['detail']}")


def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, skill: str) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    config = tmp_path / "home" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(TABLE, encoding="utf-8")
    skill_root = tmp_path / "skill"
    (skill_root / role).mkdir(parents=True)
    (skill_root / role / "SKILL.md").write_text(skill, encoding="utf-8")
    monkeypatch.setattr(work_module, "SKILL_ROOT", skill_root)


def run_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    prepared(tmp_path, monkeypatch, role, SMOKE_SKILL)
    workdir = tmp_path / "paper"
    workdir.mkdir()
    trace_path = tmp_path / "logs" / f"{role}.jsonl"
    outcome = work(role, workdir, trace_path=trace_path)
    assert outcome.stop_reason == StopReason.FINISHED, outcome.detail
    assert "hello" in (workdir / "hello.txt").read_text(encoding="utf-8").strip().lower()
    assert trace_path.stat().st_size > 0
    print(f"trace： {trace_path} ")
    print(f"现场： {workdir} ")


def test_work_runs_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    require_login("codex")
    run_smoke(tmp_path, monkeypatch, "smoke_codex")


def test_work_runs_claude_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    require_login("claude-code")
    run_smoke(tmp_path, monkeypatch, "smoke_claude")


def test_work_runs_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    require_login("pi")
    run_smoke(tmp_path, monkeypatch, "smoke_pi")


@pytest.mark.parametrize("runtime", ["codex", "claude-code", "pi"])
def test_sandbox_keeps_writes_inside_the_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str) -> None:
    require_login(runtime)
    role = f"probe_{runtime.partition('-')[0]}"
    prepared(tmp_path, monkeypatch, role, PROBE_SKILL)
    workdir = tmp_path / "probe" / "paper"
    workdir.mkdir(parents=True)
    trace_path = tmp_path / "logs" / f"{role}.jsonl"
    outcome = work(role, workdir, trace_path=trace_path)
    print(f"trace： {trace_path} ")
    print(f"现场： {workdir} ")
    assert outcome.stop_reason == StopReason.FINISHED, outcome.detail
    assert (workdir / "inside.txt").exists()
    assert not (workdir.parent / "outside.txt").exists()
    assert not HOME_PROBE.exists()
