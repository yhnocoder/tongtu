from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tongtu import arxiv
from tongtu.workdir import WorkdirError

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
