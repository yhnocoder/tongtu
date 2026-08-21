from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from tongtu.artifacts.fetch import FetchStatus
from tongtu.stages import fetch
from tongtu.workdir import Workdir, WorkdirError

BIG_TEX = ("\\documentclass{article}\n\\begin{document}\n" + "word " * 300 + "\n\\end{document}\n").encode()


def make_workdir(tmp_path: Path) -> Workdir:
    return Workdir(tmp_path / "paper")


def tar_payload(files: dict[str, bytes], mode: str = "w:gz") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def wire_download(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> list[str]:
    urls: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        urls.append(url)
        return httpx.Response(200, content=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(fetch.httpx, "get", get)
    return urls


def read_manifest(workdir: Workdir) -> dict:
    return json.loads(workdir.manifest_path("fetch").read_text(encoding="utf-8"))


def test_parse_paper_argument_recognizes_the_three_forms(tmp_path: Path) -> None:
    assert fetch.parse_paper_argument("2002.05202").arxiv_id == "2002.05202"
    assert fetch.parse_paper_argument("https://arxiv.org/abs/2002.05202").arxiv_id == "2002.05202"
    assert fetch.parse_paper_argument("https://arxiv.org/pdf/2002.05202.pdf").arxiv_id == "2002.05202"
    assert fetch.parse_paper_argument("https://arxiv.org/html/hep-th/9901001").arxiv_id == "hep-th/9901001"
    assert fetch.parse_paper_argument(str(tmp_path)).source_dir == tmp_path


def test_parse_paper_argument_rejects_bad_input() -> None:
    with pytest.raises(fetch.PaperArgumentError):
        fetch.parse_paper_argument("https://example.com/abs/2002.05202")
    with pytest.raises(fetch.PaperArgumentError):
        fetch.parse_paper_argument("https://arxiv.org/no-prefix/2002.05202")
    with pytest.raises(WorkdirError):
        fetch.parse_paper_argument("a b")
    with pytest.raises(WorkdirError):
        fetch.parse_paper_argument(".")


def test_remote_targz_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = tar_payload({"main.tex": BIG_TEX, "figs/plot.pdf": b"%PDF-1.5 fake"})
    urls = wire_download(monkeypatch, payload)
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
    assert manifest.status is FetchStatus.OK
    assert manifest.kind == "tar.gz"
    assert manifest.source == "2002.05202"
    assert manifest.url == urls[0] == "https://export.arxiv.org/e-print/2002.05202"
    assert manifest.payload_bytes == len(payload)
    assert manifest.tex_files == ["main.tex"]
    assert manifest.tex_chars == len(BIG_TEX)
    assert (workdir.src / "main.tex").read_bytes() == BIG_TEX
    assert (workdir.src / "figs" / "plot.pdf").is_file()
    assert (workdir.build / "e-print.bin").read_bytes() == payload
    assert read_manifest(workdir)["status"] == "ok"


def test_remote_pdf_only_leaves_no_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, b"%PDF-1.5 fake body")
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
    assert manifest.status is FetchStatus.PDF_ONLY
    assert manifest.kind == "pdf"
    assert list(workdir.src.iterdir()) == []
    assert (workdir.build / "e-print.bin").is_file()


def test_remote_download_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(fetch.httpx, "get", get)
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
    assert manifest.status is FetchStatus.DOWNLOAD_FAILED
    assert "ConnectError" in manifest.message
    assert list(workdir.src.iterdir()) == []
    assert not (workdir.build / "e-print.bin").exists()


def test_remote_http_error_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(404, content=b"not found", request=httpx.Request("GET", url))

    monkeypatch.setattr(fetch.httpx, "get", get)
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), make_workdir(tmp_path))
    assert manifest.status is FetchStatus.DOWNLOAD_FAILED


def test_remote_empty_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, b"")
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), make_workdir(tmp_path))
    assert manifest.status is FetchStatus.DOWNLOAD_FAILED
    assert manifest.message == "下载成功但响应体为空。"


def test_remote_gz_single_tex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, gzip.compress(BIG_TEX))
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
    assert manifest.status is FetchStatus.OK
    assert manifest.kind == "gz"
    assert (workdir.src / "main.tex").read_bytes() == BIG_TEX


def test_remote_bare_tex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, BIG_TEX)
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
    assert manifest.status is FetchStatus.OK
    assert manifest.kind == "tex"


def test_remote_unpack_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, b"\x1f\x8b broken gzip stream")
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
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
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
    assert manifest.status is FetchStatus.OK
    assert manifest.rejected == ["evil"]
    assert manifest.warnings
    assert not (workdir.src / "evil").exists()


def test_small_tex_is_a_pdf_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, tar_payload({"main.tex": b"\\documentclass{article}", "main.pdf": b"%PDF-1.5"}))
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
    assert manifest.status is FetchStatus.PDF_ONLY
    assert "1000" in manifest.message
    assert list(workdir.src.iterdir()) == []


def test_includepdf_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = ("\\documentclass{article}\n\\usepackage{pdfpages}\n\\includepdf{paper.pdf}\n" + "%" * 2000).encode()
    wire_download(monkeypatch, tar_payload({"main.tex": shell}))
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), make_workdir(tmp_path))
    assert manifest.status is FetchStatus.PDF_ONLY
    assert "includepdf" in manifest.message


def test_archive_without_tex_or_pdf_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_download(monkeypatch, tar_payload({"README": b"nothing here"}))
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
    assert manifest.status is FetchStatus.EMPTY
    assert list(workdir.src.iterdir()) == []


def test_local_ok(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / ".git" / "config").write_text("x", encoding="utf-8")
    (source / "main.tex").write_bytes(BIG_TEX)
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(fetch.PaperInput(source_dir=source), workdir)
    assert manifest.status is FetchStatus.OK
    assert manifest.kind == "local"
    assert manifest.source == str(source)
    assert (workdir.src / "main.tex").read_bytes() == BIG_TEX
    assert not (workdir.src / ".git").exists()


def test_local_missing_source(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    manifest = fetch.run(fetch.PaperInput(source_dir=tmp_path / "absent"), workdir)
    assert manifest.status is FetchStatus.SOURCE_MISSING
    assert manifest.kind == "local"
    assert list(workdir.src.iterdir()) == []


def test_local_source_overlapping_the_workdir_is_rejected(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    workdir.create()
    (workdir.src / "main.tex").write_bytes(BIG_TEX)
    manifest = fetch.run(fetch.PaperInput(source_dir=workdir.src), workdir)
    assert manifest.status is FetchStatus.UNPACK_FAILED
    assert (workdir.src / "main.tex").is_file()
    manifest = fetch.run(fetch.PaperInput(source_dir=workdir.path), workdir)
    assert manifest.status is FetchStatus.UNPACK_FAILED


def test_rerun_replaces_previous_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path)
    wire_download(monkeypatch, tar_payload({"old.tex": BIG_TEX}))
    fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
    wire_download(monkeypatch, tar_payload({"new.tex": BIG_TEX}))
    manifest = fetch.run(fetch.PaperInput(arxiv_id="2002.05202"), workdir)
    assert manifest.tex_files == ["new.tex"]
    assert not (workdir.src / "old.tex").exists()
