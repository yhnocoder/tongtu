from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tongtu.config import config_path
from tongtu.model.config import (
    CONFIG_TEMPLATE,
    DEFAULT_TIMEOUT_SECONDS,
    RUNTIME_NAMES,
    FontFamily,
    FontsConfig,
    ModelsConfig,
    ModeName,
    Target,
    load_config,
    mode_target,
)

TABLE = """
[modes.fast]
model = "claude-code/sonnet"
effort = "low"

[modes.smart]
model = "codex/gpt-6-astra"
timeout_seconds = 60.5
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


def failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> str:
    write_config(tmp_path, monkeypatch, text)
    config, detail = load_config()
    assert config is None
    return detail


def test_template_parses_and_validates() -> None:
    config = ModelsConfig.model_validate(tomllib.loads(CONFIG_TEMPLATE))
    assert config.modes.default is ModeName.SMART
    assert mode_target(config, "fast") == (Target("claude-code", "claude-sonnet-5-5", "low", 1800), "")
    assert mode_target(config, "smart") == (Target("claude-code", "claude-opus-5-5", "medium", 1800), "")


def test_modes_default_is_smart_when_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert loaded(tmp_path, monkeypatch).modes.default is ModeName.SMART


def test_modes_default_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch, '[modes]\ndefault = "fast"\n' + TABLE)
    assert config.modes.default is ModeName.FAST


def test_modes_default_must_name_a_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "modes.default" in failed(tmp_path, monkeypatch, '[modes]\ndefault = "turbo"\n' + TABLE)


@pytest.mark.parametrize("mode", ["fast", "smart"])
def test_each_mode_must_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    kept = "smart" if mode == "fast" else "fast"
    detail = failed(tmp_path, monkeypatch, f'[modes.{kept}]\nmodel = "claude-code/m"\n')
    assert f"modes.{mode}" in detail


def test_a_config_without_modes_fails_the_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "modes" in failed(tmp_path, monkeypatch, "[pdf]\ncrop_margins = true\n")


def test_a_mode_needs_a_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    detail = failed(tmp_path, monkeypatch, TABLE.replace('model = "claude-code/sonnet"\n', ""))
    assert "modes.fast.model" in detail


def test_mode_target_splits_at_the_first_slash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch, TABLE.replace("claude-code/sonnet", "pi/provider/model"))
    assert mode_target(config, "fast") == (Target("pi", "provider/model", "low", DEFAULT_TIMEOUT_SECONDS), "")


def test_mode_target_without_effort_and_with_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target, _detail = mode_target(loaded(tmp_path, monkeypatch), "smart")
    assert target == Target("codex", "gpt-6-astra", None, 60.5)
    assert str(target) == "codex/gpt-6-astra"


def test_a_bare_runtime_uses_its_default_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = loaded(tmp_path, monkeypatch, TABLE.replace("codex/gpt-6-astra", "pi"))
    target, _detail = mode_target(config, "smart")
    assert target == Target("pi", "", None, 60.5)
    assert str(target) == "pi"


@pytest.mark.parametrize("model", ["deepseek/deepseek-flash", "ghost", "/m", ""])
def test_a_model_must_name_a_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    detail = failed(tmp_path, monkeypatch, TABLE.replace("claude-code/sonnet", model))
    assert "modes.fast.model" in detail
    assert "codex, claude-code, pi" in detail


def test_runtime_names() -> None:
    assert RUNTIME_NAMES == ("codex", "claude-code", "pi")


def test_mode_target_reports_an_unknown_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target, detail = mode_target(loaded(tmp_path, monkeypatch), "turbo")
    assert target is None
    assert "turbo" in detail


@pytest.mark.parametrize("field", ["timeout_seconds = 0", "timeout_seconds = -5"])
def test_mode_timeout_must_be_positive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    assert "timeout_seconds" in failed(tmp_path, monkeypatch, TABLE.replace("timeout_seconds = 60.5", field))


@pytest.mark.parametrize(
    ("table", "extra"),
    [
        ("roles", '[roles]\ntranslate = { model = "codex/m" }\n'),
        ("provider", '[provider.demo]\nbase_url = "https://demo.example"\n'),
        ("chunking", "[chunking]\nchunk_tokens = 9000\n"),
    ],
)
def test_the_old_tables_fail_the_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str, extra: str) -> None:
    assert table in failed(tmp_path, monkeypatch, TABLE + "\n" + extra)


def test_an_unknown_mode_field_fails_the_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "jobs" in failed(tmp_path, monkeypatch, TABLE + "jobs = 4\n")


def test_an_unknown_mode_fails_the_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "modes.turbo" in failed(tmp_path, monkeypatch, TABLE + '\n[modes.turbo]\nmodel = "pi"\n')


def test_config_path_follows_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    assert config_path() == tmp_path / "config.toml"


def test_load_config_reports_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    config, detail = load_config()
    assert config is None
    assert "tongtu setup" in detail


def test_load_config_reports_broken_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "TOML" in failed(tmp_path, monkeypatch, "[modes.fast\n")


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


def test_crop_margins_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert loaded(tmp_path, monkeypatch).pdf.crop_margins is False
    assert ModelsConfig.model_validate(tomllib.loads(CONFIG_TEMPLATE)).pdf.crop_margins is False


def test_unknown_pdf_field_fails_the_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, TABLE + "\n[pdf]\ncrop = true\n")
    config, detail = load_config()
    assert config is None
    assert "crop" in detail


def test_crop_margins_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert loaded(tmp_path, monkeypatch, TABLE + "\n[pdf]\ncrop_margins = true\n").pdf.crop_margins is True


@pytest.mark.parametrize("value", ['"yes"', "1"])
def test_crop_margins_must_be_a_boolean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    write_config(tmp_path, monkeypatch, TABLE + "\n[pdf]\ncrop_margins = " + value + "\n")
    config, detail = load_config()
    assert config is None
    assert "crop_margins" in detail
