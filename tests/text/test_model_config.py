from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tongtu.config import config_path
from tongtu.model.config import (
    CONFIG_TEMPLATE,
    RUNTIME_NAMES,
    Api,
    ChunkingConfig,
    FontFamily,
    FontsConfig,
    ModelsConfig,
    ProviderConfig,
    Target,
    load_config,
    model_api,
    parse_target,
    provider_key,
    role_target,
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

[roles]
translate = { model = "demo/chat-model", effort = "low" }
review = { model = "claude-code/sonnet", effort = "high", max_turns = 8, timeout_seconds = 60 }
compile_fix = { model = "pi", timeout_seconds = 60 }
bare = { model = "demo/chat-model" }
slashless = { model = "demo" }
ghost = { model = "ghost/m" }
"""


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str = TABLE) -> ModelsConfig:
    write_config(tmp_path, monkeypatch, text)
    config, detail = load_config()
    assert config is not None, detail
    return config


def test_template_parses_and_validates() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(CONFIG_TEMPLATE))
    assert set(config.provider) == {"deepseek"}
    assert set(config.roles) == {"survey_terms", "translate", "review", "precompile_fix", "compile_fix"}
    assert config.chunking == ChunkingConfig(chunk_tokens=20000, chunk_merge_tokens=12000)
    assert config.provider["deepseek"].base_url == "https://api.deepseek.com"
    assert config.provider["deepseek"].api_key == ""
    assert config.provider["deepseek"].api_key_env == "DEEPSEEK_API_KEY"
    assert config.provider["deepseek"].api == "chat"
    assert config.roles["survey_terms"].model == "deepseek/deepseek-flash"
    assert config.roles["review"].timeout_seconds == 3600
    for role in ("translate", "review", "precompile_fix", "compile_fix"):
        assert (config.roles[role].model, config.roles[role].effort) == ("codex/gpt-6-astra", "low")
        assert config.roles[role].timeout_seconds is not None
        assert config.roles[role].max_turns is None


def test_template_roles_resolve_to_targets() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(CONFIG_TEMPLATE))
    for role in config.roles:
        target, detail = role_target(config, role)
        assert target is not None, detail
    assert role_target(config, "translate")[0] == Target("codex", "gpt-6-astra", "low")
    assert role_target(config, "survey_terms")[0] == Target("deepseek", "deepseek-flash", "low")


def test_template_fonts_match_defaults() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(CONFIG_TEMPLATE))
    assert config.fonts == FontsConfig()
    assert config.fonts.main == FontFamily(regular="LXGWWenKai-Light.ttf", bold="LXGWWenKai-Medium.ttf")
    assert config.fonts.sans == FontFamily(regular="SourceHanSansSC-Regular.otf", bold="SourceHanSansSC-Bold.otf")
    assert config.fonts.mono is None


def test_fonts_defaults_are_not_shared_between_instances() -> None:
    first, second = FontsConfig(), FontsConfig()
    assert first.main == second.main
    assert first.main is not second.main


def test_fonts_table_reads_strings_and_family_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(
        tmp_path,
        monkeypatch,
        TABLE + '\n[fonts]\nmain = "MyFont.ttf"\nsans = { regular = "~/fonts/Sans.otf", bold = "SansBold.otf" }\n'
        'mono = "Mono.ttc"\n',
    )
    assert config.fonts.main == "MyFont.ttf"
    assert config.fonts.sans == FontFamily(regular="~/fonts/Sans.otf", bold="SansBold.otf")
    assert config.fonts.mono == "Mono.ttc"


def test_fonts_table_family_without_bold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch, TABLE + '\n[fonts]\nmain = { regular = "MyFont.ttf" }\n')
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
    config = loaded(
        tmp_path,
        monkeypatch,
        TABLE + f'\n[fonts]\nmain = "~/fonts/MyFont.ttf"\nsans = {{ regular = "{sans}", bold = "{bold}" }}\n',
    )
    assert config.fonts.main == "~/fonts/MyFont.ttf"
    assert config.fonts.sans == FontFamily(regular=str(sans), bold=str(bold))


def test_fonts_table_defaults_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert loaded(tmp_path, monkeypatch).fonts == FontsConfig()


def test_chunking_defaults_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert loaded(tmp_path, monkeypatch).chunking == ChunkingConfig(chunk_tokens=5000, chunk_merge_tokens=1500)


def test_chunking_table_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch, TABLE + "\n[chunking]\nchunk_tokens = 9000\nchunk_merge_tokens = 100\n")
    assert config.chunking == ChunkingConfig(chunk_tokens=9000, chunk_merge_tokens=100)


@pytest.mark.parametrize(
    "limits", ["chunk_tokens = 0", "chunk_tokens = -1", "chunk_tokens = true", "chunk_merge_tokens = 6000"]
)
def test_invalid_chunk_limits_fail_config_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limits: str) -> None:
    write_config(tmp_path, monkeypatch, "[chunking]\n" + limits + "\n")
    config, detail = load_config()
    assert config is None
    assert "chunk" in detail


def test_runtime_table_fails_the_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE + '\n[runtime.codex]\ncommand = ["codex", "exec"]\n')
    config, detail = load_config()
    assert config is None
    assert "runtime" in detail


def test_old_role_fields_fail_the_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, '[roles]\ntranslate = { provider = "demo", model = "m", effort = "low" }\n')
    config, detail = load_config()
    assert config is None
    assert "provider" in detail


def test_provider_named_like_a_runtime_fails_the_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE + '\n[provider.pi]\nbase_url = "https://pi.example"\napi = "chat"\n')
    config, detail = load_config()
    assert config is None
    assert "pi" in detail
    assert "reserved for runtimes" in detail


def test_runtime_names() -> None:
    assert RUNTIME_NAMES == ("codex", "claude-code", "pi")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a/b/c", ("a", "b/c")),
        ("pi/deepseek/deepseek-flash", ("pi", "deepseek/deepseek-flash")),
        ("pi", ("pi", "")),
        ("codex", ("codex", "")),
        ("claude-code", ("claude-code", "")),
        (" codex/gpt ", ("codex", "gpt")),
        ("codex/", ("codex", "")),
    ],
)
def test_parse_target_accepts(text: str, expected: tuple[str, str]) -> None:
    assert parse_target(text) == (expected, "")


@pytest.mark.parametrize("text", ["deepseek", "deepseek/", "/x", "", "  "])
def test_parse_target_rejects(text: str) -> None:
    parsed, detail = parse_target(text)
    assert parsed is None
    assert "backend/model" in detail


def test_target_string_and_kind() -> None:
    assert str(Target("codex", "gpt-6-astra", "low")) == "codex/gpt-6-astra"
    assert str(Target("pi", "", None)) == "pi"
    assert Target("pi", "", None).is_runtime
    assert Target("claude-code", "opus", None).is_runtime
    assert not Target("demo", "chat-model", None).is_runtime


def test_role_target_reports_unknown_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    target, detail = role_target(config, "nobody")
    assert target is None
    assert "nobody" in detail
    assert "[roles]" in detail


def test_role_target_reports_unparsable_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    target, detail = role_target(config, "slashless")
    assert target is None
    assert "backend/model" in detail
    assert "got demo" in detail


def test_role_target_reports_undeclared_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    target, detail = role_target(config, "ghost")
    assert target is None
    assert "provider ghost" in detail
    assert "[provider.ghost]" in detail


def test_role_target_uses_the_role_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    assert role_target(config, "translate") == (Target("demo", "chat-model", "low"), "")
    assert role_target(config, "review") == (Target("claude-code", "sonnet", "high"), "")
    assert role_target(config, "compile_fix") == (Target("pi", "", None), "")
    assert role_target(config, "bare") == (Target("demo", "chat-model", None), "")


def test_role_target_effort_argument_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    assert role_target(config, "translate", "high") == (Target("demo", "chat-model", "high"), "")
    assert role_target(config, "bare", "low") == (Target("demo", "chat-model", "low"), "")


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


def test_load_config_reports_role_missing_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, '[roles]\ntranslate = { effort = "low" }\n')
    config, detail = load_config()
    assert config is None
    assert "model" in detail


def test_model_api_reads_models_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    assert model_api(config, "demo", "chat-model") == (Api.CHAT, "")


def test_model_api_falls_back_to_provider_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    assert model_api(config, "wide", "any-model") == (Api.MESSAGES, "")


def test_model_api_reports_unknown_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    api, detail = model_api(config, "demo", "other-model")
    assert api is None
    assert "other-model" in detail


def test_model_api_reports_unknown_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    api, detail = model_api(config, "ghost", "chat-model")
    assert api is None
    assert "ghost" in detail


def test_model_api_rejects_unknown_api_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch)
    api, detail = model_api(config, "odd", "any-model")
    assert api is None
    assert "grpc" in detail
