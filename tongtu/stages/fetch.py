from __future__ import annotations

import gzip
import io
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path

import httpx

from .. import arxiv, pipeline
from ..artifacts.fetch import FetchKind, FetchManifest, FetchRoute, FetchStatus, PaperMeta
from ..arxiv import PaperInput
from ..manifests import describe_error, write_manifest
from ..workdir import Workdir

STAGE_NAME = "fetch"

EPRINT_URL_TEMPLATE = "https://export.arxiv.org/e-print/{paper_id}"

PDF_URL_TEMPLATE = "https://export.arxiv.org/pdf/{paper_id}"

PDF_MAGIC = b"%PDF"

GZIP_MAGIC = b"\x1f\x8b"

MIN_TEX_CHARS = 1000

MIN_TEX_CHARS_WITH_INCLUDEPDF = 5000

COPY_SKIP_NAMES = frozenset({".git", ".svn", ".hg", "__pycache__", ".DS_Store"})


def run(paper: PaperInput, paper_workdir: Workdir, meta: PaperMeta | None = None) -> FetchManifest:
    paper_workdir.create()
    if paper.source_pdf is not None:
        manifest = _fetch_local_pdf(paper.source_pdf, paper_workdir)
    elif paper.source_dir is not None:
        manifest = _fetch_local(paper.source_dir, paper_workdir)
    else:
        manifest = _fetch_remote(paper, paper_workdir, meta)
    if manifest.status is not FetchStatus.OK and _src_is_clearable(paper, paper_workdir):
        _reset_src(paper_workdir)
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _fetch_remote(paper: PaperInput, paper_workdir: Workdir, meta: PaperMeta | None) -> FetchManifest:
    if paper.arxiv_id is None or paper.version is None:
        raise ValueError(f"fetch needs an arXiv id with a version, got {paper}")
    paper_id = arxiv.versioned_id(paper.arxiv_id, paper.version)
    url = EPRINT_URL_TEMPLATE.format(paper_id=paper_id)
    identity = {"source": paper_id, "arxiv_id": paper.arxiv_id, "version": paper.version, "url": url}
    _reset_outputs(paper_workdir)
    if meta is None:
        try:
            meta = arxiv.find_meta(paper.arxiv_id, paper.version).meta
        except arxiv.ArxivError as error:
            return FetchManifest(status=FetchStatus.DOWNLOAD_FAILED, message=str(error), **identity)
    write_manifest(paper_workdir.meta, meta)
    try:
        payload = _download(url)
    except Exception as error:
        return FetchManifest(
            status=FetchStatus.DOWNLOAD_FAILED,
            message=f"source download failed: {describe_error(error)}",
            **identity,
        )
    if not payload:
        return FetchManifest(
            status=FetchStatus.EMPTY,
            message="download succeeded but the response body is empty.",
            **identity,
        )
    paper_workdir.eprint.write_bytes(payload)
    try:
        kind, rejected, warnings, pdf = _unpack_payload(payload, paper_workdir.src)
    except Exception as error:
        return FetchManifest(
            status=FetchStatus.UNPACK_FAILED,
            payload_bytes=len(payload),
            message=describe_error(error),
            **identity,
        )
    manifest = _manifest_from_src(
        paper_workdir,
        kind=kind,
        payload_bytes=len(payload),
        rejected=rejected,
        warnings=warnings,
        has_pdf=pdf is not None,
        **identity,
    )
    if manifest.route != "pdf":
        return manifest
    _reset_src(paper_workdir)
    if pdf is None:
        pdf_url = PDF_URL_TEMPLATE.format(paper_id=paper_id)
        try:
            pdf = _download(pdf_url)
        except Exception as error:
            manifest.status = FetchStatus.DOWNLOAD_FAILED
            manifest.message = f"{paper_id} has no TeX source and the PDF download failed: {describe_error(error)}"
            return manifest
        if pdf[: len(PDF_MAGIC)] != PDF_MAGIC:
            manifest.status = FetchStatus.DOWNLOAD_FAILED
            manifest.message = f"{paper_id} has no TeX source and {pdf_url} did not return a PDF."
            return manifest
    paper_workdir.original_pdf.write_bytes(pdf)
    return manifest


def _fetch_local(source_dir: Path, paper_workdir: Workdir) -> FetchManifest:
    source = Path(source_dir).expanduser().absolute()
    if not source.is_dir():
        return FetchManifest(
            status=FetchStatus.SOURCE_MISSING,
            source=str(source),
            kind="local",
            message=f"source directory does not exist or is not a directory: {source}",
        )
    source_real = source.resolve()
    workdir_real = paper_workdir.path.resolve()
    if source_real == workdir_real or source_real.is_relative_to(paper_workdir.src.resolve()):
        return FetchManifest(
            status=FetchStatus.UNPACK_FAILED,
            source=str(source),
            kind="local",
            message="source directory overlaps the working directory; cannot copy it into src/ of this workdir.",
        )
    _reset_outputs(paper_workdir)
    try:
        shutil.copytree(source, paper_workdir.src, ignore=_copy_ignore(workdir_real), dirs_exist_ok=True)
    except Exception as error:
        return FetchManifest(
            status=FetchStatus.UNPACK_FAILED, source=str(source), kind="local", message=describe_error(error)
        )
    return _manifest_from_src(paper_workdir, source=str(source), kind="local", pdf_route=False)


