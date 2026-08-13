"""fetch 阶段：e-print 下载解包 + PDF-only / pdfpages 套壳检测（架构 §3 fetch 行）。

网络一律不打：`fetch` 的下载原语是注入的（`fetcher: Callable[[str], bytes]`），本文件
构造各种形态的 payload 直接喂进去。所有落盘都在 pytest 的 `tmp_path` 下——论文工作目录
永远不在仓库里（CLAUDE.md 纪律）。
"""

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from tongtu.stages import fetch as ft
from tongtu.workdir import Workdir

DATA = Path(__file__).parent / "data" / "fetch"


def fixture(name: str) -> str:
    return (DATA / f"{name}.tex").read_text(encoding="utf-8")


PAPER = fixture("paper")
SHELL = fixture("shell")
TINY = fixture("tiny")


@pytest.fixture
def paper(tmp_path) -> Workdir:
    """一个干净的论文工作目录（仓库外，随 tmp_path 消失）。"""
    return Workdir(path=tmp_path / "work" / "2401.01234", arxiv_id="2401.01234")


def tar_bytes(entries, *, compress=True, extra=()) -> bytes:
    """把 {名字: 内容} 打成 tar(.gz)。`extra` 是直接塞进去的 TarInfo（构造恶意成员用）。"""
    buffer = io.BytesIO()
    mode = "w:gz" if compress else "w"
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, content in entries.items():
            payload = content.encode("utf-8") if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        for info in extra:
            archive.addfile(info)
    return buffer.getvalue()


def fetcher_of(payload: bytes):
    """记录被请求的 URL 的假下载器。"""
    seen: list[str] = []

    def _fetch(url: str) -> bytes:
        seen.append(url)
        return payload

    _fetch.seen = seen  # type: ignore[attr-defined]
    return _fetch


def run(paper: Workdir, payload: bytes, arxiv_id: str = "2401.01234") -> ft.FetchResult:
    return ft.fetch(arxiv_id, paper, fetcher=fetcher_of(payload))


# --------------------------------------------------------------------------- #
# 形态分流
# --------------------------------------------------------------------------- #


def test_targz_multi_file_with_subdir(paper):
    payload = tar_bytes(
        {
            "main.tex": PAPER,
            "sections/method.tex": "\\section{Method}\nDetails.\n",
            "figures/plot.pdf": b"%PDF-1.5 fake",
            "refs.bib": "@article{x, title={T}}",
        }
    )
    result = run(paper, payload)

    assert result.status == ft.OK and result.ok
    assert result.kind == "tar.gz"
    assert result.files == ("figures/plot.pdf", "main.tex", "refs.bib", "sections/method.tex")
    assert result.tex_files == ("main.tex", "sections/method.tex")
    assert (paper.src / "sections" / "method.tex").read_text(encoding="utf-8").startswith("\\section")
    assert (paper.src / "main.tex").read_text(encoding="utf-8") == PAPER
    assert result.rejected == () and result.warnings == ()
    # 原始下载体落 build/（可丢弃），不污染 src/
    assert (paper.build / ft.RAW_NAME).read_bytes() == payload
    assert result.raw_path == paper.build / ft.RAW_NAME


def test_plain_tar_without_gzip(paper):
    result = run(paper, tar_bytes({"ms.tex": PAPER}, compress=False))
    assert result.status == ft.OK
    assert result.kind == "tar"
    assert result.files == ("ms.tex",)


def test_single_file_gz(paper):
    result = run(paper, gzip.compress(PAPER.encode("utf-8")))
    assert result.status == ft.OK
    assert result.kind == "gz"
    assert (paper.src / ft.SINGLE_NAME).read_text(encoding="utf-8") == PAPER


def test_bare_tex(paper):
    result = run(paper, PAPER.encode("utf-8"))
    assert result.status == ft.OK
    assert result.kind == "tex"
    assert (paper.src / ft.SINGLE_NAME).read_text(encoding="utf-8") == PAPER


