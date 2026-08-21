from __future__ import annotations

import gzip
import io
import shutil
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .. import __version__, workdir
from ..artifacts.fetch import FetchKind, FetchManifest, FetchStatus
from ..manifests import describe_error, write_manifest
from ..workdir import Workdir

STAGE_NAME = "fetch"

EPRINT_URL_TEMPLATE = "https://export.arxiv.org/e-print/{arxiv_id}"

DOWNLOAD_TIMEOUT_SECONDS = 60.0

DOWNLOAD_USER_AGENT = f"tongtu/{__version__} (+https://github.com/yhnocoder/tongtu)"

EPRINT_PAYLOAD_FILENAME = "e-print.bin"

PDF_MAGIC = b"%PDF"

GZIP_MAGIC = b"\x1f\x8b"

MIN_TEX_CHARS = 1000

MIN_TEX_CHARS_WITH_INCLUDEPDF = 5000

COPY_SKIP_NAMES = frozenset({".git", ".svn", ".hg", "__pycache__", ".DS_Store"})

PATH_NAVIGATION_LITERALS = frozenset({"", ".", "..", "~"})


class PaperArgumentError(ValueError):
    pass


@dataclass(frozen=True)
class PaperInput:
    arxiv_id: str | None = None
    source_dir: Path | None = None


def parse_arxiv_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host != "arxiv.org" and not host.endswith(".arxiv.org"):
        raise PaperArgumentError(f"不是 arxiv.org 链接：{url}")
    for prefix in ("/abs/", "/pdf/", "/html/"):
        if parts.path.startswith(prefix):
            arxiv_id = parts.path[len(prefix) :].rstrip("/").removesuffix(".pdf")
            if not arxiv_id:
                raise PaperArgumentError(f"链接的路径前缀之后没有编号：{url}")
            return arxiv_id
    raise PaperArgumentError(f"链接路径不含 /abs/、/pdf/、/html/ 前缀：{url}")


def _as_source_directory(paper: str) -> Path | None:
    if paper in PATH_NAVIGATION_LITERALS:
        return None
    try:
        path = Path(paper).expanduser()
    except RuntimeError:
        return None
    return path if path.is_dir() else None


def parse_paper_argument(paper: str) -> PaperInput:
    source_dir = _as_source_directory(paper)
    if source_dir is not None:
        return PaperInput(source_dir=source_dir)
    if paper.startswith(("http://", "https://")):
        arxiv_id = parse_arxiv_url(paper)
    else:
        arxiv_id = paper
    workdir.normalize_arxiv_id(arxiv_id)
    return PaperInput(arxiv_id=arxiv_id)


def run(paper: PaperInput, paper_workdir: Workdir) -> FetchManifest:
    paper_workdir.create()
    if paper.source_dir is not None:
        manifest = _fetch_local(paper.source_dir, paper_workdir)
    else:
        manifest = _fetch_remote(paper.arxiv_id or "", paper_workdir)
    if manifest.status is not FetchStatus.OK and _src_is_clearable(paper, paper_workdir):
        _reset_src(paper_workdir)
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _fetch_remote(arxiv_id: str, paper_workdir: Workdir) -> FetchManifest:
    url = EPRINT_URL_TEMPLATE.format(arxiv_id=arxiv_id)
    _reset_src(paper_workdir)
    payload_path = paper_workdir.build / EPRINT_PAYLOAD_FILENAME
    payload_path.unlink(missing_ok=True)
    try:
        payload = _download_eprint(url)
    except Exception as error:
        return FetchManifest(
            status=FetchStatus.DOWNLOAD_FAILED, source=arxiv_id, url=url, message=describe_error(error)
        )
    if not payload:
        return FetchManifest(
            status=FetchStatus.DOWNLOAD_FAILED, source=arxiv_id, url=url, message="下载成功但响应体为空。"
        )
    payload_path.write_bytes(payload)
    try:
        kind, rejected, warnings = _unpack_payload(payload, paper_workdir.src)
    except Exception as error:
        return FetchManifest(
            status=FetchStatus.UNPACK_FAILED,
            source=arxiv_id,
            url=url,
            payload_bytes=len(payload),
            message=describe_error(error),
        )
    return _manifest_from_src(
        paper_workdir,
        source=arxiv_id,
        kind=kind,
        url=url,
        payload_bytes=len(payload),
        rejected=rejected,
        warnings=warnings,
    )


def _fetch_local(source_dir: Path, paper_workdir: Workdir) -> FetchManifest:
    source = Path(source_dir).expanduser().absolute()
    if not source.is_dir():
        return FetchManifest(
            status=FetchStatus.SOURCE_MISSING,
            source=str(source),
            kind="local",
            message=f"源目录不存在或不是目录：{source}",
        )
    source_real = source.resolve()
    workdir_real = paper_workdir.path.resolve()
    if source_real == workdir_real or source_real.is_relative_to(paper_workdir.src.resolve()):
        return FetchManifest(
            status=FetchStatus.UNPACK_FAILED,
            source=str(source),
            kind="local",
            message="源目录与工作目录重叠，无法把它拷贝进本工作目录的 src/。",
        )
    _reset_src(paper_workdir)
    try:
        shutil.copytree(source, paper_workdir.src, ignore=_copy_ignore(workdir_real), dirs_exist_ok=True)
    except Exception as error:
        return FetchManifest(
            status=FetchStatus.UNPACK_FAILED, source=str(source), kind="local", message=describe_error(error)
        )
    return _manifest_from_src(paper_workdir, source=str(source), kind="local")


