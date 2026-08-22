from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tongtu.model.config import (
    DEFAULT_ASK_MODEL,
    MODELS_TEMPLATE,
    Api,
    ModelsConfig,
    ProviderConfig,
    RoleTable,
    load_config,
    model_api,
    models_path,
    provider_key,
    resolve_role,
    role_config,
)

TABLE = """
[provider.demo]
base_url = "https://demo.example/v1"
api_key_env = "DEMO_KEY"

[provider.demo.models]
"chat-model" = "chat"

[provider.wide]
base_url = "https://wide.example"
api_key_env = "WIDE_KEY"
api = "messages"

[provider.odd]
base_url = "https://odd.example"
api_key_env = "ODD_KEY"
api = "grpc"

[runtime.claude_code]
skill_path = ".claude/skills/{role}"
command = ["claude", "-p"]

[roles]
translate = { provider = "demo", model = "chat-model", effort = "low" }
review = { runtime = "claude_code", model = "sonnet", effort = "high", max_turns = 8, timeout_seconds = 60, bash = [] }
"""


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "tongtu" / "models.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_template_parses_and_validates() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(MODELS_TEMPLATE))
    assert set(config.provider) == {"opencode", "anthropic"}
    assert set(config.runtime) == {"claude_code", "claude_code_opencode"}
    assert set(config.roles) == {"survey_terms", "translate", "review", "precompile_fix", "compile_fix"}
    assert config.provider["opencode"].models["deepseek-v4-flash"] == Api.CHAT
    assert config.roles["precompile_fix"].bash == ["latexmk", "xelatex", "kpsewhich"]
    assert config.roles["review"].bash == ["python3 -I validate.py"]
    assert config.provider["opencode"].base_url == "https://opencode.ai/zen/go"
    assert config.provider["anthropic"].base_url == "https://api.anthropic.com"
    assert config.provider["opencode"].api_key == ""
    assert config.provider["opencode"].api_key_env == "OPENCODE_API_KEY"
    assert set(DEFAULT_ASK_MODEL) == set(config.provider)


def test_template_runtime_carries_sandbox_settings() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(MODELS_TEMPLATE))
    runtime = config.runtime["claude_code"]
    assert runtime.settings == {
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True,
            "network": {"allowedDomains": []},
        }
    }
    assert "--setting-sources" in runtime.command
    assert "--strict-mcp-config" in runtime.command
    assert "Edit(.claude/skills/**)" in runtime.command
    assert "{settings}" in runtime.command
    assert runtime.provider is None
    assert runtime.env is None


def test_template_opencode_runtime_carries_provider_and_env() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(MODELS_TEMPLATE))
    runtime = config.runtime["claude_code_opencode"]
    plain = config.runtime["claude_code"]
    assert runtime.provider == "opencode"
    assert runtime.command == plain.command
    assert runtime.skill_path == plain.skill_path
    assert runtime.settings == plain.settings
    assert runtime.env == {
        "ANTHROPIC_BASE_URL": "{base_url}",
        "ANTHROPIC_API_KEY": "{api_key}",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "{model}",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "{model}",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "{model}",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    }
    assert {entry.runtime for entry in config.roles.values() if entry.runtime} == {"claude_code"}


def test_provider_key_prefers_written_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("DEMO_KEY", "from-env")
    provider = ProviderConfig(base_url="https://demo.example/v1", api_key="written", api_key_env="DEMO_KEY")
    assert provider_key("demo", provider) == ("written", "models.toml 的 api_key")


def test_provider_key_falls_back_to_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("DEMO_KEY", "from-env")
    provider = ProviderConfig(base_url="https://demo.example/v1", api_key="", api_key_env="DEMO_KEY")
    assert provider_key("demo", provider) == ("from-env", "环境变量 DEMO_KEY")


def test_provider_key_reports_both_sources_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("DEMO_KEY", "")
    provider = ProviderConfig(base_url="https://demo.example/v1", api_key_env="DEMO_KEY")
    key, detail = provider_key("demo", provider)
    assert key is None
    assert "api_key" in detail
    assert "DEMO_KEY" in detail


def test_provider_key_reports_no_variable_declared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    provider = ProviderConfig(base_url="https://demo.example/v1")
    key, detail = provider_key("demo", provider)
    assert key is None
    assert "api_key_env" in detail


def test_models_path_follows_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert models_path() == tmp_path / "tongtu" / "models.toml"


def test_load_config_reports_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config, detail = load_config()
    assert config is None
    assert "tongtu setup" in detail


def test_load_config_reports_broken_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, "[provider.demo\n")
    config, detail = load_config()
    assert config is None
    assert "TOML" in detail


def test_load_config_reports_role_missing_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, '[roles]\ntranslate = { provider = "demo", effort = "low" }\n')
    config, detail = load_config()
    assert config is None
    assert "model" in detail


def test_role_config_reports_unknown_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE)
    config, _ = load_config()
    assert config is not None
    entry, detail = role_config(config, "nobody")
    assert entry is None
    assert "nobody" in detail
    assert role_config(config, "translate")[0] is config.roles["translate"]


def test_model_api_reads_models_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE)
    config, _ = load_config()
    assert config is not None
    assert model_api(config, "demo", "chat-model") == (Api.CHAT, "")


def test_model_api_falls_back_to_provider_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE)
    config, _ = load_config()
    assert config is not None
    assert model_api(config, "wide", "any-model") == (Api.MESSAGES, "")


def test_model_api_reports_unknown_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE)
    config, _ = load_config()
    assert config is not None
    api, detail = model_api(config, "demo", "other-model")
    assert api is None
    assert "other-model" in detail


def test_model_api_reports_unknown_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE)
    config, _ = load_config()
    assert config is not None
    api, detail = model_api(config, "ghost", "chat-model")
    assert api is None
    assert "ghost" in detail


def test_model_api_rejects_unknown_api_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE)
    config, _ = load_config()
    assert config is not None
    api, detail = model_api(config, "odd", "any-model")
    assert api is None
    assert "grpc" in detail


def loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModelsConfig:
    write_config(tmp_path, monkeypatch, TABLE)
    config, _ = load_config()
    assert config is not None
    return config


def test_resolve_role_uses_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    resolved, detail = resolve_role(config, "translate", RoleTable.PROVIDER)
    assert detail == ""
    assert resolved is not None
    assert (resolved.provider, resolved.runtime, resolved.model, resolved.effort) == (
        "demo",
        None,
        "chat-model",
        "low",
    )


def test_resolve_role_applies_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    resolved, _ = resolve_role(config, "translate", RoleTable.PROVIDER, "wide/other-model", "high")
    assert resolved is not None
    assert (resolved.provider, resolved.model, resolved.effort) == ("wide", "other-model", "high")


def test_resolve_role_reads_runtime_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    resolved, _ = resolve_role(config, "review", RoleTable.RUNTIME)
    assert resolved is not None
    assert (resolved.provider, resolved.runtime, resolved.model) == (None, "claude_code", "sonnet")


def test_resolve_role_rejects_model_without_slash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    resolved, detail = resolve_role(config, "translate", RoleTable.PROVIDER, "chat-model")
    assert resolved is None
    assert "provider/模型名" in detail


def test_resolve_role_rejects_unknown_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    resolved, detail = resolve_role(config, "review", RoleTable.RUNTIME, "demo/sonnet")
    assert resolved is None
    assert "runtime demo" in detail
