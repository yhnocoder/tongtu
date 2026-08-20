from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tongtu.cli import app
from tongtu.model.config import MODELS_TEMPLATE

runner = CliRunner()

TABLE = """
[provider.demo]
base_url = "https://demo.example/v1"
api_key_env = "DEMO_KEY"
api = "chat"

[runtime.demo_runtime]
skill_path = ".agent/skills/{role}"
command = ["runner", "-p"]

[roles]
translate = { provider = "demo", model = "m1", effort = "low" }
"""

INTERACTIVE_INPUT = "zen\nhttps://zen.example/v1\nZEN_KEY\nchat\n\nm1\nlow\nm2\nlow\nm3\nhigh\nm4\nxhigh\nm5\nxhigh\n"


def squeeze(text: str) -> str:
    return "".join(text.split())


def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "tongtu" / "models.toml"


def test_setup_writes_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert path.read_text(encoding="utf-8") == MODELS_TEMPLATE


def test_setup_does_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text("手改过的配置\n", encoding="utf-8")
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert path.read_text(encoding="utf-8") == "手改过的配置\n"
    assert "不覆盖" in squeeze(result.stdout)


def test_setup_interactive_writes_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup", "-i"], input=INTERACTIVE_INPUT)
    assert result.exit_code == 0
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["provider"]["zen"] == {
        "base_url": "https://zen.example/v1",
        "api_key_env": "ZEN_KEY",
        "api": "chat",
    }
    assert written["runtime"]["claude_code"]["command"][0] == "claude"
    assert written["roles"]["translate"] == {"provider": "zen", "model": "m2", "effort": "low"}
    assert written["roles"]["compile_fix"] == {
        "runtime": "claude_code",
        "model": "m5",
        "effort": "xhigh",
        "max_turns": 40,
        "timeout_seconds": 1800,
        "bash": ["latexmk", "xelatex", "kpsewhich"],
    }


def test_doctor_without_config_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "tongtusetup" in output
    assert "工具链与字体齐全" in output


def test_doctor_missing_toolchain_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(TABLE, encoding="utf-8")
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "环境有缺失：xelatex" in squeeze(result.stdout)


def test_doctor_all_present_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(TABLE, encoding="utf-8")
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "环境齐全。" in output
    assert "密钥demo" in output
    assert "运行时demo_runtime" in output
