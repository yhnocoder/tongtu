from __future__ import annotations

import shutil
import stat
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tongtu.cli import app, main
from tongtu.model.config import MODELS_TEMPLATE

runner = CliRunner()

TABLE = """
[provider.demo]
base_url = "https://demo.example/v1"
api_key_env = "DEMO_KEY"
api = "chat"

[provider.written]
base_url = "https://written.example/v1"
api_key = "written-key"
api = "chat"

[provider.unused]
base_url = "https://unused.example/v1"
api_key_env = "UNUSED_KEY"
api = "chat"

[runtime.demo_runtime]
skill_path = ".agent/skills/{role}"
command = ["runner", "-p"]

[roles]
translate = { provider = "demo", model = "m1", effort = "low" }
survey_terms = { provider = "written", model = "m1", effort = "low" }
review = { runtime = "demo_runtime", model = "m1", effort = "low", max_turns = 4, timeout_seconds = 60, bash = [] }
"""


KEYLESS_TABLE = """
[provider.demo]
base_url = "https://demo.example/v1"
api = "chat"

[roles]
translate = { provider = "demo", model = "m1", effort = "low" }
"""


def squeeze(text: str) -> str:
    return "".join(text.split())


def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "tongtu" / "models.toml"


def written_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = config_path(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(TABLE, encoding="utf-8")
    return path


def test_setup_writes_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert path.read_text(encoding="utf-8") == MODELS_TEMPLATE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_setup_does_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text("手改过的配置\n", encoding="utf-8")
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert path.read_text(encoding="utf-8") == "手改过的配置\n"
    assert "不覆盖" in squeeze(result.stdout)


def test_setup_interactive_fills_first_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup", "-i"], input="y\nzen-key\nn\n")
    assert result.exit_code == 0
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["provider"]["opencode"]["api_key"] == "zen-key"
    assert written["provider"]["opencode"]["api_key_env"] == "OPENCODE_API_KEY"
    assert written["provider"]["anthropic"]["api_key"] == ""
    assert written["roles"]["translate"] == {
        "provider": "opencode",
        "model": "deepseek-v4-flash",
        "effort": "low",
    }
    assert written["roles"]["survey_terms"]["provider"] == "opencode"
    assert written["roles"]["review"]["runtime"] == "claude_code"
    assert written["roles"]["review"]["model"] == "claude-sonnet-5"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_setup_interactive_points_ask_roles_at_second_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup", "-i"], input="n\ny\nsk-key\n")
    assert result.exit_code == 0
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["provider"]["anthropic"]["api_key"] == "sk-key"
    assert written["provider"]["opencode"]["api_key"] == ""
    assert written["roles"]["translate"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "effort": "low",
    }


def test_setup_interactive_without_any_provider_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup", "-i"], input="n\nn\n")
    assert result.exit_code == 2
    assert not path.exists()


def test_doctor_without_config_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "tongtusetup" in output
    assert "工具链与字体齐全" in output


def test_doctor_missing_toolchain_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "环境有缺失：xelatex" in squeeze(result.stdout)


def test_doctor_lists_only_referenced_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "环境齐全。" in output
    assert "密钥demo" in output
    assert "密钥unused" not in output
    assert "运行时demo_runtime" in output
    assert "环境变量DEMO_KEY" in output
    assert "models.toml的api_key" in output


def test_doctor_reports_missing_key_without_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.delenv("DEMO_KEY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "工具链与字体齐全" in output
    assert "密钥demo" in output


def test_doctor_keeps_table_names_in_the_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(KEYLESS_TABLE, encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "[provider.demo]" in result.stdout


def test_setup_keeps_the_path_on_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    monkeypatch.setenv("COLUMNS", "60")
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert [line for line in result.stdout.splitlines() if "已写出" in line and str(path) in line]


def test_entry_point_refuses_to_run_inside_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_DISABLE", "1")
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
