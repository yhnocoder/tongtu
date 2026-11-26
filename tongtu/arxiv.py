from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from . import __version__
from .artifacts.fetch import CachedMeta, PaperMeta
from .config import arxiv_cache_dir, papers_dir
from .manifests import load_manifest, write_manifest
from .workdir import Workdir, normalize_arxiv_id

API_URL = "https://export.arxiv.org/api/query"

ABS_URL_TEMPLATE = "https://arxiv.org/abs/{paper_id}"

PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{paper_id}"

REQUEST_TIMEOUT_SECONDS = 60.0

USER_AGENT = f"tongtu/{__version__} (+https://github.com/yhnocoder/tongtu)"

VERSION_SUFFIX = re.compile(r"(?P<arxiv_id>.+)v(?P<version>\d+)")

ERROR_ENTRY_MARKER = "arxiv.org/api/errors"

ENTRY_ID_PREFIX = "/abs/"

ATOM = "{http://www.w3.org/2005/Atom}"

ARXIV = "{http://arxiv.org/schemas/atom}"

PATH_NAVIGATION_LITERALS = frozenset({"", ".", "..", "~"})

LATEST_CACHE_SECONDS = 86400


class PaperArgumentError(ValueError):
    pass


class ArxivError(Exception):
    pass


class MetaSource(StrEnum):
    ARXIV = "arxiv"
    CACHE = "cache"
    WORKDIR = "workdir"


@dataclass(frozen=True)
class FoundMeta:
    meta: PaperMeta
    source: MetaSource
    fetched_at: datetime


@dataclass(frozen=True)
class PaperInput:
    arxiv_id: str | None = None
    version: int | None = None
    source_dir: Path | None = None


def split_version(paper_id: str) -> tuple[str, int | None]:
    match = VERSION_SUFFIX.fullmatch(paper_id)
    if match is None:
        return paper_id, None
    return match["arxiv_id"], int(match["version"])


def versioned_id(arxiv_id: str, version: int) -> str:
    return f"{arxiv_id}v{version}"


def parse_arxiv_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host != "arxiv.org" and not host.endswith(".arxiv.org"):
        raise PaperArgumentError(f"not an arxiv.org URL: {url}")
    for prefix in ("/abs/", "/pdf/", "/html/"):
        if parts.path.startswith(prefix):
            arxiv_id = parts.path[len(prefix) :].rstrip("/").removesuffix(".pdf")
            if not arxiv_id:
                raise PaperArgumentError(f"no id after the path prefix in URL: {url}")
            return arxiv_id
    raise PaperArgumentError(f"URL path has none of the /abs/, /pdf/, /html/ prefixes: {url}")


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
    paper_id = parse_arxiv_url(paper) if paper.startswith(("http://", "https://")) else paper
    arxiv_id, version = split_version(paper_id)
    normalize_arxiv_id(arxiv_id)
    return PaperInput(arxiv_id=arxiv_id, version=version)


def latest_local_version(papers: Path, arxiv_id: str) -> int | None:
    pattern = re.compile(re.escape(normalize_arxiv_id(arxiv_id)) + r"v(\d+)")
    if not papers.is_dir():
        return None
    versions = [
        int(match.group(1))
        for entry in papers.iterdir()
        if entry.is_dir() and (match := pattern.fullmatch(entry.name)) is not None
    ]
    return max(versions, default=None)


def find_meta(arxiv_id: str, version: int | None = None, *, refresh: bool = False) -> FoundMeta:
    normalized = normalize_arxiv_id(arxiv_id)
    key = normalized if version is None else versioned_id(normalized, version)
    if not refresh:
        if version is not None:
            meta_path = Workdir(papers_dir() / key).meta
            meta = load_manifest(meta_path, PaperMeta)
            if meta is not None:
                fetched_at = datetime.fromtimestamp(meta_path.stat().st_mtime, UTC)
                return FoundMeta(_with_abstract_zh(meta), MetaSource.WORKDIR, fetched_at)
        cached = load_manifest(_cache_path(key), CachedMeta)
        if cached is not None and (version is not None or _fresh(cached.fetched_at)):
            return FoundMeta(_with_abstract_zh(cached.meta), MetaSource.CACHE, cached.fetched_at)
    meta = _with_abstract_zh(query(arxiv_id, version))
    fetched_at = datetime.now(UTC)
    for cache_key in dict.fromkeys((key, versioned_id(normalized, meta.version))):
        write_manifest(_cache_path(cache_key), CachedMeta(fetched_at=fetched_at, meta=meta))
    return FoundMeta(meta, MetaSource.ARXIV, fetched_at)


