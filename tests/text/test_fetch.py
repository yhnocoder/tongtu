from __future__ import annotations

import gzip
import io
import json
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tongtu.artifacts.fetch import FetchManifest, FetchStatus, PaperMeta
from tongtu.arxiv import PaperInput
from tongtu.stages import fetch
from tongtu.workdir import Workdir

from .test_arxiv import ERROR_FEED, VERSIONED_FEED

BIG_TEX = ("\\documentclass{article}\n\\begin{document}\n" + "word " * 300 + "\n\\end{document}\n").encode()


PAPER = PaperInput(arxiv_id="1412.6980", version=8)

API_URL = "https://export.arxiv.org/api/query?id_list=1412.6980v8"

EPRINT_URL = "https://export.arxiv.org/e-print/1412.6980v8"


def make_workdir(tmp_path: Path) -> Workdir:
    return Workdir(tmp_path / "1412.6980v8")


def tar_payload(files: dict[str, bytes], mode: str = "w:gz") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def wire_network(
    monkeypatch: pytest.MonkeyPatch,
    api: Callable[[str], httpx.Response],
    eprint: Callable[[str], httpx.Response],
) -> list[str]:
    urls: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        urls.append(url)
        return api(url) if url.startswith("https://export.arxiv.org/api/") else eprint(url)

    monkeypatch.setattr(httpx, "get", get)
    return urls


def respond(content: bytes, status_code: int = 200) -> Callable[[str], httpx.Response]:
    return lambda url: httpx.Response(status_code, content=content, request=httpx.Request("GET", url))


def wire_download(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> list[str]:
    return wire_network(monkeypatch, respond(VERSIONED_FEED.encode()), respond(payload))


def read_meta(workdir: Workdir) -> PaperMeta:
    return PaperMeta.model_validate_json(workdir.meta.read_text(encoding="utf-8"))


def read_manifest(workdir: Workdir) -> dict:
    return json.loads(workdir.manifest_path("fetch").read_text(encoding="utf-8"))


def test_remote_targz_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = tar_payload({"main.tex": BIG_TEX, "figs/plot.pdf": b"%PDF-1.5 fake"})
    urls = wire_download(monkeypatch, payload)
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.OK
    assert manifest.kind == "tar.gz"
    assert (manifest.source, manifest.arxiv_id, manifest.version) == ("1412.6980v8", "1412.6980", 8)
    assert urls == [API_URL, EPRINT_URL]
    assert manifest.url == EPRINT_URL
    assert manifest.payload_bytes == len(payload)
    assert manifest.tex_files == ["main.tex"]
    assert manifest.tex_chars == len(BIG_TEX)
    assert (workdir.src / "main.tex").read_bytes() == BIG_TEX
    assert (workdir.src / "figs" / "plot.pdf").is_file()
    assert (workdir.build / "e-print.bin").read_bytes() == payload
    assert read_manifest(workdir)["status"] == "ok"
    assert workdir.meta == workdir.out / "1412.6980v8.meta.json"
    meta = read_meta(workdir)
    assert (meta.arxiv_id, meta.version, meta.title) == ("1412.6980", 8, "Adam: A Method for Stochastic Optimization")


def test_given_meta_is_written_without_asking_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_download(monkeypatch, tar_payload({"main.tex": BIG_TEX}))
    meta = PaperMeta(
        arxiv_id="1412.6980",
        version=8,
        title="Adam",
        authors=["Diederik Kingma", "Jimmy Ba"],
        abstract="We introduce Adam.",
        primary_category="cs.LG",
        categories=["cs.LG"],
        published=datetime(2014, 12, 22, 13, 54, 29, tzinfo=UTC),
        updated=datetime(2015, 7, 23, 20, 27, 47, tzinfo=UTC),
        comment=None,
        journal_ref=None,
        doi=None,
        abs_url="https://arxiv.org/abs/1412.6980v8",
        pdf_url="https://arxiv.org/pdf/1412.6980v8",
    )
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir, meta)
    assert manifest.status is FetchStatus.OK
    assert urls == [EPRINT_URL]
    assert read_meta(workdir) == meta


@pytest.mark.parametrize(
    "api",
    [respond(ERROR_FEED.encode()), respond(b"server error", 503)],
    ids=["error-entry", "http-error"],
)
def test_failed_api_request_is_download_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: Callable[[str], httpx.Response]
) -> None:
    urls = wire_network(monkeypatch, api, respond(tar_payload({"main.tex": BIG_TEX})))
    workdir = make_workdir(tmp_path)
    workdir.create()
    workdir.meta.write_text("stale", encoding="utf-8")
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.DOWNLOAD_FAILED
    assert "arXiv API" in manifest.message
    assert (manifest.source, manifest.arxiv_id, manifest.version) == ("1412.6980v8", "1412.6980", 8)
    assert urls == [API_URL]
    assert not workdir.meta.exists()
    assert list(workdir.src.iterdir()) == []
    assert FetchManifest.model_validate(read_manifest(workdir)).status is FetchStatus.DOWNLOAD_FAILED