def _download_eprint(url: str) -> bytes:
    response = httpx.get(
        url,
        headers={"User-Agent": DOWNLOAD_USER_AGENT},
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def _unpack_payload(payload: bytes, src: Path) -> tuple[FetchKind, list[str], list[str]]:
    if payload[:4] == PDF_MAGIC:
        (src / "main.pdf").write_bytes(payload)
        return "pdf", [], []
    if payload[:2] == GZIP_MAGIC:
        try:
            archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")
        except tarfile.ReadError:
            archive = None
        if archive is not None:
            with archive:
                rejected, warnings = _extract_tar_members(archive, src)
            return "tar.gz", rejected, warnings
        decompressed = gzip.decompress(payload)
        if decompressed[:4] == PDF_MAGIC:
            (src / "main.pdf").write_bytes(decompressed)
        else:
            (src / "main.tex").write_bytes(decompressed)
        return "gz", [], []
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.ReadError:
        archive = None
    if archive is not None:
        with archive:
            rejected, warnings = _extract_tar_members(archive, src)
        return "tar", rejected, warnings
    (src / "main.tex").write_bytes(payload)
    return "tex", [], []


def _extract_tar_members(archive: tarfile.TarFile, dest: Path) -> tuple[list[str], list[str]]:
    rejected: list[str] = []
    for member in archive:
        if not (member.isreg() or member.isdir()):
            rejected.append(member.name)
            continue
        try:
            archive.extract(member, path=dest, filter="data")
        except tarfile.FilterError:
            rejected.append(member.name)
    warnings: list[str] = []
    if rejected:
        warnings.append(
            f"解包拒绝了 {len(rejected)} 个 tar 成员（不是普通文件或目录，或路径不安全），名单见 rejected 字段"
        )
    return rejected, warnings


def _copy_ignore(workdir_real: Path) -> Callable[[str, list[str]], set[str]]:
    def ignore(parent: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name in COPY_SKIP_NAMES}
        for name in names:
            if name not in skipped and (Path(parent) / name).resolve() == workdir_real:
                skipped.add(name)
        return skipped

    return ignore


def _manifest_from_src(
    paper_workdir: Workdir,
    *,
    source: str,
    kind: FetchKind,
    url: str = "",
    payload_bytes: int = 0,
    rejected: list[str] | None = None,
    warnings: list[str] | None = None,
) -> FetchManifest:
    src = paper_workdir.src
    has_pdf = False
    tex_files: list[str] = []
    tex_chars = 0
    has_includepdf = False
    paths = sorted(src.rglob("*"), key=lambda path: path.relative_to(src).as_posix())
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(src).as_posix()
        if path.suffix.lower() == ".pdf":
            has_pdf = True
        if path.suffix.lower() == ".tex":
            text = path.read_text(encoding="utf-8", errors="ignore")
            tex_files.append(relative)
            tex_chars += len(text)
            has_includepdf = has_includepdf or "\\includepdf" in text
    status, message = _verdict(has_pdf, tex_files, tex_chars, has_includepdf)
    return FetchManifest(
        status=status,
        source=source,
        kind=kind,
        url=url,
        payload_bytes=payload_bytes,
        tex_files=tex_files,
        tex_chars=tex_chars,
        rejected=list(rejected or []),
        warnings=list(warnings or []),
        message=message,
    )


def _verdict(has_pdf: bool, tex_files: list[str], tex_chars: int, has_includepdf: bool) -> tuple[FetchStatus, str]:
    if not tex_files:
        if has_pdf:
            return FetchStatus.PDF_ONLY, "src/ 里没有 .tex 文件、只有 PDF，无法从源码翻译。"
        return FetchStatus.EMPTY, "src/ 里既没有 .tex 文件也没有 PDF。"
    if tex_chars < MIN_TEX_CHARS:
        return FetchStatus.PDF_ONLY, (
            f".tex 字符总量 {tex_chars} 低于 {MIN_TEX_CHARS}，没有实质文本内容，按 PDF 套壳处理。"
        )
    if has_includepdf and tex_chars < MIN_TEX_CHARS_WITH_INCLUDEPDF:
        return FetchStatus.PDF_ONLY, (
            f".tex 里出现 \\includepdf 且字符总量 {tex_chars} 低于 {MIN_TEX_CHARS_WITH_INCLUDEPDF}，"
            "判定为 pdfpages 套壳。"
        )
    return FetchStatus.OK, ""


def _src_is_clearable(paper: PaperInput, paper_workdir: Workdir) -> bool:
    if paper.source_dir is None:
        return True
    source_real = Path(paper.source_dir).expanduser().absolute().resolve()
    workdir_real = paper_workdir.path.resolve()
    return source_real != workdir_real and not source_real.is_relative_to(paper_workdir.src.resolve())


def _reset_src(paper_workdir: Workdir) -> None:
    shutil.rmtree(paper_workdir.src, ignore_errors=True)
    paper_workdir.create()
