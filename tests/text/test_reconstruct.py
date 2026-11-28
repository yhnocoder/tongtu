from __future__ import annotations

from pathlib import Path

import httpx
import pymupdf
import pytest

from tongtu.artifacts.fetch import FetchManifest, FetchStatus
from tongtu.artifacts.reconstruct import ReconstructManifest, ReconstructStatus
from tongtu.manifests import load_manifest, write_manifest
from tongtu.reconstruct import omlx
from tongtu.stages import reconstruct
from tongtu.workdir import Workdir


def make_workdir(tmp_path: Path, route: str) -> Workdir:
    workdir = Workdir(tmp_path / "paper")
    workdir.create()
    kind = "local_pdf" if route == "pdf" else "local"
    manifest = FetchManifest(status=FetchStatus.OK, source="paper", kind=kind, route=route)
    write_manifest(workdir.manifest_path("fetch"), manifest)
    return workdir


def no_session(*args: object, **kwargs: object) -> None:
    raise AssertionError("reconstruct started an agent session")


def read_manifest(workdir: Workdir) -> ReconstructManifest:
    manifest = load_manifest(workdir.manifest_path(reconstruct.STAGE_NAME), ReconstructManifest)
    assert manifest is not None
    return manifest


def test_tex_route_is_ok_without_a_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tongtu.model.work", no_session)
    workdir = make_workdir(tmp_path, "tex")
    (workdir.src / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.OK
    assert manifest.rebuilt is False
    assert (manifest.rule_check, manifest.final_check, manifest.fix_session) == (None, None, None)
    assert read_manifest(workdir) == manifest
    assert (workdir.src / "main.tex").is_file()
    assert not workdir.sandbox(reconstruct.STAGE_NAME).exists()


def test_pdf_without_words_is_no_text_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tongtu.model.work", no_session)
    workdir = make_workdir(tmp_path, "pdf")
    blank = pymupdf.open()
    blank.new_page()
    blank.save(workdir.original_pdf)
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.NO_TEXT_LAYER
    assert manifest.rebuilt is True
    assert "scanned PDFs are not supported" in manifest.message
    assert read_manifest(workdir).status is ReconstructStatus.NO_TEXT_LAYER
    assert not (workdir.src / "main.tex").exists()


def test_file_that_is_not_a_pdf_is_no_text_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tongtu.model.work", no_session)
    workdir = make_workdir(tmp_path, "pdf")
    workdir.original_pdf.write_bytes(b"not a pdf at all")
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.NO_TEXT_LAYER
    assert "cannot open" in manifest.message


def test_missing_original_pdf_is_no_text_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tongtu.model.work", no_session)
    workdir = make_workdir(tmp_path, "pdf")
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.NO_TEXT_LAYER
    assert str(workdir.original_pdf) in manifest.message


def enable_omlx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[omlx]\nenabled = true\nbase_url = "http://127.0.0.1:11000/v1"\n')
    monkeypatch.setenv("TONGTU_HOME", str(home))
    requested: list[str] = []

    def refuse(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(omlx.httpx, "get", refuse)
    return requested


def test_unreachable_omlx_is_omlx_unavailable_before_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tongtu.model.work", no_session)
    requested = enable_omlx(tmp_path, monkeypatch)
    workdir = make_workdir(tmp_path, "pdf")
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Sequence transduction models translate sentences between languages.")
    document.save(workdir.original_pdf)
    manifest = reconstruct.run(workdir, mode="fast")
    assert manifest.status is ReconstructStatus.OMLX_UNAVAILABLE
    assert manifest.rebuilt is True
    assert manifest.message.startswith("cannot reach oMLX at http://127.0.0.1:11000/v1 (ConnectError).")
    assert "  omlx start\n" in manifest.message
    assert manifest.message.endswith("  omlx serve --port 11000")
    assert requested == ["http://127.0.0.1:11000/v1/models"]
    assert read_manifest(workdir) == manifest
    assert not manifest.ok
    assert not (workdir.sandbox(reconstruct.STAGE_NAME) / "main.tex").exists()
    assert not (workdir.src / "main.tex").exists()


def test_tex_route_does_not_check_omlx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tongtu.model.work", no_session)
    requested = enable_omlx(tmp_path, monkeypatch)
    workdir = make_workdir(tmp_path, "tex")
    (workdir.src / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")
    assert reconstruct.run(workdir, mode="fast").status is ReconstructStatus.OK
    assert requested == []
