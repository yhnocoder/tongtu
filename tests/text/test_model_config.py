from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tongtu.config import config_path
from tongtu.model.config import (
    DEFAULT_ASK_MODEL,
    MODELS_TEMPLATE,
    Api,
    FontFamily,
    FontsConfig,
    ModelsConfig,
    ProviderConfig,
    RoleTable,
    load_config,
    model_api,
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
review = { runtime = "claude_code", model = "sonnet", effort = "high", max_turns = 8, timeout_seconds = 60 }
"""


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_template_parses_and_validates() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(MODELS_TEMPLATE))
    assert set(config.provider) == {"opencode", "deepseek", "anthropic"}
    assert set(config.runtime) == {"codex", "codex_opencode"}
    assert set(config.roles) == {"survey_terms", "translate", "review", "precompile_fix", "compile_fix"}
    assert config.provider["opencode"].models["deepseek-v4-flash"] == Api.CHAT
    assert config.roles["review"].timeout_seconds == 3600
    assert config.provider["opencode"].base_url == "https://opencode.ai/zen/go"
    assert config.provider["anthropic"].base_url == "https://api.anthropic.com"
    assert config.provider["opencode"].api_key == ""
    assert config.provider["opencode"].api_key_env == "OPENCODE_API_KEY"
    assert set(DEFAULT_ASK_MODEL) == set(config.provider)


def test_template_fonts_match_defaults() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(MODELS_TEMPLATE))
    assert config.fonts == FontsConfig()
    assert config.fonts.main == FontFamily(regular="LXGWWenKai-Light.ttf", bold="LXGWWenKai-Medium.ttf")
    assert config.fonts.sans == FontFamily(regular="SourceHanSansSC-Regular.otf", bold="SourceHanSansSC-Bold.otf")
    assert config.fonts.mono is None


def test_fonts_defaults_are_not_shared_between_instances() -> None:
    first, second = FontsConfig(), FontsConfig()
    assert first.main == second.main
    assert first.main is not second.main


def test_fonts_table_reads_strings_and_family_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        tmp_path,
        monkeypatch,
        TABLE + '\n[fonts]\nmain = "MyFont.ttf"\nsans = { regular = "~/fonts/Sans.otf", bold = "SansBold.otf" }\n'
        'mono = "Mono.ttc"\n',
    )
    config, detail = load_config()
    assert detail == ""
    assert config is not None
    assert config.fonts.main == "MyFont.ttf"
    assert config.fonts.sans == FontFamily(regular="~/fonts/Sans.otf", bold="SansBold.otf")
    assert config.fonts.mono == "Mono.ttc"


def test_fonts_table_family_without_bold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE + '\n[fonts]\nmain = { regular = "MyFont.ttf" }\n')
    config, detail = load_config()
    assert detail == ""
    assert config is not None
    assert config.fonts.main == FontFamily(regular="MyFont.ttf", bold=None)


def test_fonts_table_rejects_old_bold_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE + '\n[fonts]\nbold = "LXGWWenKai-Medium.ttf"\nsans_bold = "Bold.otf"\n')
    config, detail = load_config()
    assert config is None
    assert "fonts.bold" in detail
    assert "fonts.sans_bold" in detail


def test_fonts_table_rejects_unknown_family_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE + '\n[fonts]\nmain = { regular = "R.ttf", italic = "I.ttf" }\n')
    config, detail = load_config()
    assert config is None
    assert "fonts.main" in detail
    assert "italic" in detail


def test_fonts_table_rejects_family_without_regular(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE + '\n[fonts]\nsans = { bold = "B.otf" }\n')
    config, detail = load_config()
    assert config is None
    assert "fonts.sans" in detail
    assert "regular" in detail


def test_fonts_table_rejects_fallback_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        tmp_path,
        monkeypatch,
        TABLE + '\n[fonts]\nmain = ["Source Han Serif SC", "LXGWWenKai-Light.ttf"]\nsans = ["Noto Sans CJK SC"]\n',
    )
    config, detail = load_config()
    assert config is None
    assert "fonts.main" in detail
    assert "fonts.sans" in detail


def test_fonts_table_rejects_relative_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE + '\n[fonts]\nmain = "myfonts/MyFont.ttf"\nmono = "fonts/Mono.otf"\n')
    config, detail = load_config()
    assert config is None
    assert "fonts.main" in detail
    assert "fonts.mono" in detail
    assert "font path myfonts/MyFont.ttf must be absolute or start with ~" in detail


def test_fonts_table_rejects_relative_paths_in_a_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE + '\n[fonts]\nsans = { regular = "Sans.otf", bold = "fonts/Bold.otf" }\n')
    config, detail = load_config()
    assert config is None
    assert "fonts.sans" in detail
    assert "font path fonts/Bold.otf must be absolute or start with ~" in detail


def test_fonts_table_accepts_home_and_absolute_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sans = tmp_path / "custom" / "Sans.otf"
    bold = tmp_path / "custom" / "SansBold.otf"
    write_config(
        tmp_path,
        monkeypatch,
        TABLE + f'\n[fonts]\nmain = "~/fonts/MyFont.ttf"\nsans = {{ regular = "{sans}", bold = "{bold}" }}\n',
    )
    config, detail = load_config()
    assert detail == ""
    assert config is not None
    assert config.fonts.main == "~/fonts/MyFont.ttf"
    assert config.fonts.sans == FontFamily(regular=str(sans), bold=str(bold))


def test_fonts_table_defaults_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE)
    config, _ = load_config()
    assert config is not None
    assert config.fonts == FontsConfig()


def preserved_claude_config() -> ModelsConfig:
    start = MODELS_TEMPLATE.index("# [runtime.claude_code]")
    end = MODELS_TEMPLATE.index("# Codex 登录会话", start)
    uncommented = "\n".join(
        line.removeprefix("# ") for line in MODELS_TEMPLATE[start:end].splitlines() if line.startswith("# ")
    )
    lines = []
    for line in uncommented.splitlines():
        if line.startswith(("[", "skill_path", "events", "command", "settings", "env", "provider", "           ")):
            lines.append(line)
    return ModelsConfig.model_validate(tomllib.loads("\n".join(lines)))


def test_template_runtime_carries_sandbox_settings() -> None:
    config = preserved_claude_config()
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
    assert runtime.env == {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_BASE_URL": ""}


def test_template_opencode_runtime_carries_provider_and_env() -> None:
    config = preserved_claude_config()
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


def test_template_codex_runtime_carries_provider_and_env() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(MODELS_TEMPLATE))
    runtime = config.runtime["codex_opencode"]
    assert runtime.provider == "opencode"
    assert runtime.skill_path == ".codex/skills/{role}"
    assert runtime.command[0] == "codex"
    assert "--ignore-user-config" in runtime.command
    assert "--ignore-rules" in runtime.command
    assert runtime.settings is None
    assert runtime.env == {"OPENCODE_API_KEY": "{api_key}", "CODEX_HOME": "{tmp_dir}"}


def test_template_runtimes_declare_events() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(MODELS_TEMPLATE))
    assert config.runtime["codex"].events == "codex-json"
    assert config.runtime["codex_opencode"].events == "codex-json"


def test_events_field_defaults_to_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE)
    config, detail = load_config()
    assert detail == ""
    assert config is not None
    assert config.runtime["claude_code"].events is None


def test_events_field_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        tmp_path,
        monkeypatch,
        TABLE.replace('command = ["claude", "-p"]', 'command = ["claude", "-p"]\nevents = "stream-json"'),
    )
    config, detail = load_config()
    assert detail == ""
    assert config is not None
    assert config.runtime["claude_code"].events == "stream-json"


def test_unknown_events_value_fails_the_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        tmp_path,
        monkeypatch,
        TABLE.replace('command = ["claude", "-p"]', 'command = ["claude", "-p"]\nevents = "verbose-text"'),
    )
    config, detail = load_config()
    assert config is None
    assert "claude_code" in detail
    assert "verbose-text" in detail
    assert "codex-json" in detail
    assert "stream-json" in detail


def test_provider_key_prefers_written_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    monkeypatch.setenv("DEMO_KEY", "from-env")
    provider = ProviderConfig(base_url="https://demo.example/v1", api_key="written", api_key_env="DEMO_KEY")
    assert provider_key("demo", provider) == ("written", "api_key in config.toml")


def test_provider_key_falls_back_to_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    monkeypatch.setenv("DEMO_KEY", "from-env")
    provider = ProviderConfig(base_url="https://demo.example/v1", api_key="", api_key_env="DEMO_KEY")
    assert provider_key("demo", provider) == ("from-env", "environment variable DEMO_KEY")


def test_provider_key_reports_both_sources_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    monkeypatch.setenv("DEMO_KEY", "")
    provider = ProviderConfig(base_url="https://demo.example/v1", api_key_env="DEMO_KEY")
    key, detail = provider_key("demo", provider)
    assert key is None
    assert "api_key" in detail
    assert "DEMO_KEY" in detail


def test_provider_key_reports_no_variable_declared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    provider = ProviderConfig(base_url="https://demo.example/v1")
    key, detail = provider_key("demo", provider)
    assert key is None
    assert "api_key_env" in detail


def test_config_path_follows_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    assert config_path() == tmp_path / "config.toml"


def test_load_config_reports_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
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
    assert "provider/model" in detail


def test_resolve_role_rejects_unknown_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    resolved, detail = resolve_role(config, "review", RoleTable.RUNTIME, "demo/sonnet")
    assert resolved is None
    assert "runtime demo" in detail


def test_codex_defaults_resolve_aliases_and_require_sandbox() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(MODELS_TEMPLATE))
    resolved, detail = resolve_role(config, "translate", RoleTable.RUNTIME)
    assert detail == ""
    assert resolved is not None
    assert (resolved.runtime, resolved.model, resolved.effort) == ("codex", "gpt-6-astra", "low")
    runtime = config.runtime["codex"]
    assert runtime.auth == "codex"
    assert "workspace-write" in runtime.command
    for setting in (
        'approval_policy="never"',
        "sandbox_workspace_write.network_access=false",
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "allow_login_shell=false",
    ):
        assert setting in runtime.command
    assert config.roles["translate"].chunk_tokens == 20000


@pytest.mark.parametrize(
    "limits", ["chunk_tokens = 0", "chunk_tokens = -1", "chunk_tokens = true", "chunk_merge_tokens = 6000"]
)
def test_invalid_chunk_limits_fail_config_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limits: str) -> None:
    write_config(tmp_path, monkeypatch, '[roles.translate]\nmodel="astra"\neffort="light"\n' + limits)
    config, detail = load_config()
    assert config is None
    assert "chunk" in detail
