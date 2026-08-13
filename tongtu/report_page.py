"""静态检验页生成器（架构 §11、决策 8）。

`report.html` 有三重角色：**产物契约的第一个消费者**（文枢动工前先在这儿暴露契约缺陷）、
**零期验收工具**（anchors 热区画不出来就无法验收）、**开源 README 的 demo**。

本模块只做一件事：把 `tongtu/data/report_page/` 里的模板资产 + 一份产物数据，落成
`out/report.html` + `out/report-data.js` + `out/vendor/pdfjs/`。**不联网**——PDF.js 是
一次性 vendor 进仓库的静态资产（见 `vendor/pdfjs/VERSION`），生成器只负责拷贝。

## 为什么数据要走 data-as-JS

页面必须能双击打开（`file://`）。而 `file://` 下 `fetch` / `XHR` 读同目录文件被浏览器
一律拒绝，ESM 脚本也因 CORS 加载不了。故：

* 页面用**经典脚本**（非 module）；
* 数据落成 `report-data.js`（`window.TONGTU_REPORT = {...}`），其中 `zh.pdf` 以 base64
  内嵌，PDF.js 从 `Uint8Array` 加载——体积 +33% 是有意付出的代价；
* 同时保留 http(s) 下的相对路径 `fetch("zh.pdf")` 快路径（`app.js` 两条路都实现了）。

## 红线

需要服务端或 LLM 调用的功能一律归文枢，本页永不添加（架构 §11）。生成器这一侧的对应
纪律是：只读产物包内已有的文件，不发任何请求。
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Mapping

from . import CONTRACT_VERSION

__all__ = [
    "ASSETS_DIRNAME",
    "BLOCK_TEX_LIMIT",
    "DATA_NAME",
    "PAGE_NAME",
    "PAGE_TEMPLATE",
    "VENDOR_DIRNAME",
    "PageError",
    "PageResult",
    "assets_dir",
    "assets_hash",
    "block_summary",
    "render",
    "render_data",
]


class PageError(RuntimeError):
    """模板资产不可用（包坏了）。检验页出不来是 export 的失败，不是论文的问题。"""


#: 模板资产目录（包内；wheel 与源码树同一路径，故不需要 `find_fonts` 那样的双链查找）。
ASSETS_DIRNAME = "data/report_page"

#: 页面模板与产物文件名。
PAGE_TEMPLATE = "page.html"
PAGE_NAME = "report.html"
DATA_NAME = "report-data.js"
VENDOR_DIRNAME = "vendor"

#: 单个块塞进检验页的原始 TeX 上限。热区点开是给人看一眼源码的，不是发布代码浏览器；
#: 一个几万字符的 tikz 块会把 `report-data.js` 撑大而没人真去读完。
BLOCK_TEX_LIMIT = 20000


def assets_dir() -> Path:
    """定位模板资产目录。"""
    try:
        path = Path(str(files("tongtu").joinpath(ASSETS_DIRNAME)))
    except (ModuleNotFoundError, TypeError, OSError):
        path = Path(__file__).resolve().parent / ASSETS_DIRNAME
    if not (path / PAGE_TEMPLATE).is_file():
        fallback = Path(__file__).resolve().parent / ASSETS_DIRNAME
        if (fallback / PAGE_TEMPLATE).is_file():
            return fallback
        raise PageError(f"找不到检验页模板资产：{path}/{PAGE_TEMPLATE}")
    return path


def assets_hash() -> str:
    """模板资产（含 vendor）的内容 hash——改模板即失效 export（架构 §4）。"""
    root = assets_dir()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


@dataclass(frozen=True)
class PageResult:
    """一次检验页生成的结果。"""

    page: Path
    data: Path
    vendor: tuple[Path, ...] = ()
    pdf_bytes: int = 0
    data_bytes: int = 0
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict:
        data: dict = {
            "page": self.page.name,
            "data": self.data.name,
            "vendor": [p.name for p in self.vendor],
            "pdf_bytes": self.pdf_bytes,
            "data_bytes": self.data_bytes,
        }
        if self.warnings:
            data["warnings"] = list(self.warnings)
        return data


def block_summary(blocks: Mapping | None, *, limit: int = BLOCK_TEX_LIMIT) -> dict:
    """blocks.json → `{块 id: {category, environment, label, tex, captions}}`。

    热区点开要看的是**原始 TeX**（架构 §11），故这份摘要必须带 `tex`；其余字段只留检验页
    真用得上的那几个，span / placeholder 之类不进页面。
    """
    summary: dict[str, dict] = {}
    if not isinstance(blocks, Mapping):
        return summary
    captions_by_block: dict[str, list[str]] = {}
    for caption in blocks.get("captions", ()) or ():
        if not isinstance(caption, Mapping):
            continue
        block_id = str(caption.get("block_id") or "")
        text = str(caption.get("text") or "").strip()
        if block_id and text:
            captions_by_block.setdefault(block_id, []).append(text)
    for block in blocks.get("blocks", ()) or ():
        if not isinstance(block, Mapping):
            continue
        block_id = str(block.get("id") or "")
        if not block_id:
            continue
        tex = str(block.get("tex") or "")
        truncated = len(tex) > limit
        summary[block_id] = {
            "category": block.get("category"),
            "environment": block.get("environment"),
            "label": block.get("label"),
            "tex": tex[:limit] + ("\n…（已截断）" if truncated else ""),
            "captions": captions_by_block.get(block_id, []),
        }
    return summary


def render_data(
    *,
    report: Mapping,
    anchors: Mapping,
    blocks: Mapping | None,
    figures: Mapping | None,
    pdf: Path | None,
    pdf_name: str = "zh.pdf",
) -> tuple[str, int]:
    """组装 `report-data.js` 的正文，返回 `(文本, PDF 字节数)`。"""
    payload: dict = {
        "contract_version": CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "report": dict(report),
        "anchors": dict(anchors),
        "blocks": block_summary(blocks),
        "figures": dict(figures) if isinstance(figures, Mapping) else {"figures": []},
        "pdf": {"name": pdf_name, "base64": "", "bytes": 0},
    }
    raw = b""
    if pdf is not None and Path(pdf).is_file():
        raw = Path(pdf).read_bytes()
        payload["pdf"] = {
            "name": pdf_name,
            "base64": base64.b64encode(raw).decode("ascii"),
            "bytes": len(raw),
        }
    body = json.dumps(payload, ensure_ascii=False)
    text = (
        "/* 通途检验页数据（export 自动生成，勿手改）。\n"
        " * 之所以是 .js 而不是 .json：file:// 下 fetch 同目录文件被浏览器拒绝，\n"
        " * 而 <script src> 不受此限——这是「双击即可打开」的唯一办法。 */\n"
        "window.TONGTU_REPORT = " + body + ";\n"
    )
    return text, len(raw)


def render(
    out_dir: str | Path,
    *,
    report: Mapping,
    anchors: Mapping,
    blocks: Mapping | None = None,
    figures: Mapping | None = None,
    pdf: str | Path | None = None,
    pdf_name: str = "zh.pdf",
    title: str = "",
) -> PageResult:
    """生成 `report.html` + `report-data.js` + `vendor/`（全部落在 `out_dir` 下）。"""
    root = assets_dir()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    template = (root / PAGE_TEMPLATE).read_text(encoding="utf-8")
    style = (root / "style.css").read_text(encoding="utf-8")
    app = (root / "app.js").read_text(encoding="utf-8")
    page = (
        template.replace("__STYLE__", style)
        .replace("__APP__", app)
        .replace("__TITLE__", title or "产物包")
    )

    data_text, pdf_bytes = render_data(
        report=report,
        anchors=anchors,
        blocks=blocks,
        figures=figures,
        pdf=Path(pdf) if pdf is not None else None,
        pdf_name=pdf_name,
    )
    if not pdf_bytes:
        warnings.append("没有 zh.pdf 可内嵌，检验页只剩侧栏（PDF 区会显示加载失败）")

    page_path = out / PAGE_NAME
    data_path = out / DATA_NAME
    page_path.write_text(page, encoding="utf-8")
    data_path.write_text(data_text, encoding="utf-8")

    vendor_src = root / VENDOR_DIRNAME
    vendor_out = out / VENDOR_DIRNAME
    copied: list[Path] = []
    if vendor_src.is_dir():
        for path in sorted(vendor_src.rglob("*")):
            if not path.is_file():
                continue
            target = vendor_out / path.relative_to(vendor_src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            copied.append(target)
    else:  # pragma: no cover - 包坏了才会走到
        warnings.append("包内没有 vendor/（PDF.js），检验页渲染不出 PDF")

    return PageResult(
        page=page_path,
        data=data_path,
        vendor=tuple(copied),
        pdf_bytes=pdf_bytes,
        data_bytes=len(data_text.encode("utf-8")),
        warnings=tuple(warnings),
    )
