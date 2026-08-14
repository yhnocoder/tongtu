"""静态检验页生成器（架构 §11、决策 8）与 `tongtu preview`。

页面本身的交互没法在 pytest 里点（那要浏览器），能机械验证的是它**赖以成立的前提**：

1. **零外链**：页面与数据文件里不许出现任何 http(s) 请求——检验页要在没网的机器上双击
   即开；vendor（PDF.js）里的 URL 只允许是 XML 命名空间与 license 地址那几个已知常量；
2. **data-as-JS**：`report-data.js` 定义 `window.TONGTU_REPORT`，`zh.pdf` 的 base64 能
   原样解回字节（file:// 下 fetch 被拒，这是唯一能把 PDF 喂给 PDF.js 的办法）；
3. **两条加载路径都在**：http(s) 下走相对路径 fetch，file:// 下走内嵌 base64；
4. **红线**：模板里写着「需 server / LLM 的功能永不添加」，且生成器本身不联网。
"""

from __future__ import annotations

import base64
import json
import re

import pytest

from tongtu import CONTRACT_VERSION, report_page
from tongtu.cli import run_preview

PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Page /MediaBox [0 0 612 792] >>\nendobj\n%%EOF\n"

REPORT = {
    "contract_version": CONTRACT_VERSION,
    "paper": {"arxiv_id": "2401.00001", "title": "On Placeholders"},
    "status": "ok_with_fallback",
    "validation": {"chunks_total": 3, "fallback": 1},
    "compile": {"passed": True, "engine": "xelatex", "inject": {"branch": "inject"}},
    "fallbacks": [{"chunk_id": "c002", "reason": "compile_failed"}],
    "artifacts": [],
}

ANCHORS = {
    "contract_version": CONTRACT_VERSION,
    "pdf": {"path": "zh.pdf", "page_count": 1},
    "coordinate_system": {"origin": "top-left", "unit": "pt"},
    "anchors": [
        {
            "id": "BLK-1",
            "type": "equation",
            "page": 1,
            "rects": [{"x": 72.0, "y": 100.0, "w": 400.0, "h": 20.0}],
            "block_id": "BLK-1",
            "source": "synctex",
            "confidence": 0.9,
        }
    ],
}

BLOCKS = {
    "blocks": [
        {
            "id": "BLK-1",
            "placeholder": "⟦BLK-1⟧",
            "category": "math",
            "environment": "equation",
            "label": "eq:e",
            "tex": "\\begin{equation}E = mc^2\\end{equation}",
        }
    ],
    "captions": [
        {"id": "CAP-0", "block_id": "BLK-1", "text": "能量公式", "stream_text": "能量公式"}
    ],
}

FIGURES = {"contract_version": CONTRACT_VERSION, "figures": []}

#: vendor 里允许出现的 URL 主机。一个都不是页面会去请求的地址——它们是 **XML 命名空间
#: 标识符**（SVG / XMP / XFA 里 `xmlns=` 的取值，字符串常量而已）与 license 抬头，外加
#: PDF 内**链接注解**的规范化模板（`http://${t}`：把 `www.x` 补成可点的 URL，页面自己
#: 从不 fetch 它）。真要发请求的代码走的是 fetch / XHR / Worker，那三样在下面另有断言。
VENDOR_URL_HOSTS = (
    "www.w3.org",
    "ns.adobe.com",
    "www.xfa.org",
    "www.apache.org",
    "github.com",
    "${t}",
    "${e}",
)

_URL_RE = re.compile(r"https?://[^\s\"'`)<>]+")


@pytest.fixture
def page(tmp_path) -> report_page.PageResult:
    pdf = tmp_path / "zh.pdf"
    pdf.write_bytes(PDF)
    return report_page.render(
        tmp_path / "out",
        report=REPORT,
        anchors=ANCHORS,
        blocks=BLOCKS,
        figures=FIGURES,
        pdf=pdf,
        title="2401.00001",
    )


