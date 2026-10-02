from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

from tongtu.model.config import Target
from tongtu.model.runtimes import RUNTIMES
from tongtu.model.work import StopReason, work

pytestmark = pytest.mark.llm

work_module = importlib.import_module("tongtu.model.work")

TABLE = """
[roles]
smoke_codex = { model = "codex/gpt-6-astra", effort = "low", timeout_seconds = 300 }
smoke_claude = { model = "claude-code/claude-haiku-4-5-20251001", effort = "low", max_turns = 5, timeout_seconds = 300 }
smoke_pi = { model = "pi", timeout_seconds = 300 }
sandbox_probe_codex = { model = "codex/gpt-6-astra", effort = "low", timeout_seconds = 300 }
sandbox_probe_claude = { model = "claude-code/claude-haiku-4-5-20251001", effort = "low", max_turns = 8, timeout_seconds = 300 }
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

TARGETS = {
    "codex": Target("codex", "gpt-6-astra", "low"),
    "claude-code": Target("claude-code", "claude-haiku-4-5-20251001", "low"),
    "pi": Target("pi", "", None),
}

HOME_PROBE = Path.home() / "tongtu-sandbox-probe.txt"


def require_login(name: str) -> None:
    ok, detail = RUNTIMES[name].login_check(TARGETS[name], os.environ)
    if not ok:
        pytest.skip(f"{name} 没有登录：{detail}")


def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, skill: str) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    config = tmp_path / "home" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(TABLE, encoding="utf-8")
    skill_root = tmp_path / "skill"
    (skill_root / role).mkdir(parents=True)
    (skill_root / role / "SKILL.md").write_text(skill, encoding="utf-8")
    monkeypatch.setattr(work_module, "SKILL_ROOT", skill_root)


def bash_results(trace_path: Path, needle: str) -> list[dict]:
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    uses: dict[str, dict] = {}
    results: list[dict] = []
    for event in events:
        message = event.get("message")
        for block in (message.get("content") if isinstance(message, dict) else None) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses[block.get("id", "")] = block
            if block.get("type") == "tool_result":
                use = uses.get(block.get("tool_use_id", ""), {})
                if use.get("name") == "Bash" and needle in json.dumps(use.get("input", {})):
                    results.append(block)
    return results


def codex_home_state() -> tuple[bool, list[tuple[str, float]]]:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if not home.is_dir():
        return False, []
    return True, sorted((str(path), path.stat().st_mtime) for path in home.rglob("*"))


def print_trace_lines(trace_path: Path, needle: str) -> None:
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if needle in line:
            print(f"写文件的事件： {line}")


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


def test_claude_code_sandbox_keeps_writes_inside_the_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "darwin":
        pytest.skip("claude-code 只在 darwin 上开沙箱")
    require_login("claude-code")
    prepared(tmp_path, monkeypatch, "sandbox_probe_claude", PROBE_SKILL)
    workdir = tmp_path / "probe" / "paper"
    workdir.mkdir(parents=True)
    trace_path = tmp_path / "logs" / "sandbox_probe_claude.jsonl"
    outcome = work("sandbox_probe_claude", workdir, trace_path=trace_path)
    print(f"trace： {trace_path} ")
    print(f"现场： {workdir} ")
    print(f"越界写的 tool_result： {[block.get('content') for block in bash_results(trace_path, 'outside.txt')]}")
    assert outcome.stop_reason == StopReason.FINISHED, outcome.detail
    assert (workdir / "inside.txt").exists()
    inside_touches = bash_results(trace_path, "inside.txt")
    assert any(not block.get("is_error") for block in inside_touches), (
        "inside.txt 不是经一次成功的 Bash 调用建出的，说明沙箱没有起来"
    )
    assert not (workdir.parent / "outside.txt").exists()
    assert not HOME_PROBE.exists()


def test_codex_sandbox_keeps_writes_inside_the_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    require_login("codex")
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
    assert not (workdir.parent / "outside.txt").exists()
    assert not HOME_PROBE.exists()
    assert codex_home_state() == before


def test_codex_sandbox_enforces_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    require_login("codex")
    prepared(tmp_path, monkeypatch, "sandbox_probe_codex", SMOKE_SKILL)
    site = tmp_path / "site"
    site.mkdir()
    (site / "escape").symlink_to(tmp_path, target_is_directory=True)
    outside_tmp = Path("/tmp") / f"tongtu-{tmp_path.name}-{site.stat().st_ino}.txt"
    paths = {
        "inside": "inside.txt",
        "outside": "../outside.txt",
        "symlink": "escape/escaped.txt",
        "tmp": str(outside_tmp),
        "skill": ".codex/skills/sandbox_probe_codex/SKILL.md",
    }
    script = (
        "import json, pathlib, socket\n"
        "results = {}\n"
        f"for name, target in {paths!r}.items():\n"
        "    try:\n"
        "        with pathlib.Path(target).open('a') as stream: stream.write('probe')\n"
        "        results[name] = 'allowed'\n"
        "    except OSError as error: results[name] = type(error).__name__\n"
        "try:\n"
        "    connection = socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "    connection.close()\n"
        "    results['network'] = 'allowed'\n"
        "except OSError as error: results['network'] = type(error).__name__\n"
        "pathlib.Path('results.json').write_text(json.dumps(results))\n"
    )
    (site / "probe.py").write_text(script)
    outcome = work(
        "sandbox_probe_codex",
        site,
        trace_path=tmp_path / "trace.jsonl",
        prompt=(
            "这是已授权的沙箱验证。读取并执行 python3 probe.py。必须实际尝试脚本中的测试操作，"
            "包括向临时现场外写测试标记和连接网络，以验证操作系统拒绝它们；"
            "不要预先因路径越界而跳过，也不要提权或绕过。脚本执行后结束。"
        ),
    )
    assert outcome.stop_reason is StopReason.FINISHED, outcome.detail
    assert json.loads((site / "results.json").read_text()) == {
        "inside": "allowed",
        "outside": "PermissionError",
        "symlink": "PermissionError",
        "tmp": "PermissionError",
        "skill": "PermissionError",
        "network": "PermissionError",
    }
    assert (site / "inside.txt").exists()
    assert not (tmp_path / "outside.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert not outside_tmp.exists()
    print(f"sandbox evidence: {tmp_path}")