def _fetch_local_pdf(source_pdf: Path, paper_workdir: Workdir) -> FetchManifest:
    source = Path(source_pdf).expanduser().absolute()
    identity = {"source": str(source), "kind": "local_pdf", "route": "pdf"}
    _reset_outputs(paper_workdir)
    try:
        payload = source.read_bytes()
    except OSError as error:
        return FetchManifest(
            status=FetchStatus.SOURCE_MISSING,
            message=f"cannot read the source PDF {source} ({describe_error(error)})",
            **identity,
        )
    if payload[: len(PDF_MAGIC)] != PDF_MAGIC:
        return FetchManifest(
            status=FetchStatus.SOURCE_MISSING,
            message=f"{source} does not start with {PDF_MAGIC.decode()}; it is not a PDF file.",
            **identity,
        )
    paper_workdir.original_pdf.write_bytes(payload)
    return FetchManifest(status=FetchStatus.OK, payload_bytes=len(payload), **identity)


def _download(url: str) -> bytes:
    response = httpx.get(
        url,
        headers={"User-Agent": arxiv.USER_AGENT},
        timeout=arxiv.REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def _unpack_payload(payload: bytes, src: Path) -> tuple[FetchKind, list[str], list[str], bytes | None]:
    if payload[:4] == PDF_MAGIC:
        return "pdf", [], [], payload
    if payload[:2] == GZIP_MAGIC:
        try:
            archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")
        except tarfile.ReadError:
            archive = None
        if archive is not None:
            with archive:
                rejected, warnings = _extract_tar_members(archive, src)
            return "tar.gz", rejected, warnings, None
        decompressed = gzip.decompress(payload)
        if decompressed[:4] == PDF_MAGIC:
            return "gz", [], [], decompressed
        (src / "main.tex").write_bytes(decompressed)
        return "gz", [], [], None
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.ReadError:
        archive = None
    if archive is not None:
        with archive:
            rejected, warnings = _extract_tar_members(archive, src)
        return "tar", rejected, warnings, None
    (src / "main.tex").write_bytes(payload)
    return "tex", [], [], None


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
            f"unpacking rejected {len(rejected)} tar members (not regular files or directories, "
            "or unsafe paths); see the rejected field for the list"
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
    arxiv_id: str | None = None,
    version: int | None = None,
    url: str = "",
    payload_bytes: int = 0,
    rejected: list[str] | None = None,
    warnings: list[str] | None = None,
    has_pdf: bool = False,
    pdf_route: bool = True,
) -> FetchManifest:
    src = paper_workdir.src
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
    status, route, reason = _verdict(has_pdf, tex_files, tex_chars, has_includepdf)
    if route == "pdf" and not pdf_route:
        status, route = FetchStatus.EMPTY, "tex"
        reason = f"no TeX source: {reason} To rebuild a paper from its PDF, pass the PDF file as PAPER."
    if route == "pdf":
        warnings = [*(warnings or []), f"no TeX source: {reason} The paper is rebuilt from its PDF."]
    return FetchManifest(
        status=status,
        source=source,
        arxiv_id=arxiv_id,
        version=version,
        kind=kind,
        route=route,
        url=url,
        payload_bytes=payload_bytes,
        tex_files=tex_files,
        tex_chars=tex_chars,
        rejected=list(rejected or []),
        warnings=list(warnings or []),
        message=reason if status is not FetchStatus.OK else "",
    )


def _verdict(
    has_pdf: bool, tex_files: list[str], tex_chars: int, has_includepdf: bool
) -> tuple[FetchStatus, FetchRoute, str]:
    if not tex_files:
        if has_pdf:
            return FetchStatus.OK, "pdf", "src/ has no .tex files, only a PDF."
        return FetchStatus.EMPTY, "tex", "src/ has neither .tex files nor a PDF."
    if tex_chars < MIN_TEX_CHARS:
        return (
            FetchStatus.OK,
            "pdf",
            (
                f"total .tex characters {tex_chars} is below {MIN_TEX_CHARS}; no substantial text, treated as a PDF shell."
            ),
        )
    if has_includepdf and tex_chars < MIN_TEX_CHARS_WITH_INCLUDEPDF:
        return (
            FetchStatus.OK,
            "pdf",
            (
                f".tex uses \\includepdf and total characters {tex_chars} is below {MIN_TEX_CHARS_WITH_INCLUDEPDF}; "
                "judged a pdfpages shell."
            ),
        )
    return FetchStatus.OK, "tex", ""


def _src_is_clearable(paper: PaperInput, paper_workdir: Workdir) -> bool:
    if paper.source_dir is None:
        return True
    source_real = Path(paper.source_dir).expanduser().absolute().resolve()
    workdir_real = paper_workdir.path.resolve()
    return source_real != workdir_real and not source_real.is_relative_to(paper_workdir.src.resolve())


def _reset_outputs(paper_workdir: Workdir) -> None:
    pipeline.clean(paper_workdir, STAGE_NAME)
    paper_workdir.create()


def _reset_src(paper_workdir: Workdir) -> None:
    shutil.rmtree(paper_workdir.src, ignore_errors=True)
    paper_workdir.create()