def payload(result: report_page.PageResult) -> dict:
    """从 `report-data.js` 里把 `window.TONGTU_REPORT` 那个对象抠出来。"""
    text = result.data.read_text(encoding="utf-8")
    body = text.split("window.TONGTU_REPORT = ", 1)[1].rstrip().removesuffix(";")
    return json.loads(body)


# ----------------------------------------------------------------- 产物形态


def test_render_writes_page_data_and_vendor(page):
    assert page.page.name == "report.html"
    assert page.data.name == "report-data.js"
    html = page.page.read_text(encoding="utf-8")

    assert '<script src="vendor/pdfjs/pdf.min.js"></script>' in html
    assert '<script src="report-data.js"></script>' in html
    assert (page.page.parent / "vendor" / "pdfjs" / "pdf.min.js").is_file()
    assert (page.page.parent / "vendor" / "pdfjs" / "pdf.worker.min.js").is_file()
    assert (page.page.parent / "vendor" / "pdfjs" / "LICENSE").is_file(), "license 随包"
    # CSS 与自家 JS 一律内联（少两个外部文件，也少两条可能失败的加载）
    assert "__STYLE__" not in html and "__APP__" not in html
    assert ".hot {" in html and "window.TONGTU_REPORT" in html


def test_data_js_round_trips_the_pdf(page):
    data = payload(page)

    assert data["pdf"]["name"] == "zh.pdf"
    assert base64.b64decode(data["pdf"]["base64"]) == PDF
    assert data["pdf"]["bytes"] == len(PDF)
    assert page.pdf_bytes == len(PDF)


def test_data_js_carries_what_the_page_needs(page):
    data = payload(page)

    assert data["contract_version"] == CONTRACT_VERSION
    assert data["report"]["status"] == "ok_with_fallback"
    assert data["anchors"]["anchors"][0]["id"] == "BLK-1"
    # 热区点开要看原始 TeX（架构 §11），故摘要必须带 tex
    assert data["blocks"]["BLK-1"]["tex"] == "\\begin{equation}E = mc^2\\end{equation}"
    assert data["blocks"]["BLK-1"]["captions"] == ["能量公式"]
    assert data["figures"] == FIGURES


def test_block_summary_truncates_giant_blocks():
    blocks = {"blocks": [{"id": "BLK-1", "tex": "x" * 500, "category": "tikz"}]}

    summary = report_page.block_summary(blocks, limit=100)

    assert summary["BLK-1"]["tex"].startswith("x" * 100)
    assert "已截断" in summary["BLK-1"]["tex"]


def test_render_without_a_pdf_still_produces_a_page(tmp_path):
    result = report_page.render(
        tmp_path / "out", report=REPORT, anchors=ANCHORS, pdf=None
    )

    assert result.page.is_file()
    assert result.pdf_bytes == 0
    assert any("zh.pdf" in w for w in result.warnings)


# ------------------------------------------------------------------- 零外链


def _urls(text: str) -> set[str]:
    return set(_URL_RE.findall(text))


def test_our_own_files_have_no_external_links(page):
    """模板与产物里一个外链都不许有——检验页要在没网的机器上双击即开。"""
    root = report_page.assets_dir()
    ours = [
        root / "page.html",
        root / "style.css",
        root / "app.js",
        page.page,
        page.data,
    ]
    for path in ours:
        assert _urls(path.read_text(encoding="utf-8")) == set(), path.name


def test_vendor_urls_are_only_namespaces_and_licenses():
    """vendor 是第三方代码，只核对它没有会真发请求的地址（命名空间 / license 除外）。"""
    vendor = report_page.assets_dir() / "vendor" / "pdfjs"
    assert list(vendor.glob("*.js")), "vendor 里应当有 PDF.js"
    for path in sorted(vendor.glob("*.js")):
        for url in _urls(path.read_text(encoding="utf-8", errors="replace")):
            host = url.split("//", 1)[1].split("/", 1)[0]
            assert host in VENDOR_URL_HOSTS, (path.name, url)


