from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tongtu import arxiv
from tongtu.artifacts.fetch import CachedMeta, PaperMeta
from tongtu.manifests import load_manifest, write_manifest
from tongtu.workdir import Workdir, WorkdirError

LATEST_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/api//e5XJ8Cf+qeZoQhn1CF4DuD+yuk</id>
  <title>arXiv Query: search_query=&amp;id_list=2106.04426&amp;start=0&amp;max_results=10</title>
  <opensearch:totalResults>1</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2106.04426v3</id>
    <title>Hash Layers For Large
      Sparse Models</title>
    <updated>2021-07-20T13:46:33Z</updated>
    <link href="https://arxiv.org/abs/2106.04426v3" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2106.04426v3" rel="related" type="application/pdf" title="pdf"/>
    <summary>We investigate the training of sparse layers that use different parameters
for different inputs based on hashing in large Transformer models.</summary>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <published>2021-06-08T14:54:24Z</published>
    <arxiv:primary_category term="cs.LG"/>
    <author>
      <name>Stephen Roller</name>
    </author>
    <author>
      <name>Sainbayar Sukhbaatar</name>
    </author>
    <author>
      <name>Arthur Szlam</name>
    </author>
    <author>
      <name>Jason Weston</name>
    </author>
  </entry>
</feed>
"""

VERSIONED_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/api/IUF+hKXTRc2J96doXrQyIC26Rw0</id>
  <opensearch:totalResults>1</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/1412.6980v8</id>
    <title>Adam: A Method for Stochastic Optimization</title>
    <updated>2015-07-23T20:27:47Z</updated>
    <summary>We introduce Adam, an algorithm for first-order gradient-based optimization.</summary>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <published>2014-12-22T13:54:29Z</published>
    <arxiv:comment>Published as a conference paper at the 3rd International Conference for Learning Representations, San Diego, 2015</arxiv:comment>
    <arxiv:primary_category term="cs.LG"/>
    <author>
      <name>Diederik Kingma</name>
    </author>
    <author>
      <name>Jimmy Ba</name>
    </author>
  </entry>
</feed>
"""

OLD_STYLE_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/hep-th/9901001v3</id>
    <title>String Junctions and Their Duals in Heterotic String Theory</title>
    <updated>1999-05-10T04:45:54Z</updated>
    <summary>  We explicitly give the correspondence between spectra of heterotic string theory.</summary>
    <category term="hep-th" scheme="http://arxiv.org/schemas/atom"/>
    <published>1999-01-01T01:01:10Z</published>
    <arxiv:comment>13 pages + 4 eps figures, PTPTeX, typographical errors corrected</arxiv:comment>
    <arxiv:primary_category term="hep-th"/>
    <arxiv:journal_ref>Prog.Theor.Phys.101:1155-1164,1999</arxiv:journal_ref>
    <author>
      <name>Yosuke Imamura</name>
    </author>
    <arxiv:doi>10.1143/PTP.101.1155</arxiv:doi>
  </entry>
</feed>
"""

MISSING_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/api/LUV3fyibCktl9NWN1BGmwxjdgNo</id>
  <title>arXiv Query: search_query=&amp;id_list=2106.99999&amp;start=0&amp;max_results=10</title>
  <opensearch:totalResults>0</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
</feed>
"""

ERROR_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/</id>
  <title>arXiv Search Results</title>
  <entry>
    <id>https://arxiv.org/api/errors#incorrect_id_format_for_notanid</id>
    <title>Error</title>
    <updated>2026-11-25T15:15:29Z</updated>
    <summary>incorrect id format for notanid</summary>
    <author>
      <name>arXiv api core</name>
    </author>
  </entry>