def test_pdf_only_magic(paper):
    result = run(paper, b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n rest of a real pdf")

    assert result.status == ft.PDF_ONLY
    assert result.fallback is True and result.ok is False
    assert result.kind == "pdf"
    assert "降级路线" in result.message
    assert list(paper.src.iterdir()) == []  # 什么都没往 src/ 写


def test_gzipped_pdf_is_pdf_only(paper):
    result = run(paper, gzip.compress(b"%PDF-1.4\nbinary"))
    assert result.status == ft.PDF_ONLY
    assert list(paper.src.iterdir()) == []


def test_pdfpages_shell_is_pdf_only(paper):
    payload = tar_bytes({"main.tex": SHELL, "camera-ready.pdf": b"%PDF-1.4 fake"})
    result = run(paper, payload)

    assert result.status == ft.PDF_ONLY
    assert "pdfpages" in result.message
    assert ft.SHELL_MIN_CHARS < result.tex_chars < ft.SHELL_INCLUDEPDF_CHARS


def test_no_substance_is_pdf_only(paper):
    result = run(paper, TINY.encode("utf-8"))
    assert result.status == ft.PDF_ONLY
    assert "无实质内容" in result.message


def test_tree_without_tex_but_with_pdf(paper):
    result = run(paper, tar_bytes({"paper.pdf": b"%PDF-1.4 fake", "notes.txt": "hi"}))
    assert result.status == ft.PDF_ONLY
    assert result.tex_files == ()


def test_tree_without_tex_or_pdf_is_empty(paper):
    result = run(paper, tar_bytes({"readme.txt": "nothing here"}))
    assert result.status == ft.EMPTY
    assert result.fallback is False


# --------------------------------------------------------------------------- #
# 解包安全（Python 3.12 filter="data" 语义的自行兜底）
# --------------------------------------------------------------------------- #


def test_path_traversal_members_are_rejected(paper, tmp_path):
    payload = tar_bytes(
        {
            "../evil.tex": "\\documentclass{article}",
            "../../evil2.tex": "escaped",
            "/tmp/absolute.tex": "escaped",
            "sub/../../deep_escape.tex": "escaped",
            "main.tex": PAPER,
        }
    )
    result = run(paper, payload)

    assert result.status == ft.OK
    assert result.files == ("main.tex",)  # 只有干净成员落了盘
    assert len(result.rejected) == 4
    assert result.warnings and "不安全成员" in result.warnings[0]
    # 工作目录之外一个字节都没写
    assert not (paper.path.parent / "evil.tex").exists()
    assert not (tmp_path / "evil2.tex").exists()
    assert not (tmp_path / "deep_escape.tex").exists()
    assert not Path("/tmp/absolute.tex").exists()


def test_symlink_and_device_members_are_rejected(paper):
    link = tarfile.TarInfo("passwd.tex")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    hard = tarfile.TarInfo("hard.tex")
    hard.type = tarfile.LNKTYPE
    hard.linkname = "main.tex"
    result = run(paper, tar_bytes({"main.tex": PAPER}, extra=(link, hard)))

    assert result.status == ft.OK
    assert result.files == ("main.tex",)
    assert sorted(result.rejected) == ["hard.tex", "passwd.tex"]
    assert not (paper.src / "passwd.tex").exists()


def test_corrupt_gzip_is_structured_error(paper):
    result = run(paper, b"\x1f\x8b\x08\x00 garbage that is neither tar nor gz")
    assert result.status == ft.UNPACK_FAILED
    assert "解压失败" in result.message


# --------------------------------------------------------------------------- #
# 下载路径本身
# --------------------------------------------------------------------------- #


def test_url_is_built_from_arxiv_id(paper):
    grab = fetcher_of(PAPER.encode("utf-8"))
    result = ft.fetch("2401.01234v2", paper, fetcher=grab)
    assert grab.seen == ["https://export.arxiv.org/e-print/2401.01234v2"]
    assert result.url == grab.seen[0]


def test_old_style_id_keeps_slash(paper):
    grab = fetcher_of(PAPER.encode("utf-8"))
    ft.fetch("hep-th/9901001", paper, fetcher=grab)
    assert grab.seen == ["https://export.arxiv.org/e-print/hep-th/9901001"]


def test_explicit_url_overrides_id(paper):
    grab = fetcher_of(PAPER.encode("utf-8"))
    ft.fetch("2401.01234", paper, fetcher=grab, url="http://mirror.local/e/2401.01234")
    assert grab.seen == ["http://mirror.local/e/2401.01234"]


def test_download_error_is_structured_not_raised(paper):
    def boom(url: str) -> bytes:
        raise OSError("connection reset by peer")

    result = ft.fetch("2401.01234", paper, fetcher=boom)
    assert result.status == ft.DOWNLOAD_FAILED
    assert "connection reset" in result.message
    assert result.ok is False and result.fallback is False


def test_empty_response_is_download_failed(paper):
    result = run(paper, b"")
    assert result.status == ft.DOWNLOAD_FAILED


@pytest.mark.parametrize("bad", ["", "   ", "../etc/passwd", "/absolute", "has space"])
def test_illegal_ids_are_structured(paper, bad):
    result = ft.fetch(bad, paper, fetcher=fetcher_of(b"unused"))
    assert result.status == ft.SOURCE_MISSING


def test_workdir_layout_is_created(paper):
    run(paper, tar_bytes({"main.tex": PAPER}))
    for area in (paper.src, paper.build, paper.out, paper.logs, paper.manifests):
        assert area.is_dir()


def test_to_json_is_flat(paper):
    data = run(paper, tar_bytes({"main.tex": PAPER})).to_json()
    assert data["status"] == ft.OK
    assert data["kind"] == "tar.gz"
    assert data["tex_files"] == ["main.tex"]
    assert data["url"].endswith("2401.01234")


# --------------------------------------------------------------------------- #
# 本地目录输入（tongtu run <dir>）
# --------------------------------------------------------------------------- #


def test_ingest_local_copies_tree(paper, tmp_path):
    source = tmp_path / "mypaper"
    (source / "sections").mkdir(parents=True)
    (source / "main.tex").write_text(PAPER, encoding="utf-8")
    (source / "sections" / "intro.tex").write_text("\\section{Intro}\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("[core]", encoding="utf-8")

    result = ft.ingest_local(source, paper)

    assert result.status == ft.OK and result.kind == "local"
    assert result.files == ("main.tex", "sections/intro.tex")  # .git 被跳过
    assert (paper.src / "main.tex").read_text(encoding="utf-8") == PAPER
    assert (source / "main.tex").exists()  # 用户目录原样不动


def test_ingest_local_skips_nested_workdir(tmp_path):
    """`tongtu run .` 时工作目录可能就在源码目录里——不能把自己拷进自己。"""
    source = tmp_path / "mypaper"
    source.mkdir()
    (source / "main.tex").write_text(PAPER, encoding="utf-8")
    inside = Workdir(path=source / ".tongtu")

    result = ft.ingest_local(source, inside)

    assert result.status == ft.OK
    assert result.files == ("main.tex",)
    assert not (inside.src / ".tongtu").exists()


def test_ingest_local_on_src_itself_is_noop(paper):
    paper.create()
    (paper.src / "main.tex").write_text(PAPER, encoding="utf-8")
    result = ft.ingest_local(paper.src, paper)
    assert result.status == ft.OK
    assert result.files == ("main.tex",)


def test_ingest_local_missing_dir(paper, tmp_path):
    result = ft.ingest_local(tmp_path / "nope", paper)
    assert result.status == ft.SOURCE_MISSING
    assert "不存在" in result.message


def test_ingest_local_pdf_shell(paper, tmp_path):
    source = tmp_path / "shellpaper"
    source.mkdir()
    (source / "wrapper.tex").write_text(SHELL, encoding="utf-8")
    (source / "camera-ready.pdf").write_bytes(b"%PDF-1.4 fake")

    result = ft.ingest_local(source, paper)
    assert result.status == ft.PDF_ONLY and result.fallback is True