def test_the_page_only_ever_touches_relative_paths(page):
    """自家代码里的每一处取资源都必须是相对路径（`vendor/…` / `zh.pdf` / `figures/…`）。"""
    app = (report_page.assets_dir() / "app.js").read_text(encoding="utf-8")

    for call in re.findall(r"(?:fetch|loadScript)\(([^)]*)\)", app):
        assert "http" not in call, call
    assert 'VENDOR = "vendor/pdfjs/"' in app
    assert "XMLHttpRequest" not in app and "WebSocket" not in app


def test_both_pdf_loading_paths_exist(page):
    """file:// 走内嵌 base64、http(s) 走相对路径 fetch——两条都要在页面里。"""
    html = page.page.read_text(encoding="utf-8")

    assert 'location.protocol === "file:"' in html
    assert "fetch(name)" in html
    assert "base64ToBytes(embedded)" in html


def test_the_red_line_is_written_into_the_template():
    """架构 §11 的红线：需 server / LLM 的功能永不添加——写进模板注释，改的人看得见。"""
    html = (report_page.assets_dir() / "page.html").read_text(encoding="utf-8")
    app = (report_page.assets_dir() / "app.js").read_text(encoding="utf-8")

    assert "永不添加" in html and "永不添加" in app
    assert "CSP" in html


def test_assets_hash_changes_with_the_template(tmp_path, monkeypatch):
    """模板资产的内容 hash 进 export 的输入 hash：改模板就该重新出页面（架构 §4）。"""
    first = report_page.assets_hash()

    assert len(first) == 64
    assert first == report_page.assets_hash(), "同样的资产，同样的 hash"


# ------------------------------------------------------------------- preview


class _Args:
    def __init__(self, **kwargs):
        self.__dict__.update({"id": "2401.00001", "workdir": None, "serve": False})
        self.__dict__.update(kwargs)


def test_preview_opens_the_page(tmp_path, page, capsys):
    """`tongtu preview`：打开 `out/report.html`。"""
    opened: list[str] = []

    code = run_preview(
        _Args(workdir=str(tmp_path)), opener=lambda url: opened.append(url) or True
    )

    assert code == 0
    assert opened and opened[0].startswith("file://") and opened[0].endswith("report.html")
    assert opened[0] in capsys.readouterr().out


def test_preview_prints_the_path_when_there_is_no_browser(tmp_path, page, capsys):
    """headless 环境（容器、SSH）打不开浏览器 → 打印路径并退 0，不算失败。"""
    code = run_preview(_Args(workdir=str(tmp_path)), opener=lambda url: False)

    assert code == 0
    out = capsys.readouterr().out
    assert "report.html" in out and "打不开浏览器" in out


def test_preview_survives_a_browser_that_raises(tmp_path, page, capsys):
    def boom(url):
        raise RuntimeError("no display")

    assert run_preview(_Args(workdir=str(tmp_path)), opener=boom) == 0
    assert "report.html" in capsys.readouterr().out


def test_preview_without_a_package_fails(tmp_path, capsys):
    code = run_preview(_Args(workdir=str(tmp_path / "empty")), opener=lambda url: True)

    assert code == 1
    assert "tongtu run" in capsys.readouterr().err


def test_preview_serve_starts_a_local_server(tmp_path, page, capsys):
    """`--serve`：起本地 http.server（http 下页面走相对路径读 zh.pdf）。"""
    served: dict = {}

    class FakeServer:
        server_address = ("127.0.0.1", 8765)

        def serve_forever(self):
            served["ran"] = True
            raise KeyboardInterrupt

        def server_close(self):
            served["closed"] = True

    urls: list[str] = []
    code = run_preview(
        _Args(workdir=str(tmp_path), serve=True),
        opener=lambda url: urls.append(url) or True,
        server=FakeServer,
    )

    assert code == 0
    assert served == {"ran": True, "closed": True}
    assert urls == ["http://127.0.0.1:8765/report.html"]
    assert "http://127.0.0.1:8765/report.html" in capsys.readouterr().out
