"""工作目录解析与四区布局（架构 §5）。"""

from pathlib import Path

import pytest

from tongtu import workdir as wd


def test_default_root_without_env():
    assert wd.default_root(env={}) == Path("~/.local/share/tongtu").expanduser()


def test_default_root_uses_tongtu_home(tmp_path):
    assert wd.default_root(env={"TONGTU_HOME": str(tmp_path)}) == tmp_path


def test_resolve_default_location():
    resolved = wd.resolve("2401.01234", env={})
    assert resolved == (Path("~/.local/share/tongtu").expanduser() / "2401.01234").absolute()


def test_resolve_prefers_tongtu_home(tmp_path):
    resolved = wd.resolve("2401.01234", env={"TONGTU_HOME": str(tmp_path)})
    assert resolved == (tmp_path / "2401.01234").absolute()


def test_workdir_flag_beats_env(tmp_path):
    explicit = tmp_path / "elsewhere"
    resolved = wd.resolve("2401.01234", workdir=explicit, env={"TONGTU_HOME": str(tmp_path / "home")})
    assert resolved == explicit.absolute()


def test_blank_tongtu_home_falls_back():
    assert wd.default_root(env={"TONGTU_HOME": "   "}) == Path("~/.local/share/tongtu").expanduser()


def test_old_style_id_stays_single_level(tmp_path):
    resolved = wd.resolve("hep-th/9901001", env={"TONGTU_HOME": str(tmp_path)})
    assert resolved == (tmp_path / "hep-th_9901001").absolute()
    assert resolved.parent == tmp_path


@pytest.mark.parametrize("bad", ["", "   ", "..", "/etc", "~/x", "../evil"])
def test_illegal_ids_rejected(bad):
    with pytest.raises(wd.WorkdirError):
        wd.resolve(bad, env={})


def test_resolve_needs_id_or_workdir():
    with pytest.raises(wd.WorkdirError):
        wd.resolve(env={})


def test_create_lays_out_four_areas(tmp_path):
    paper = wd.open_workdir("2401.01234", env={"TONGTU_HOME": str(tmp_path)}, create=True)
    assert paper.path == (tmp_path / "2401.01234").absolute()
    for area in wd.AREAS:
        assert (paper.path / area).is_dir(), f"缺少 {area}/"
    assert paper.manifests.is_dir()
    assert paper.manifests == paper.build / "manifests"
    assert (paper.src, paper.build, paper.out, paper.logs) == paper.areas


def test_open_workdir_without_create_touches_nothing(tmp_path):
    paper = wd.open_workdir("2401.01234", env={"TONGTU_HOME": str(tmp_path)})
    assert not paper.exists()
    assert list(tmp_path.iterdir()) == []


def test_create_is_idempotent(tmp_path):
    paper = wd.open_workdir("2401.01234", env={"TONGTU_HOME": str(tmp_path)}, create=True)
    (paper.src / "main.tex").write_text("hello", encoding="utf-8")
    paper.create()
    assert (paper.src / "main.tex").read_text(encoding="utf-8") == "hello"


def test_manifest_path(tmp_path):
    paper = wd.open_workdir(workdir=tmp_path / "paper")
    assert paper.manifest_path("mask") == tmp_path / "paper" / "build" / "manifests" / "mask.json"
    with pytest.raises(wd.WorkdirError):
        paper.manifest_path("../escape")
