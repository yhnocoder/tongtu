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

from tongtu import arxiv
from tongtu.artifacts.fetch import FetchManifest, FetchStatus, PaperMeta
from tongtu.arxiv import PaperInput
from tongtu.stages import fetch
from tongtu.workdir import Workdir

from .test_arxiv import ERROR_FEED, VERSIONED_FEED

BIG_TEX = ("\\documentclass{article}\n\\begin{document}\n" + "word " * 300 + "\n\\end{document}\n").encode()


PAPER = PaperInput(arxiv_id="1412.6980", version=8)

API_URL = "https://export.arxiv.org/api/query?id_list=1412.6980v8"

EPRINT_URL = "https://export.arxiv.org/e-print/1412.6980v8"

PDF_URL = "https://export.arxiv.org/pdf/1412.6980v8"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))


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


def test_fetch_reads_the_paper_info_from_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_download(monkeypatch, tar_payload({"main.tex": BIG_TEX}))
    first = fetch.run(PAPER, make_workdir(tmp_path))
    assert first.status is FetchStatus.OK
    assert (tmp_path / "home" / "cache" / "arxiv" / "1412.6980v8.json").is_file()
    urls.clear()
    second_workdir = Workdir(tmp_path / "again")
    assert fetch.run(PAPER, second_workdir).status is FetchStatus.OK
    assert urls == [EPRINT_URL]
    assert read_meta(second_workdir).title == "Adam: A Method for Stochastic Optimization"


def test_fetch_writes_the_saved_abstract_zh_into_the_meta_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, tar_payload({"main.tex": BIG_TEX}))
    meta = arxiv.parse_feed(VERSIONED_FEED, "1412.6980v8")
    found = arxiv.FoundMeta(meta, arxiv.MetaSource.ARXIV, datetime.now(UTC))
    arxiv.save_abstract_zh(found, "我们提出 Adam。")
    workdir = make_workdir(tmp_path)
    assert fetch.run(PAPER, workdir).status is FetchStatus.OK
    assert read_meta(workdir).abstract_zh == "我们提出 Adam。"


@pytest.mark.parametrize(
    "api",
    [respond(ERROR_FEED.encode()), respond(b"server error", 500)],
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


def test_remote_pdf_eprint_takes_the_pdf_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_download(monkeypatch, b"%PDF-1.5 fake body")
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.OK
    assert manifest.kind == "pdf"
    assert manifest.route == "pdf"
    assert manifest.message == ""
    assert urls == [API_URL, EPRINT_URL]
    assert list(workdir.src.iterdir()) == []
    assert workdir.original_pdf.read_bytes() == b"%PDF-1.5 fake body"
    assert (workdir.build / "e-print.bin").is_file()
    assert read_meta(workdir).version == 8


def test_gzipped_pdf_eprint_takes_the_pdf_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_download(monkeypatch, gzip.compress(b"%PDF-1.5 zipped"))
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert (manifest.status, manifest.kind, manifest.route) == (FetchStatus.OK, "gz", "pdf")
    assert urls == [API_URL, EPRINT_URL]
    assert workdir.original_pdf.read_bytes() == b"%PDF-1.5 zipped"
    assert list(workdir.src.iterdir()) == []


def wire_pdf_download(
    monkeypatch: pytest.MonkeyPatch, eprint: bytes, pdf: Callable[[str], httpx.Response]
) -> list[str]:
    def source(url: str) -> httpx.Response:
        return respond(eprint)(url) if url == EPRINT_URL else pdf(url)

    return wire_network(monkeypatch, respond(VERSIONED_FEED.encode()), source)


def test_tex_shell_downloads_the_arxiv_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = tar_payload({"main.tex": b"\\documentclass{article}", "main.pdf": b"%PDF-1.5 figure"})
    urls = wire_pdf_download(monkeypatch, shell, respond(b"%PDF-1.7 paper"))
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert (manifest.status, manifest.kind, manifest.route) == (FetchStatus.OK, "tar.gz", "pdf")
    assert urls == [API_URL, EPRINT_URL, PDF_URL]
    assert any("1000" in warning for warning in manifest.warnings)
    assert workdir.original_pdf.read_bytes() == b"%PDF-1.7 paper"
    assert list(workdir.src.iterdir()) == []


def test_failed_pdf_download_is_download_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = tar_payload({"main.tex": b"\\documentclass{article}"})
    wire_pdf_download(monkeypatch, shell, respond(b"busy", status_code=503))
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert manifest.status is FetchStatus.DOWNLOAD_FAILED
    assert manifest.route == "pdf"
    assert "PDF download failed" in manifest.message
    assert not workdir.original_pdf.exists()
    assert list(workdir.src.iterdir()) == []


def test_pdf_download_that_is_not_a_pdf_is_download_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = tar_payload({"main.tex": b"\\documentclass{article}"})
    wire_pdf_download(monkeypatch, shell, respond(b"<html>captcha</html>"))
    manifest = fetch.run(PAPER, make_workdir(tmp_path))
    assert manifest.status is FetchStatus.DOWNLOAD_FAILED
    assert "did not return a PDF" in manifest.message


def test_local_pdf_file_takes_the_pdf_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4 local")
    urls = wire_download(monkeypatch, b"")
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PaperInput(source_pdf=source), workdir)
    assert urls == []
    assert (manifest.status, manifest.kind, manifest.route) == (FetchStatus.OK, "local_pdf", "pdf")
    assert manifest.source == str(source)
    assert (manifest.arxiv_id, manifest.version) == (None, None)
    assert workdir.original_pdf.read_bytes() == b"%PDF-1.4 local"
    assert list(workdir.out.iterdir()) == []
    assert list(workdir.src.iterdir()) == []


def test_local_pdf_file_that_vanished_is_source_missing(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PaperInput(source_pdf=tmp_path / "gone.pdf"), workdir)
    assert manifest.status is FetchStatus.SOURCE_MISSING
    assert manifest.kind == "local_pdf"
    assert not workdir.original_pdf.exists()


def test_local_directory_without_tex_source_is_empty(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"%PDF-1.4")
    manifest = fetch.run(PaperInput(source_dir=source), make_workdir(tmp_path))
    assert manifest.status is FetchStatus.EMPTY
    assert manifest.route == "tex"
    assert "pass the PDF file as PAPER" in manifest.message


def test_old_manifest_without_route_reads_as_tex() -> None:
    manifest = FetchManifest.model_validate_json('{"status": "ok", "source": "x", "kind": "tar.gz"}')
    assert manifest.route == "tex"


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
    assert manifest.status is FetchStatus.EMPTY
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
    shell = tar_payload({"main.tex": b"\\documentclass{article}", "main.pdf": b"%PDF-1.5"})
    wire_pdf_download(monkeypatch, shell, respond(b"%PDF-1.7 paper"))
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(PAPER, workdir)
    assert (manifest.status, manifest.route) == (FetchStatus.OK, "pdf")
    assert any("1000" in warning for warning in manifest.warnings)
    assert list(workdir.src.iterdir()) == []


def test_includepdf_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = ("\\documentclass{article}\n\\usepackage{pdfpages}\n\\includepdf{paper.pdf}\n" + "%" * 2000).encode()
    wire_pdf_download(monkeypatch, tar_payload({"main.tex": shell}), respond(b"%PDF-1.7 paper"))
    manifest = fetch.run(PAPER, make_workdir(tmp_path))
    assert (manifest.status, manifest.route) == (FetchStatus.OK, "pdf")
    assert any("includepdf" in warning for warning in manifest.warnings)


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
