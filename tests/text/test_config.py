from __future__ import annotations

from pathlib import Path

import pytest

from tongtu import config
from tongtu.config import config_path, glossary_path, home_dir, legacy_dirs_present, papers_dir


def test_home_dir_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TONGTU_HOME", raising=False)
    assert home_dir() == Path("~/.tongtu").expanduser()


def test_home_dir_follows_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    assert home_dir() == tmp_path / "home"


def test_home_dir_expands_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert home_dir({"TONGTU_HOME": "~/elsewhere"}) == tmp_path / "elsewhere"


def test_blank_env_counts_as_unset() -> None:
    assert home_dir({"TONGTU_HOME": "   "}) == Path("~/.tongtu").expanduser()


def test_files_live_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    assert config_path() == tmp_path / "home" / "config.toml"
    assert glossary_path() == tmp_path / "home" / "glossary.json"
    assert papers_dir() == tmp_path / "home" / "papers"


def test_legacy_dirs_present_lists_only_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = tmp_path / "config" / "tongtu"
    existing.mkdir(parents=True)
    monkeypatch.setattr(config, "LEGACY_DIRS", (existing, tmp_path / "share" / "tongtu"))
    assert legacy_dirs_present() == [existing]
