from __future__ import annotations

from pathlib import Path

import pytest

from tongtu.workdir import WorkdirError, normalize_arxiv_id, resolve


def test_resolve_without_env_lands_under_default_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TONGTU_HOME", raising=False)
    assert resolve("2002.05202") == Path("~/.tongtu/papers/2002.05202").expanduser()


def test_home_env_overrides_default_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    assert resolve("2002.05202") == tmp_path / "papers" / "2002.05202"


def test_workdir_argument_wins_over_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    assert resolve("2002.05202", tmp_path / "elsewhere") == tmp_path / "elsewhere"


def test_resolve_requires_id_or_workdir() -> None:
    with pytest.raises(WorkdirError):
        resolve()


def test_slash_in_the_id_becomes_underscore() -> None:
    assert normalize_arxiv_id("hep-th/9901001") == "hep-th_9901001"


@pytest.mark.parametrize("bad", ["", "  ", "a b", "../x", "~x", "/abs", ".hidden", "a\\b"])
def test_invalid_ids_are_rejected(bad: str) -> None:
    with pytest.raises(WorkdirError):
        normalize_arxiv_id(bad)