def test_remote_pdf_only_leaves_no_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, b"%PDF-1.5 fake body")
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.PDF_ONLY
    assert manifest.kind == "pdf"
    assert manifest.message.startswith("1412.6980v8 has no TeX source")
    assert "Other versions of 1412.6980 may have TeX source" in manifest.message
    assert "earlier" not in manifest.message
    assert list(workdir.src.iterdir()) == []
    assert (workdir.build / "e-print.bin").is_file()
    assert read_meta(workdir).version == 8


def test_remote_download_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(url: str) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    wire_network(monkeypatch, respond(VERSIONED_FEED.encode()), refuse)
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.DOWNLOAD_FAILED
    assert "ConnectError" in manifest.message
    assert list(workdir.src.iterdir()) == []
    assert not (workdir.build / "e-print.bin").exists()
    assert read_meta(workdir).version == 8


def test_remote_http_error_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_network(monkeypatch, respond(VERSIONED_FEED.encode()), respond(b"not found", 404))
    manifest = fetch.run(PAPER, make_workdir(tmp_path))
    assert manifest.status is FetchStatus.DOWNLOAD_FAILED


def test_remote_paper_without_a_version_is_a_program_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        fetch.run(PaperInput(arxiv_id="1412.6980"), make_workdir(tmp_path))


def test_remote_empty_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, b"")
    manifest = fetch.run(PAPER, make_workdir(tmp_path))
    assert manifest.status is FetchStatus.DOWNLOAD_FAILED
    assert manifest.message == "download succeeded but the response body is empty."


def test_remote_gz_single_tex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, gzip.compress(BIG_TEX))
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.OK
    assert manifest.kind == "gz"
    assert (workdir.src / "main.tex").read_bytes() == BIG_TEX


def test_remote_bare_tex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, BIG_TEX)
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.OK
    assert manifest.kind == "tex"


def test_remote_unpack_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, b"\x1f\x8b broken gzip stream")
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.UNPACK_FAILED
    assert manifest.message
    assert list(workdir.src.iterdir()) == []
    assert (workdir.build / "e-print.bin").is_file()


def test_remote_tar_rejects_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("main.tex")
        info.size = len(BIG_TEX)
        archive.addfile(info, io.BytesIO(BIG_TEX))
        link = tarfile.TarInfo("evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    wire_download(monkeypatch, buffer.getvalue())
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.OK
    assert manifest.rejected == ["evil"]
    assert manifest.warnings
    assert not (workdir.src / "evil").exists()


def test_small_tex_is_a_pdf_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, tar_payload({"main.tex": b"\\documentclass{article}", "main.pdf": b"%PDF-1.5"}))
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.PDF_ONLY
    assert "1000" in manifest.message
    assert list(workdir.src.iterdir()) == []


def test_includepdf_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = ("\\documentclass{article}\n\\usepackage{pdfpages}\n\\includepdf{paper.pdf}\n" + "%" * 2000).encode()
    wire_download(monkeypatch, tar_payload({"main.tex": shell}))
    manifest = fetch.run(PAPER, make_workdir(tmp_path))
    assert manifest.status is FetchStatus.PDF_ONLY
    assert "includepdf" in manifest.message


def test_archive_without_tex_or_pdf_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, tar_payload({"README": b"nothing here"}))
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.EMPTY
    assert list(workdir.src.iterdir()) == []


def test_local_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / ".git" / "config").write_text("x", encoding="utf-8")
    (source / "main.tex").write_bytes(BIG_TEX)
    urls = wire_download(monkeypatch, b"")
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PaperInput(source_dir=source), workdir)
    assert urls == []
    assert manifest.status is FetchStatus.OK
    assert manifest.kind == "local"
    assert manifest.source == str(source)
    assert (manifest.arxiv_id, manifest.version) == (None, None)
    assert list(workdir.out.iterdir()) == []
    assert (workdir.src / "main.tex").read_bytes() == BIG_TEX
    assert not (workdir.src / ".git").exists()


def test_local_missing_source(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PaperInput(source_dir=tmp_path / "absent"), workdir)
    assert manifest.status is FetchStatus.SOURCE_MISSING
    assert manifest.kind == "local"
    assert list(workdir.src.iterdir()) == []


def test_local_source_overlapping_the_workdir_is_rejected(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    workdir.create()
    (workdir.src / "main.tex").write_bytes(BIG_TEX)
    manifest = fetch.run(PaperInput(source_dir=workdir.src), workdir)
    assert manifest.status is FetchStatus.UNPACK_FAILED
    assert (workdir.src / "main.tex").is_file()
    manifest = fetch.run(PaperInput(source_dir=workdir.path), workdir)
    assert manifest.status is FetchStatus.UNPACK_FAILED


def test_rerun_replaces_previous_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_download(monkeypatch, tar_payload({"old.tex": BIG_TEX}))
    fetch.run(PAPER, workdir)
    wire_download(monkeypatch, tar_payload({"new.tex": BIG_TEX}))
    manifest = fetch.run(PAPER, workdir)
    assert manifest.tex_files == ["new.tex"]
    assert not (workdir.src / "old.tex").exists()
    assert read_meta(workdir).version == 8