def save_abstract_zh(found: FoundMeta, abstract_zh: str) -> FoundMeta:
    key = _versioned_key(found.meta)
    cache_path = _cache_path(key)
    cached = load_manifest(cache_path, CachedMeta)
    entry = cached or CachedMeta(fetched_at=found.fetched_at, meta=found.meta)
    write_manifest(cache_path, entry.model_copy(update={"meta": _translated(entry.meta, abstract_zh)}))
    meta_path = Workdir(papers_dir() / key).meta
    workdir_meta = load_manifest(meta_path, PaperMeta)
    if workdir_meta is not None:
        write_manifest(meta_path, _translated(workdir_meta, abstract_zh))
    return FoundMeta(_translated(found.meta, abstract_zh), found.source, found.fetched_at)


def _translated(meta: PaperMeta, abstract_zh: str) -> PaperMeta:
    return meta.model_copy(update={"abstract_zh": abstract_zh})


def _versioned_key(meta: PaperMeta) -> str:
    return versioned_id(normalize_arxiv_id(meta.arxiv_id), meta.version)


def _with_abstract_zh(meta: PaperMeta) -> PaperMeta:
    key = _versioned_key(meta)
    workdir_meta = load_manifest(Workdir(papers_dir() / key).meta, PaperMeta)
    cached = load_manifest(_cache_path(key), CachedMeta)
    for source in (workdir_meta, cached.meta if cached is not None else None):
        if source is not None and source.abstract_zh:
            return _translated(meta, source.abstract_zh)
    return meta


def _cache_path(key: str) -> Path:
    return arxiv_cache_dir() / f"{key}.json"


def _fresh(fetched_at: datetime) -> bool:
    return datetime.now(UTC) - fetched_at < timedelta(seconds=LATEST_CACHE_SECONDS)


def query(arxiv_id: str, version: int | None = None) -> PaperMeta:
    paper_id = arxiv_id if version is None else versioned_id(arxiv_id, version)
    try:
        response = httpx.get(
            f"{API_URL}?id_list={paper_id}",
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise ArxivError(f"arXiv API request for {paper_id} failed: {type(error).__name__}: {error}") from error
    return parse_feed(response.text, paper_id)


def parse_feed(feed: str, paper_id: str) -> PaperMeta:
    try:
        root = ElementTree.fromstring(feed)
    except ElementTree.ParseError as error:
        raise ArxivError(f"arXiv API response for {paper_id} is not valid XML: {error}") from error
    entry = root.find(f"{ATOM}entry")
    if entry is None:
        raise ArxivError(f"arXiv API has no entry for {paper_id}; check the id and version")
    entry_id = _text(entry, f"{ATOM}id")
    if ERROR_ENTRY_MARKER in entry_id:
        raise ArxivError(f"arXiv API reported an error for {paper_id}: {_collapsed(entry, f'{ATOM}summary')}")
    arxiv_id, version = split_version(entry_id.partition(ENTRY_ID_PREFIX)[2])
    if version is None:
        raise ArxivError(f"arXiv API entry id {entry_id!r} for {paper_id} carries no version")
    primary = entry.find(f"{ARXIV}primary_category")
    try:
        return PaperMeta(
            arxiv_id=arxiv_id,
            version=version,
            title=_collapsed(entry, f"{ATOM}title"),
            authors=[_text(author, f"{ATOM}name") for author in entry.findall(f"{ATOM}author")],
            abstract=_collapsed(entry, f"{ATOM}summary"),
            primary_category=primary.get("term", "") if primary is not None else "",
            categories=[category.get("term", "") for category in entry.findall(f"{ATOM}category")],
            published=_text(entry, f"{ATOM}published"),
            updated=_text(entry, f"{ATOM}updated"),
            comment=_optional(entry, f"{ARXIV}comment"),
            journal_ref=_optional(entry, f"{ARXIV}journal_ref"),
            doi=_optional(entry, f"{ARXIV}doi"),
            abs_url=ABS_URL_TEMPLATE.format(paper_id=versioned_id(arxiv_id, version)),
            pdf_url=PDF_URL_TEMPLATE.format(paper_id=versioned_id(arxiv_id, version)),
        )
    except ValidationError as error:
        raise ArxivError(f"arXiv API entry for {paper_id} is incomplete: {error}") from error


def _text(element: ElementTree.Element, tag: str) -> str:
    return (element.findtext(tag) or "").strip()


def _collapsed(element: ElementTree.Element, tag: str) -> str:
    return " ".join((element.findtext(tag) or "").split())


def _optional(element: ElementTree.Element, tag: str) -> str | None:
    text = element.findtext(tag)
    return text.strip() if text is not None else None
