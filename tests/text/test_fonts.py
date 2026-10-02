from __future__ import annotations

from pathlib import Path

import pytest

from tongtu import fonts
from tongtu.model.config import FontsConfig


def test_configured_falls_back_to_defaults_without_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    assert fonts.configured() == FontsConfig()


def test_configured_reads_the_fonts_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[fonts]\nmain = "MyFont.ttf"\nmono = "Mono.otf"\n', encoding="utf-8")
    assert fonts.configured() == FontsConfig(main="MyFont.ttf", mono="Mono.otf")


def test_search_directories_default_is_the_bundled_directory() -> None:
    assert fonts.FONTS_DIR.is_dir()
    assert fonts.search_directories(FontsConfig()) == [fonts.FONTS_DIR]


def test_search_directories_appends_the_parent_of_a_font_path(tmp_path: Path) -> None:
    font = tmp_path / "custom" / "MyFont.otf"
    font.parent.mkdir()
    font.write_bytes(b"font-bytes")
    assert fonts.search_directories(FontsConfig(main=str(font))) == [fonts.FONTS_DIR, font.parent]


def test_search_directories_expands_the_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "fonts").mkdir()
    config = FontsConfig(bold="~/fonts/Bold.ttf")
    assert fonts.search_directories(config) == [fonts.FONTS_DIR, tmp_path / "fonts"]


def test_search_directories_lists_a_shared_directory_once(tmp_path: Path) -> None:
    custom = tmp_path / "custom"
    custom.mkdir()
    config = FontsConfig(main=str(custom / "Main.otf"), sans=str(custom / "Sans.otf"), mono="Mono.ttf")
    assert fonts.search_directories(config) == [fonts.FONTS_DIR, custom]


def test_search_directories_drops_missing_directories(tmp_path: Path) -> None:
    config = FontsConfig(sans_bold=str(tmp_path / "missing" / "Bold.otf"))
    assert fonts.search_directories(config) == [fonts.FONTS_DIR]


def test_environment_sets_both_variables_from_the_search_path() -> None:
    environment = fonts.environment(FontsConfig(), {"PATH": "/usr/bin"})
    assert environment["PATH"] == "/usr/bin"
    assert environment["TTFONTS"] == f"{fonts.FONTS_DIR}//:"
    assert environment["OPENTYPEFONTS"] == environment["TTFONTS"]
    assert set(environment) == {"PATH", "TTFONTS", "OPENTYPEFONTS"}


def test_environment_prepends_to_existing_variables(tmp_path: Path) -> None:
    custom = tmp_path / "custom"
    custom.mkdir()
    config = FontsConfig(main=str(custom / "Main.ttf"))
    base = {"TTFONTS": "/x//:", "OPENTYPEFONTS": "/y//:"}
    environment = fonts.environment(config, base)
    assert environment["TTFONTS"] == f"{fonts.FONTS_DIR}//:{custom}//:/x//:"
    assert environment["OPENTYPEFONTS"] == f"{fonts.FONTS_DIR}//:{custom}//:/y//:"
    assert base == {"TTFONTS": "/x//:", "OPENTYPEFONTS": "/y//:"}