</feed>
"""


def wire_api(monkeypatch: pytest.MonkeyPatch, feed: str, status_code: int = 200) -> list[str]:
    urls: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        urls.append(url)
        return httpx.Response(status_code, text=feed, request=httpx.Request("GET", url))

    monkeypatch.setattr(arxiv.httpx, "get", get)
    return urls


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def cache_file(home: Path, key: str) -> Path:
    return home / "cache" / "arxiv" / f"{key}.json"


def write_cached(home: Path, key: str, meta: PaperMeta, fetched_at: datetime) -> None:
    write_manifest(cache_file(home, key), CachedMeta(fetched_at=fetched_at, meta=meta))


def adam(version: int = 8, title: str = "Adam") -> PaperMeta:
    return PaperMeta(
        arxiv_id="1412.6980",
        version=version,
        title=title,
        authors=["Diederik Kingma", "Jimmy Ba"],
        abstract="We introduce Adam.",
        primary_category="cs.LG",
        categories=["cs.LG"],
        published=datetime(2014, 12, 22, 13, 54, 29, tzinfo=UTC),
        updated=datetime(2015, 7, 23, 20, 27, 47, tzinfo=UTC),
        comment=None,
        journal_ref=None,
        doi=None,
        abs_url=f"https://arxiv.org/abs/1412.6980v{version}",
        pdf_url=f"https://arxiv.org/pdf/1412.6980v{version}",
    )


def test_find_meta_without_version_asks_arxiv_and_writes_two_cache_entries(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = wire_api(monkeypatch, LATEST_FEED)
    found = arxiv.find_meta("2106.04426")
    assert (found.source, found.meta.version) == (arxiv.MetaSource.ARXIV, 3)
    assert urls == [f"{arxiv.API_URL}?id_list=2106.04426"]
    for key in ("2106.04426", "2106.04426v3"):
        cached = load_manifest(cache_file(isolated_home, key), CachedMeta)
        assert cached is not None
        assert (cached.meta, cached.fetched_at) == (found.meta, found.fetched_at)


def test_find_meta_reads_a_fresh_latest_entry_from_the_cache(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched_at = datetime.now(UTC) - timedelta(seconds=arxiv.LATEST_CACHE_SECONDS - 60)
    write_cached(isolated_home, "1412.6980", adam(), fetched_at)
    urls = wire_api(monkeypatch, VERSIONED_FEED)
    found = arxiv.find_meta("1412.6980")
    assert (found.source, found.meta, found.fetched_at) == (arxiv.MetaSource.CACHE, adam(), fetched_at)
    assert urls == []


def test_find_meta_asks_arxiv_again_once_the_latest_entry_expires(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched_at = datetime.now(UTC) - timedelta(seconds=arxiv.LATEST_CACHE_SECONDS + 60)
    write_cached(isolated_home, "1412.6980", adam(version=7), fetched_at)
    urls = wire_api(monkeypatch, VERSIONED_FEED)
    found = arxiv.find_meta("1412.6980")
    assert (found.source, found.meta.version) == (arxiv.MetaSource.ARXIV, 8)
    assert len(urls) == 1
    cached = load_manifest(cache_file(isolated_home, "1412.6980"), CachedMeta)
    assert cached is not None and cached.meta.version == 8


def test_find_meta_keeps_a_versioned_entry_without_expiry(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fetched_at = datetime(2020, 1, 1, tzinfo=UTC)
    write_cached(isolated_home, "1412.6980v8", adam(), fetched_at)
    urls = wire_api(monkeypatch, VERSIONED_FEED)
    found = arxiv.find_meta("1412.6980", 8)
    assert (found.source, found.fetched_at) == (arxiv.MetaSource.CACHE, fetched_at)
    assert urls == []


def test_find_meta_with_version_writes_one_cache_entry(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_api(monkeypatch, VERSIONED_FEED)
    found = arxiv.find_meta("1412.6980", 8)
    assert found.source == arxiv.MetaSource.ARXIV
    assert sorted(path.name for path in cache_file(isolated_home, "x").parent.iterdir()) == ["1412.6980v8.json"]


def test_find_meta_uses_the_normalized_id_as_the_cache_key(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_api(monkeypatch, OLD_STYLE_FEED)
    arxiv.find_meta("hep-th/9901001")
    assert cache_file(isolated_home, "hep-th_9901001").is_file()
    assert cache_file(isolated_home, "hep-th_9901001v3").is_file()


def test_find_meta_prefers_the_meta_file_of_the_working_directory(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = Workdir(isolated_home / "papers" / "1412.6980v8")
    write_manifest(workdir.meta, adam(title="from the working directory"))
    write_cached(isolated_home, "1412.6980v8", adam(title="from the cache"), datetime.now(UTC))
    urls = wire_api(monkeypatch, VERSIONED_FEED)
    found = arxiv.find_meta("1412.6980", 8)
    assert (found.source, found.meta.title) == (arxiv.MetaSource.WORKDIR, "from the working directory")
    assert found.fetched_at == datetime.fromtimestamp(workdir.meta.stat().st_mtime, UTC)
    assert urls == []


def test_find_meta_without_version_ignores_the_working_directory(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(Workdir(isolated_home / "papers" / "1412.6980v8").meta, adam())
    urls = wire_api(monkeypatch, VERSIONED_FEED)
    assert arxiv.find_meta("1412.6980").source == arxiv.MetaSource.ARXIV
    assert len(urls) == 1


@pytest.mark.parametrize("content", ["{", '{"fetched_at": "2026-01-01T00:00:00Z"}'])
def test_find_meta_treats_a_broken_cache_file_as_absent(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    path = cache_file(isolated_home, "1412.6980v8")
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    wire_api(monkeypatch, VERSIONED_FEED)
    assert arxiv.find_meta("1412.6980", 8).source == arxiv.MetaSource.ARXIV
    assert load_manifest(path, CachedMeta) is not None


def test_find_meta_treats_a_cache_time_without_a_timezone_as_absent(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = cache_file(isolated_home, "1412.6980")
    path.parent.mkdir(parents=True)
    content = {"fetched_at": datetime.now().isoformat(), "meta": adam(version=7).model_dump(mode="json")}
    path.write_text(json.dumps(content), encoding="utf-8")
    urls = wire_api(monkeypatch, VERSIONED_FEED)
    found = arxiv.find_meta("1412.6980")
    assert (found.source, found.meta.version) == (arxiv.MetaSource.ARXIV, 8)
    assert len(urls) == 1
    cached = load_manifest(path, CachedMeta)
    assert cached is not None and cached.fetched_at.tzinfo is not None


def test_find_meta_refresh_skips_the_working_directory_and_the_cache(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(Workdir(isolated_home / "papers" / "1412.6980v8").meta, adam(title="old"))
    write_cached(isolated_home, "1412.6980v8", adam(title="old"), datetime(2020, 1, 1, tzinfo=UTC))
    urls = wire_api(monkeypatch, VERSIONED_FEED)
    found = arxiv.find_meta("1412.6980", 8, refresh=True)
    assert (found.source, found.meta.title) == (arxiv.MetaSource.ARXIV, "Adam: A Method for Stochastic Optimization")
    assert len(urls) == 1
    cached = load_manifest(cache_file(isolated_home, "1412.6980v8"), CachedMeta)
    assert cached is not None and cached.meta == found.meta


def test_find_meta_leaves_no_cache_entry_when_arxiv_fails(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_api(monkeypatch, MISSING_FEED)
    with pytest.raises(arxiv.ArxivError):
        arxiv.find_meta("2106.99999")
    assert not cache_file(isolated_home, "2106.99999").exists()


def test_split_version_separates_the_suffix() -> None:
    assert arxiv.split_version("2412.19437v2") == ("2412.19437", 2)
    assert arxiv.split_version("2412.19437") == ("2412.19437", None)
    assert arxiv.split_version("hep-th/9901001v3") == ("hep-th/9901001", 3)
    assert arxiv.split_version("solv-int/9901001") == ("solv-int/9901001", None)
    assert arxiv.versioned_id("hep-th/9901001", 3) == "hep-th/9901001v3"


def test_parse_paper_argument_recognizes_the_three_forms(tmp_path: Path) -> None:
    assert arxiv.parse_paper_argument("2002.05202") == arxiv.PaperInput(arxiv_id="2002.05202")
    assert arxiv.parse_paper_argument("https://arxiv.org/abs/2002.05202").arxiv_id == "2002.05202"
    assert arxiv.parse_paper_argument("https://arxiv.org/pdf/2002.05202.pdf").arxiv_id == "2002.05202"
    assert arxiv.parse_paper_argument("https://arxiv.org/html/hep-th/9901001").arxiv_id == "hep-th/9901001"
    assert arxiv.parse_paper_argument(str(tmp_path)) == arxiv.PaperInput(source_dir=tmp_path)


def test_parse_paper_argument_reads_the_version(tmp_path: Path) -> None:
    assert arxiv.parse_paper_argument("2412.19437v2") == arxiv.PaperInput(arxiv_id="2412.19437", version=2)
    assert arxiv.parse_paper_argument("hep-th/9901001v3") == arxiv.PaperInput(arxiv_id="hep-th/9901001", version=3)
    assert arxiv.parse_paper_argument("https://arxiv.org/abs/2412.19437v2") == arxiv.PaperInput(
        arxiv_id="2412.19437", version=2
    )
    assert arxiv.parse_paper_argument("https://arxiv.org/pdf/2412.19437v2.pdf").version == 2


def test_parse_paper_argument_rejects_bad_input() -> None:
    with pytest.raises(arxiv.PaperArgumentError):
        arxiv.parse_paper_argument("https://example.com/abs/2002.05202")
    with pytest.raises(arxiv.PaperArgumentError):
        arxiv.parse_paper_argument("https://arxiv.org/no-prefix/2002.05202")
    with pytest.raises(WorkdirError):
        arxiv.parse_paper_argument("a b")
    with pytest.raises(WorkdirError):
        arxiv.parse_paper_argument(".")


def test_latest_local_version_takes_the_highest_versioned_directory(tmp_path: Path) -> None:
    for name in ("2412.19437v1", "2412.19437v10", "2412.19437v2", "2412.19437", "2412.194370v99", "x2412.19437v50"):
        (tmp_path / name).mkdir()
    (tmp_path / "2412.19437v11").write_text("not a directory", encoding="utf-8")
    assert arxiv.latest_local_version(tmp_path, "2412.19437") == 10


def test_latest_local_version_ignores_the_unversioned_directory(tmp_path: Path) -> None:
    (tmp_path / "2412.19437").mkdir()
    assert arxiv.latest_local_version(tmp_path, "2412.19437") is None
    assert arxiv.latest_local_version(tmp_path / "absent", "2412.19437") is None


def test_latest_local_version_matches_the_normalized_old_style_id(tmp_path: Path) -> None:
    (tmp_path / "hep-th_9901001v3").mkdir()
    assert arxiv.latest_local_version(tmp_path, "hep-th/9901001") == 3


def test_query_without_version_returns_the_latest_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch, LATEST_FEED)
    meta = arxiv.query("2106.04426")
    assert urls == ["https://export.arxiv.org/api/query?id_list=2106.04426"]
    assert (meta.arxiv_id, meta.version) == ("2106.04426", 3)
    assert meta.title == "Hash Layers For Large Sparse Models"
    assert meta.authors == ["Stephen Roller", "Sainbayar Sukhbaatar", "Arthur Szlam", "Jason Weston"]
    assert meta.abstract == (
        "We investigate the training of sparse layers that use different parameters "
        "for different inputs based on hashing in large Transformer models."
    )
    assert meta.primary_category == "cs.LG"
    assert meta.categories == ["cs.LG", "cs.CL"]
    assert meta.published == datetime(2021, 6, 8, 14, 54, 24, tzinfo=UTC)
    assert meta.updated == datetime(2021, 7, 20, 13, 46, 33, tzinfo=UTC)
    assert (meta.comment, meta.journal_ref, meta.doi) == (None, None, None)
    assert meta.abs_url == "https://arxiv.org/abs/2106.04426v3"
    assert meta.pdf_url == "https://arxiv.org/pdf/2106.04426v3"


def test_query_with_version_asks_for_that_version(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch, VERSIONED_FEED)
    meta = arxiv.query("1412.6980", 8)
    assert urls == ["https://export.arxiv.org/api/query?id_list=1412.6980v8"]
    assert (meta.arxiv_id, meta.version) == ("1412.6980", 8)
    assert meta.comment == (
        "Published as a conference paper at the 3rd International Conference for Learning Representations, "
        "San Diego, 2015"
    )
    assert meta.pdf_url == "https://arxiv.org/pdf/1412.6980v8"


def test_query_reads_journal_ref_and_doi_of_an_old_style_id(monkeypatch: pytest.MonkeyPatch) -> None:
    wire_api(monkeypatch, OLD_STYLE_FEED)
    meta = arxiv.query("hep-th/9901001")
    assert (meta.arxiv_id, meta.version) == ("hep-th/9901001", 3)
    assert meta.journal_ref == "Prog.Theor.Phys.101:1155-1164,1999"
    assert meta.doi == "10.1143/PTP.101.1155"
    assert meta.abstract == "We explicitly give the correspondence between spectra of heterotic string theory."
    assert meta.abs_url == "https://arxiv.org/abs/hep-th/9901001v3"


def test_query_of_an_absent_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    wire_api(monkeypatch, MISSING_FEED)
    with pytest.raises(arxiv.ArxivError, match="no entry for 2106.99999"):
        arxiv.query("2106.99999")


@pytest.mark.parametrize("status_code", [200, 400])
def test_query_of_an_error_entry_raises(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    wire_api(monkeypatch, ERROR_FEED, status_code)
    with pytest.raises(arxiv.ArxivError):
        arxiv.query("notanid")


def test_query_reports_a_failed_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(arxiv.httpx, "get", get)
    with pytest.raises(arxiv.ArxivError, match="ReadTimeout"):
        arxiv.query("2106.04426")


def test_query_reports_a_response_that_is_not_xml(monkeypatch: pytest.MonkeyPatch) -> None:
    wire_api(monkeypatch, "<html>")
    with pytest.raises(arxiv.ArxivError):
        arxiv.query("2106.04426")


def wire_statuses(
    monkeypatch: pytest.MonkeyPatch, statuses: list[int], headers: dict[str, str] | None = None
) -> tuple[list[str], list[float]]:
    urls: list[str] = []
    waits: list[float] = []
    pending = iter(statuses)

    def get(url: str, **kwargs: object) -> httpx.Response:
        urls.append(url)
        status_code = next(pending)
        feed = VERSIONED_FEED if status_code == 200 else ""
        return httpx.Response(status_code, text=feed, headers=headers, request=httpx.Request("GET", url))

    monkeypatch.setattr(arxiv.httpx, "get", get)
    monkeypatch.setattr(arxiv.time, "sleep", waits.append)
    return urls, waits


def test_query_waits_and_retries_after_a_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    urls, waits = wire_statuses(monkeypatch, [429, 200])
    meta = arxiv.query("1412.6980", 8)
    assert (meta.arxiv_id, meta.version) == ("1412.6980", 8)
    assert len(urls) == 2
    assert waits == [5]


def test_query_waits_for_the_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    urls, waits = wire_statuses(monkeypatch, [429, 200], {"Retry-After": "2"})
    arxiv.query("1412.6980", 8)
    assert len(urls) == 2
    assert waits == [2]


def test_query_ignores_a_retry_after_header_that_is_not_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    urls, waits = wire_statuses(monkeypatch, [429, 200], {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    arxiv.query("1412.6980", 8)
    assert len(urls) == 2
    assert waits == [5]


def test_query_retries_when_arxiv_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    urls, waits = wire_statuses(monkeypatch, [503, 503, 200])
    arxiv.query("1412.6980", 8)
    assert len(urls) == 3
    assert waits == [5, 15]


def test_query_raises_once_the_retries_run_out(monkeypatch: pytest.MonkeyPatch) -> None:
    urls, waits = wire_statuses(monkeypatch, [429, 429, 429, 429])
    with pytest.raises(arxiv.ArxivError, match="after 3 retries"):
        arxiv.query("1412.6980", 8)
    assert len(urls) == 4
    assert waits == [5, 15, 45]


def test_query_does_not_retry_a_missing_page(monkeypatch: pytest.MonkeyPatch) -> None:
    urls, waits = wire_statuses(monkeypatch, [404])
    with pytest.raises(arxiv.ArxivError, match="HTTPStatusError"):
        arxiv.query("1412.6980", 8)
    assert len(urls) == 1
    assert waits == []


def translated(meta: PaperMeta, abstract_zh: str = "我们提出 Adam。") -> PaperMeta:
    return meta.model_copy(update={"abstract_zh": abstract_zh})


def test_paper_meta_without_abstract_zh_reads_as_absent(isolated_home: Path) -> None:
    data = json.loads(CachedMeta(fetched_at=datetime.now(UTC), meta=adam()).model_dump_json())
    del data["meta"]["abstract_zh"]
    path = cache_file(isolated_home, "1412.6980v8")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    found = arxiv.find_meta("1412.6980", 8)
    assert (found.source, found.meta.abstract_zh) == (arxiv.MetaSource.CACHE, None)


def test_find_meta_takes_abstract_zh_from_the_versioned_entry_for_an_unversioned_query(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_cached(isolated_home, "1412.6980", adam(), datetime.now(UTC))
    write_cached(isolated_home, "1412.6980v8", translated(adam()), datetime.now(UTC))
    urls = wire_api(monkeypatch, VERSIONED_FEED)
    found = arxiv.find_meta("1412.6980")
    assert (found.source, found.meta.abstract_zh) == (arxiv.MetaSource.CACHE, "我们提出 Adam。")
    assert urls == []


def test_find_meta_prefers_abstract_zh_of_the_working_directory(isolated_home: Path) -> None:
    write_manifest(Workdir(isolated_home / "papers" / "1412.6980v8").meta, translated(adam(), "工作目录"))
    write_cached(isolated_home, "1412.6980", adam(), datetime.now(UTC))
    write_cached(isolated_home, "1412.6980v8", translated(adam(), "缓存"), datetime.now(UTC))
    assert arxiv.find_meta("1412.6980", 8).meta.abstract_zh == "工作目录"
    assert arxiv.find_meta("1412.6980").meta.abstract_zh == "工作目录"


def test_find_meta_falls_back_to_the_cache_when_the_meta_file_has_no_abstract_zh(isolated_home: Path) -> None:
    write_manifest(Workdir(isolated_home / "papers" / "1412.6980v8").meta, adam(title="工作目录"))
    write_cached(isolated_home, "1412.6980v8", translated(adam()), datetime.now(UTC))
    found = arxiv.find_meta("1412.6980", 8)
    assert (found.source, found.meta.title, found.meta.abstract_zh) == (
        arxiv.MetaSource.WORKDIR,
        "工作目录",
        "我们提出 Adam。",
    )


@pytest.mark.parametrize("version", [8, None])
def test_find_meta_refresh_keeps_abstract_zh(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, version: int | None
) -> None:
    write_cached(isolated_home, "1412.6980v8", translated(adam()), datetime(2020, 1, 1, tzinfo=UTC))
    wire_api(monkeypatch, VERSIONED_FEED)
    found = arxiv.find_meta("1412.6980", version, refresh=True)
    assert (found.source, found.meta.abstract_zh) == (arxiv.MetaSource.ARXIV, "我们提出 Adam。")
    cached = load_manifest(cache_file(isolated_home, "1412.6980v8"), CachedMeta)
    assert cached is not None
    assert cached.meta.title == "Adam: A Method for Stochastic Optimization"
    assert cached.meta.abstract_zh == "我们提出 Adam。"


def test_save_abstract_zh_updates_the_versioned_entry(isolated_home: Path) -> None:
    fetched_at = datetime(2020, 1, 1, tzinfo=UTC)
    write_cached(isolated_home, "1412.6980", adam(), datetime.now(UTC))
    write_cached(isolated_home, "1412.6980v8", adam(), fetched_at)
    found = arxiv.find_meta("1412.6980")
    saved = arxiv.save_abstract_zh(found, "译文")
    assert saved.meta == translated(adam(), "译文")
    assert (saved.source, saved.fetched_at) == (found.source, found.fetched_at)
    cached = load_manifest(cache_file(isolated_home, "1412.6980v8"), CachedMeta)
    assert cached == CachedMeta(fetched_at=fetched_at, meta=translated(adam(), "译文"))
    assert not (isolated_home / "papers").exists()


def test_save_abstract_zh_writes_a_missing_entry_and_the_meta_file(isolated_home: Path) -> None:
    workdir = Workdir(isolated_home / "papers" / "1412.6980v8")
    write_manifest(workdir.meta, adam(title="工作目录"))
    found = arxiv.find_meta("1412.6980", 8)
    arxiv.save_abstract_zh(found, "译文")
    assert load_manifest(workdir.meta, PaperMeta) == translated(adam(title="工作目录"), "译文")
    cached = load_manifest(cache_file(isolated_home, "1412.6980v8"), CachedMeta)
    assert cached == CachedMeta(fetched_at=found.fetched_at, meta=translated(adam(title="工作目录"), "译文"))
