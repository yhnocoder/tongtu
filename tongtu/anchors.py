"""anchors 合成：synctex / blocks / pdf-scan 三来源叠加成交互地图（架构 §7、§11）。

`anchors.json` 是产物包里唯一「把源码结构与排版结果对上」的文件：公式、图、表、章节在
`zh.pdf` 里的页码与矩形区域。检验页拿它画热区（不画出来零期就无法验收，架构 §11），
文枢 read path 拿它做定位。

## 三个来源各知道什么

| 来源 | 知道 | 不知道 |
|---|---|---|
| `synctex` | 源码行 → 页码 + 精确矩形（xelatex 编译时一并产出的映射） | 这一行是什么东西 |
| `blocks` | 每个块的类型 / label / caption / 所属翻译块 | 它排到了第几页 |
| `pdf-scan` | 有几页、每页多大 | 别的都不知道 |

合成即「blocks 给语义、synctex 给坐标、pdf-scan 给画布」。**synctex 缺席不是错误**：
没跑 `-synctex=1`、编译器是假的、或 latexmk 清掉了中间文件都会让它缺席，此时退化为
**页级锚点**——矩形取整页，`source` 如实记 `blocks`、`confidence` 相应压低，检验页据此
画虚线热区。宁可标注「大概在这一页」，也不许伪造一个精确矩形。

## 叠加次序与容差是开放问题 4

架构附录 B 开放问题 4 写着「anchors 三来源叠加的实现次序与热区容差：零期拿真实论文实测
后定」。故本模块把它们全部收敛成模块级常量（:data:`SOURCE_PRIORITY`、
:data:`RECT_PADDING_PT`、:data:`BAND_MERGE_TOLERANCE_PT`、:data:`SYNCTEX_SCALE`），
标注「真实论文实测后定」，改一个数即可重新校准，不必翻遍实现。

## 坐标系

`anchors.schema.json` 约定 `origin=top-left`、`unit=pt`——与 PDF.js 视口一致，检验页拿到
矩形乘一个缩放比例就能画。synctex 自己的坐标正是「相对页面左上角、向下为正」的 sp
（TeX 小点），故换算只是一个常数因子；矩形由「基线 + 高 + 深」还原成上边沿矩形。
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import CONTRACT_VERSION
from .stages.mask import Block, Caption, MaskResult
from .texlex import line_number, line_starts, read_group

__all__ = [
    "BAND_MERGE_TOLERANCE_PT",
    "BLOCK_TYPES",
    "CONFIDENCE",
    "DEFAULT_PAGE_SIZE",
    "MAX_RECTS_PER_ANCHOR",
    "RECT_PADDING_PT",
    "SECTION_LEVELS",
    "SKIP_CATEGORIES",
    "SOURCE_PRIORITY",
    "SYNCTEX_SCALE",
    "Anchor",
    "AnchorsResult",
    "PdfInfo",
    "Rect",
    "Section",
    "SyncTexMap",
    "SyncTexRecord",
    "block_line_spans",
    "build",
    "line_of",
    "parse_synctex",
    "scan_pdf",
    "sections_in",
]


# --------------------------------------------------------------------- 常量

#: 三来源的叠加次序：靠前者给出的坐标胜出（**开放问题 4，真实论文实测后定**）。
#: 现在的次序就是精度次序——synctex 是编译器自己吐的映射，blocks 只能给页级估计，
#: pdf-scan 只提供画布尺寸、从不单独产出锚点。
SOURCE_PRIORITY: tuple[str, ...] = ("synctex", "blocks", "pdf-scan")

#: 热区外扩容差（pt）。**开放问题 4，真实论文实测后定**：synctex 的盒子紧贴基线，
#: 上下不含行距，直接画出来会显得「压着字」。
RECT_PADDING_PT = 2.0

#: 同页矩形合并成一条「行带」的纵向容差（pt）。**开放问题 4，真实论文实测后定**。
BAND_MERGE_TOLERANCE_PT = 3.0

#: 一个锚点最多留几个矩形；超出即并成一个包围盒（热区不是排版，够点就行）。
MAX_RECTS_PER_ANCHOR = 12

#: synctex 坐标 → PDF pt 的换算因子。**开放问题 4 的一部分**：synctex 写的是 TeX 小点
#: （1 TeX pt = 65536 sp，1 in = 72.27 TeX pt），而 PDF 用大点（1 in = 72 bp）。
SP_PER_TEX_PT = 65536.0
TEX_PT_PER_INCH = 72.27
PDF_PT_PER_INCH = 72.0
SYNCTEX_SCALE = (PDF_PT_PER_INCH / TEX_PT_PER_INCH) / SP_PER_TEX_PT

#: 读不出页面尺寸时的兜底画布（US Letter，pt）。只影响页级锚点画多大，不影响定位。
DEFAULT_PAGE_SIZE: tuple[float, float] = (612.0, 792.0)

#: 各来源的置信度（检验页据此画实线 / 虚线热区）。
CONFIDENCE: dict[str, float] = {
    "synctex": 0.9,
    "merged": 0.95,
    "blocks": 0.2,  # 页级锚点：知道是什么，不知道在哪
    "pdf-scan": 0.1,
}

#: blocks.json 的块分类 → `anchors.schema.json` 的锚点类型。
BLOCK_TYPES: dict[str, str] = {
    "math": "equation",
    "figure": "figure",
    "table": "table",
    "algorithm": "algorithm",
    "theorem": "block",
    "code": "block",
    "tikz": "figure",
    "other": "block",
    "unknown": "block",
}

#: 不产锚点的块分类：前导区不排版，注释块根本不进 PDF。
SKIP_CATEGORIES: frozenset[str] = frozenset({"preamble", "comment"})

#: 章节命令 → 层级（数字只用于排序与 id，不进产物）。
SECTION_LEVELS: dict[str, int] = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
}

_SECTION_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{"
)

#: 粗清理：把标题里的控制序列与花括号剥掉（只为侧栏可读，不追求 TeX 语义）。
_COMMAND_RE = re.compile(r"\\[A-Za-z@]+\*?|[{}]|\\[^A-Za-z]")
_WS_RE = re.compile(r"\s+")


# ------------------------------------------------------------------ 基本形状


@dataclass(frozen=True)
class Rect:
    """页内矩形（pt，原点在页面左上角，y 向下）。"""

    x: float
    y: float
    w: float
    h: float

    def padded(self, amount: float) -> "Rect":
        return Rect(self.x - amount, self.y - amount, self.w + 2 * amount, self.h + 2 * amount)

    def union(self, other: "Rect") -> "Rect":
        x0 = min(self.x, other.x)
        y0 = min(self.y, other.y)
        x1 = max(self.x + self.w, other.x + other.w)
        y1 = max(self.y + self.h, other.y + other.h)
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def clamp(self, width: float, height: float) -> "Rect":
        """裁进页面。热区跑到纸外没有意义，也会让检验页的覆盖层长歪。"""
        x0 = min(max(self.x, 0.0), width)
        y0 = min(max(self.y, 0.0), height)
        x1 = min(max(self.x + self.w, 0.0), width)
        y1 = min(max(self.y + self.h, 0.0), height)
        return Rect(x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))

    def to_json(self) -> dict:
        return {
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "w": round(self.w, 2),
            "h": round(self.h, 2),
        }


@dataclass(frozen=True)
class Anchor:
    """一条锚点，字段与 `anchors.schema.json` 的 `anchors[]` 一一对应。"""

    id: str
    type: str
    page: int
    rects: tuple[Rect, ...]
    label: str | None = None
    number: str | None = None
    title: str | None = None
    block_id: str | None = None
    chunk_id: str | None = None
    source: str = "blocks"
    confidence: float = 0.0

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "number": self.number,
            "title": self.title,
            "block_id": self.block_id,
            "chunk_id": self.chunk_id,
            "page": self.page,
            "rects": [r.to_json() for r in self.rects],
            "source": self.source,
            "confidence": round(self.confidence, 3),
        }


@dataclass(frozen=True)
class PdfInfo:
    """pdf-scan 来源的全部产出：有几页、每页多大。"""

    page_count: int = 1
    pages: tuple[tuple[float, float], ...] = ()
    parsed: bool = False
    """真从 PDF 里读出来了吗（假编译器 / 对象流压缩时为 False，尺寸即兜底值）。"""

    def size(self, page: int) -> tuple[float, float]:
        if 1 <= page <= len(self.pages):
            return self.pages[page - 1]
        return self.pages[0] if self.pages else DEFAULT_PAGE_SIZE

    def to_json(self, path: str) -> dict:
        data: dict = {"path": path, "page_count": self.page_count}
        if self.pages:
            data["pages"] = [
                {"page": i + 1, "width": round(w, 2), "height": round(h, 2)}
                for i, (w, h) in enumerate(self.pages)
            ]
        return data


# ------------------------------------------------------------------ pdf-scan

_MEDIABOX_RE = re.compile(
    rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]"
)
_PAGE_RE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
_COUNT_RE = re.compile(rb"/Count\s+(\d+)")


def scan_pdf(source: str | Path | bytes) -> PdfInfo:
    """零依赖读 PDF 的页数与每页 MediaBox（pdf-scan 来源）。

    只做**够用**的解析：`/Type /Page` 计数与 `/MediaBox` 抓取，页数与 MediaBox 条数对得
    上就逐页配对，对不上就全部用第一个。对象流（xref stream）会把这些藏进压缩流里，那时
    一个都抓不到——如实退回单页兜底并把 `parsed` 记成 False，不猜。

    真正的定位精度从来不指望这一层（那是 synctex 的活），本层只提供画布尺寸。
    """
    if isinstance(source, (str, Path)):
        try:
            data = Path(source).read_bytes()
        except OSError:
            return PdfInfo()
    else:
        data = bytes(source)
    if not data:
        return PdfInfo()

    boxes: list[tuple[float, float]] = []
    for match in _MEDIABOX_RE.finditer(data):
        x0, y0, x1, y1 = (float(v) for v in match.groups())
        width, height = abs(x1 - x0), abs(y1 - y0)
        if width > 0 and height > 0:
            boxes.append((width, height))

    pages = len(_PAGE_RE.findall(data))
    counts = [int(m.group(1)) for m in _COUNT_RE.finditer(data)]
    if counts:
        pages = max([pages, *counts])
    if pages <= 0:
        pages = len(boxes) or 1

    if not boxes:
        return PdfInfo(page_count=pages, pages=(), parsed=False)
    if len(boxes) == pages:
        return PdfInfo(page_count=pages, pages=tuple(boxes), parsed=True)
    return PdfInfo(page_count=pages, pages=tuple([boxes[0]] * pages), parsed=True)


# ------------------------------------------------------------------- synctex

#: 一条盒子记录：`<kind><tag>,<line>[,<column>]:<x>,<y>:<w>,<h>,<d>`。
#: 只收带完整尺寸的四种（`[` vbox、`(` hbox、`v`/`h` 空盒），glue / kern / math 那几种
#: 只有一个点、画不出矩形，跳过。
_RECORD_RE = re.compile(
    r"^([\[(hv])(\d+),(\d+)(?:,\d+)?:(-?\d+),(-?\d+):(-?\d+),(-?\d+),(-?\d+)"
)
_INPUT_RE = re.compile(r"^Input:(\d+):(.*)$")
_SHEET_RE = re.compile(r"^\{(\d+)$")
_HEADER_NUM_RE = re.compile(r"^(Unit|X Offset|Y Offset|Magnification):\s*([-\d.]+)")


@dataclass(frozen=True)
class SyncTexRecord:
    """synctex 里的一个盒子：某个输入文件的某一行，排到了第几页的哪个矩形。"""

    tag: int
    line: int
    page: int
    rect: Rect


@dataclass(frozen=True)
class SyncTexMap:
    """一份解析好的 `zh.synctex.gz`。"""

    inputs: Mapping[int, str] = field(default_factory=dict)
    records: tuple[SyncTexRecord, ...] = ()
    unit: float = 1.0
    x_offset: float = 0.0
    y_offset: float = 0.0
    version: str = ""
    warnings: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.records)

    def tag_for(self, name: str) -> int | None:
        """按文件名找 tag（synctex 记的路径可能是相对、绝对或带 `./` 前缀）。"""
        target = Path(name).name
        for tag, path in sorted(self.inputs.items()):
            if Path(path.strip()).name == target:
                return tag
        return None

    def rects_for(self, tag: int, line_start: int, line_end: int) -> dict[int, list[Rect]]:
        """某个输入文件的一段行区间 → `{页码: [矩形…]}`（尚未合并与外扩）。"""
        found: dict[int, list[Rect]] = {}
        for record in self.records:
            if record.tag != tag or not (line_start <= record.line <= line_end):
                continue
            found.setdefault(record.page, []).append(record.rect)
        return found


def _read_synctex(source: str | Path | bytes) -> str:
    if isinstance(source, (str, Path)):
        data = Path(source).read_bytes()
    else:
        data = bytes(source)
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:  # 截断的 .gz
            raise ValueError(f"synctex 解压失败：{exc}") from exc
    return data.decode("utf-8", errors="replace")


def parse_synctex(source: str | Path | bytes) -> SyncTexMap:
    """零依赖解析 `zh.synctex.gz`（gzip + 文本记录格式）。

    文件分两段：头部（`SyncTeX Version` / `Input:<tag>:<path>` / `Unit` / `X Offset` /
    `Y Offset` / `Magnification`）与 `Content:` 之后的页面记录。页面以 `{<页码>` 开始、
    `}<页码>` 结束，其间每行是一条盒子记录。

    解析纪律与本仓库其余解析器一致：**认不出的行一律跳过**，绝不因为一条怪记录让整份映射
    作废——最坏的后果只是少几个精确热区，退化为页级锚点。
    """
    text = _read_synctex(source)
    inputs: dict[int, str] = {}
    records: list[SyncTexRecord] = []
    warnings: list[str] = []
    unit, x_offset, y_offset = 1.0, 0.0, 0.0
    version = ""
    page: int | None = None
    in_content = False

    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        if line.startswith("SyncTeX Version:"):
            version = line.split(":", 1)[1].strip()
            continue
        match = _INPUT_RE.match(line)
        if match is not None:
            inputs[int(match.group(1))] = match.group(2)
            continue
        if not in_content:
            match = _HEADER_NUM_RE.match(line)
            if match is not None:
                value = float(match.group(2))
                if match.group(1) == "Unit":
                    unit = value or 1.0
                elif match.group(1) == "X Offset":
                    x_offset = value
                elif match.group(1) == "Y Offset":
                    y_offset = value
                continue
            if line.startswith("Content:"):
                in_content = True
                continue
            continue
        match = _SHEET_RE.match(line)
        if match is not None:
            page = int(match.group(1))
            continue
        if line.startswith("}"):
            page = None
            continue
        if page is None:
            continue  # `Postamble` 之类的收尾段
        match = _RECORD_RE.match(line)
        if match is None:
            continue
        kind, tag, src_line, x, y, w, h, d = match.groups()
        del kind
        scale = SYNCTEX_SCALE * unit
        left = (int(x) + x_offset) * scale
        baseline = (int(y) + y_offset) * scale
        width = int(w) * scale
        height = int(h) * scale
        depth = int(d) * scale
        # synctex 的 (x, y) 是盒子左边沿与**基线**；矩形上边沿 = 基线 - 高，
        # 下边沿 = 基线 + 深（origin=top-left，y 向下）。
        rect = Rect(x=left, y=baseline - height, w=width, h=height + depth)
        records.append(
            SyncTexRecord(tag=int(tag), line=int(src_line), page=page, rect=rect)
        )

    if not records and in_content:
        warnings.append("synctex 里没有可用的盒子记录（版本或格式不认识）")
    return SyncTexMap(
        inputs=inputs,
        records=tuple(records),
        unit=unit,
        x_offset=x_offset,
        y_offset=y_offset,
        version=version,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------- 源码位置 → 行号

def line_of(starts: Sequence[int], offset: int) -> int:
    """字符偏移 → 1-based 行号（薄封装 :func:`tongtu.texlex.line_number`）。"""
    return line_number(starts, max(0, offset))


def _needle(block: Block) -> str:
    """块 tex 里最长的一段「不含 CAP 占位符」的文本——用它在 zh.tex 里定位。

    caption 被翻译之后块 tex 不再逐字节出现在 zh.tex 里（`⟦CAP-k⟧` 换成了译文），故不能
    直接找整块；最长的无 CAP 片段既保留了 `\\begin{figure}` 这类结构、又必然逐字节存在。
    """
    if "⟦CAP-" not in block.tex:
        return block.tex
    parts = re.split(r"⟦CAP-\d+⟧", block.tex)
    return max(parts, key=len) if parts else block.tex


def block_line_spans(text: str, blocks: Iterable[Block]) -> dict[str, tuple[int, int]]:
    """`{块 id: (起始行, 结束行)}`——块在 **zh.tex** 里的行区间（1-based，闭区间）。

    blocks.json 记的 `span` 是块在 `flat.tex` 里的位置，而 synctex 认的是 `zh.tex` 的行号；
    两者之间隔着回填（caption 译文长度可变）与注入（导言区多出一块），偏移量不是常数。
    与其维护一张脆弱的偏移表，不如**按文档顺序在 zh.tex 里顺序查找**：块内容本就逐字节
    存在（caption 除外，见 :func:`_needle`），顺序查找还顺带保证了「后一个块不会定位到前
    一个块之前」。找不到的块直接缺席——少一个精确热区，不损坏任何东西。
    """
    starts = line_starts(text)
    spans: dict[str, tuple[int, int]] = {}
    cursor = 0
    for block in blocks:
        needle = _needle(block)
        if not needle.strip():
            continue
        position = text.find(needle, cursor)
        if position < 0:
            position = text.find(needle)  # 顺序被打乱（重排？）时退回全局查找
            if position < 0:
                continue
        head = block.tex.split(needle, 1)[0] if needle in block.tex else ""
        tail = block.tex[len(head) + len(needle) :] if needle in block.tex else ""
        first = line_of(starts, position) - head.count("\n")
        last = line_of(starts, position + max(0, len(needle) - 1)) + tail.count("\n")
        spans[block.id] = (max(1, first), max(1, last))
        cursor = position + len(needle)
    return spans


@dataclass(frozen=True)
class Section:
    """zh.tex 里的一个章节命令（章节锚点的语义来源）。"""

    id: str
    level: int
    command: str
    title: str
    line: int


def clean_title(text: str, limit: int = 120) -> str:
    """把 TeX 标题粗清理成可读文本（侧栏与 tooltip 用）。"""
    plain = _WS_RE.sub(" ", _COMMAND_RE.sub(" ", text)).strip()
    return plain if len(plain) <= limit else plain[: limit - 1].rstrip() + "…"


def sections_in(text: str) -> tuple[Section, ...]:
    """扫 zh.tex 里的 `\\section` 家族，取标题与行号。

    章节不是掩码块（散文留在流里），blocks.json 里没有它们，可它们恰恰是检验页导航最有用
    的一类锚点，故单独扫一遍。用词法工具读平衡花括号，不用正则啃嵌套。
    """
    starts = line_starts(text)
    found: list[Section] = []
    for match in _SECTION_RE.finditer(text):
        group = read_group(text, match.end() - 1)
        if group is None:
            continue
        title = clean_title(group[0])
        command = match.group(1)
        found.append(
            Section(
                id=f"sec-{len(found) + 1:03d}",
                level=SECTION_LEVELS.get(command, 2),
                command=command,
                title=title,
                line=line_of(starts, match.start()),
            )
        )
    return tuple(found)


# --------------------------------------------------------------------- 合成


def _merge_bands(rects: Sequence[Rect], tolerance: float = BAND_MERGE_TOLERANCE_PT) -> list[Rect]:
    """把同页的一堆盒子并成若干「行带」。

    synctex 一行正文能吐出十几个盒子，逐个画热区既难看又难点。纵向重叠（含容差）的并成
    一条带，取包围盒——对公式、图、表这类块，结果就是那个块的外框。
    """
    ordered = sorted((r for r in rects if r.w > 0 and r.h > 0), key=lambda r: (r.y, r.x))
    bands: list[Rect] = []
    for rect in ordered:
        if bands and rect.y <= bands[-1].y + bands[-1].h + tolerance:
            bands[-1] = bands[-1].union(rect)
        else:
            bands.append(rect)
    if len(bands) > MAX_RECTS_PER_ANCHOR:
        merged = bands[0]
        for rect in bands[1:]:
            merged = merged.union(rect)
        return [merged]
    return bands


def _page_rect(info: PdfInfo, page: int) -> Rect:
    width, height = info.size(page)
    return Rect(0.0, 0.0, width, height)


def _finish(rects: Sequence[Rect], info: PdfInfo, page: int) -> tuple[Rect, ...]:
    width, height = info.size(page)
    return tuple(r.padded(RECT_PADDING_PT).clamp(width, height) for r in rects)


def _estimate_page(line: int, total_lines: int, page_count: int) -> int:
    """没有 synctex 时的页码估计：源码行在全文中的相对位置线性映射到页码。

    这是**估计**不是定位，故对应的 `confidence` 只有 :data:`CONFIDENCE`\\ ``["blocks"]``。
    比「一律记第 1 页」有用，比假装精确诚实。真实论文实测后可换成更好的估计（开放问题 4）。
    """
    if page_count <= 1 or total_lines <= 1:
        return 1
    ratio = min(max((line - 1) / (total_lines - 1), 0.0), 1.0)
    return min(page_count, max(1, int(ratio * page_count) + 1))


def _chunk_index(chunks: Mapping | None) -> dict[str, str]:
    """`{块占位符: 所属翻译块 id}`——检验页据此标注「这段是回退原文」。"""
    index: dict[str, str] = {}
    if not isinstance(chunks, Mapping):
        return index
    for entry in chunks.get("chunks", ()) or ():
        if not isinstance(entry, Mapping):
            continue
        chunk_id = entry.get("id")
        source = entry.get("src")
        if not isinstance(chunk_id, str) or not isinstance(source, str):
            continue
        for token in re.findall(r"⟦BLK-\d+⟧", source):
            index.setdefault(token, chunk_id)
    return index


def _each(items, build):
    """逐条解析，**认不出的跳过**。

    anchors 是产物包里「锦上添花」的那一份：blocks.json 若真坏了，裁决它的是 export 的
    schema 校验（那一关会判 failed），而不是在这里抛一个 KeyError 把阶段炸成栈回溯。
    """
    found = []
    for item in items or ():
        if isinstance(item, (Block, Caption)):
            found.append(item)
            continue
        try:
            found.append(build(item))
        except (KeyError, TypeError, ValueError):
            continue
    return found


def _normalize_blocks(blocks) -> tuple[list[Block], dict[str, Caption]]:
    """接受 `MaskResult` 或 blocks.json 的 dict（与 unmask / figures 同一套约定）。"""
    if isinstance(blocks, MaskResult):
        return list(blocks.blocks), {c.id: c for c in blocks.captions}
    if isinstance(blocks, Mapping):
        return (
            _each(blocks.get("blocks"), Block.from_json),
            {c.id: c for c in _each(blocks.get("captions"), Caption.from_json)},
        )
    if isinstance(blocks, Sequence):
        return _each(blocks, Block.from_json), {}
    raise TypeError(f"无法识别的块清单类型：{type(blocks).__name__}")


@dataclass(frozen=True)
class AnchorsResult:
    """anchors 合成的结果。"""

    anchors: tuple[Anchor, ...] = ()
    pdf: PdfInfo = field(default_factory=PdfInfo)
    pdf_path: str = "zh.pdf"
    synctex_used: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def by_source(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for anchor in self.anchors:
            counts[anchor.source] = counts.get(anchor.source, 0) + 1
        return counts

    @property
    def degraded(self) -> bool:
        """有没有锚点退化成页级（没有 synctex 时全部如此）。"""
        return any(a.source != "synctex" for a in self.anchors)

    def to_anchors_json(self) -> dict:
        """按 `docs/schemas/anchors.schema.json` 组装 anchors.json 的内容。"""
        return {
            "contract_version": CONTRACT_VERSION,
            "pdf": self.pdf.to_json(self.pdf_path),
            "coordinate_system": {"origin": "top-left", "unit": "pt"},
            "anchors": [a.to_json() for a in self.anchors],
        }

    def to_json(self) -> dict:
        """manifest / report 用的摘要。"""
        data: dict = {
            "anchors": len(self.anchors),
            "by_source": self.by_source,
            "synctex": self.synctex_used,
            "page_count": self.pdf.page_count,
            "pages_parsed": self.pdf.parsed,
        }
        if self.warnings:
            data["warnings"] = list(self.warnings)
        return data


def build(
    *,
    zh_tex: str,
    blocks,
    pdf: str | Path | bytes | None = None,
    synctex: str | Path | bytes | None = None,
    chunks: Mapping | None = None,
    tex_name: str = "zh.tex",
    pdf_path: str = "zh.pdf",
) -> AnchorsResult:
    """三来源合成 anchors。

    :param zh_tex: `zh.tex` 全文（synctex 的行号与块的定位都以它为准）。
    :param blocks: `MaskResult` 或 blocks.json 的 dict —— 语义来源。
    :param pdf: `zh.pdf` 路径或字节 —— 画布来源（页数与页面尺寸）。
    :param synctex: `zh.synctex.gz` 路径或字节；`None` / 不存在 / 解析不出 → 页级锚点降级。
    :param chunks: chunks.json 的 dict，给出即为锚点填 `chunk_id`。

    出口判据在调用方（export）：产物过 `anchors.schema.json`。本函数只保证「说得出来源」。
    """
    block_list, caption_by_id = _normalize_blocks(blocks)
    warnings: list[str] = []

    info = scan_pdf(pdf) if pdf is not None else PdfInfo()
    if pdf is not None and not info.parsed:
        warnings.append(
            "PDF 里读不到 /MediaBox（对象流压缩或不是真 PDF），页面尺寸用兜底值"
        )

    mapping = SyncTexMap()
    tag: int | None = None
    if synctex is not None:
        try:
            mapping = parse_synctex(synctex)
        except (OSError, ValueError) as exc:
            warnings.append(f"synctex 解析失败（{exc}），锚点降级为页级")
        else:
            warnings.extend(mapping.warnings)
            tag = mapping.tag_for(tex_name)
            if mapping and tag is None:
                warnings.append(
                    f"synctex 里没有 {tex_name} 的 Input 记录（有 "
                    f"{', '.join(sorted(Path(p).name for p in mapping.inputs.values())[:5])}），"
                    "锚点降级为页级"
                )
    else:
        warnings.append("没有 zh.synctex.gz，锚点降级为页级（不伪造精确矩形）")

    spans = block_line_spans(zh_tex, block_list)
    total_lines = zh_tex.count("\n") + 1
    chunk_of = _chunk_index(chunks)
    synctex_ok = bool(mapping) and tag is not None

    anchors: list[Anchor] = []

    def emit(
        *,
        base_id: str,
        kind: str,
        line_span: tuple[int, int] | None,
        label: str | None = None,
        title: str | None = None,
        block_id: str | None = None,
        chunk_id: str | None = None,
    ) -> None:
        """一个对象 → 一条或多条锚点（跨页对象每页一条）。"""
        pages: dict[int, list[Rect]] = {}
        if synctex_ok and line_span is not None:
            assert tag is not None
            pages = mapping.rects_for(tag, line_span[0], line_span[1])
        if pages:
            for order, page in enumerate(sorted(pages)):
                rects = _finish(_merge_bands(pages[page]), info, page)
                if not rects:
                    continue
                anchors.append(
                    Anchor(
                        id=base_id if order == 0 else f"{base_id}@p{page}",
                        type=kind,
                        page=page,
                        rects=rects,
                        label=label,
                        title=title,
                        block_id=block_id,
                        chunk_id=chunk_id,
                        source="synctex",
                        confidence=CONFIDENCE["synctex"],
                    )
                )
            return
        # 降级：页级锚点。页码是估计值，矩形取整页，`source` 如实记 blocks。
        line = line_span[0] if line_span is not None else 1
        page = _estimate_page(line, total_lines, info.page_count)
        anchors.append(
            Anchor(
                id=base_id,
                type=kind,
                page=page,
                rects=(_page_rect(info, page),),
                label=label,
                title=title,
                block_id=block_id,
                chunk_id=chunk_id,
                source="blocks",
                confidence=CONFIDENCE["blocks"],
            )
        )

    for section in sections_in(zh_tex):
        emit(
            base_id=section.id,
            kind="section",
            line_span=(section.line, section.line),
            title=section.title or None,
        )

    for block in block_list:
        if block.category in SKIP_CATEGORIES:
            continue
        kind = BLOCK_TYPES.get(block.category, "block")
        caption = next(
            (
                caption_by_id[cid].text
                for cid in block.caption_ids
                if cid in caption_by_id and caption_by_id[cid].text.strip()
            ),
            "",
        )
        emit(
            base_id=block.id,
            kind=kind,
            line_span=spans.get(block.id),
            label=block.label,
            title=clean_title(caption) or None,
            block_id=block.id,
            chunk_id=chunk_of.get(block.placeholder),
        )

    anchors.sort(key=lambda a: (a.page, a.rects[0].y if a.rects else 0.0, a.id))
    return AnchorsResult(
        anchors=tuple(anchors),
        pdf=info,
        pdf_path=pdf_path,
        synctex_used=synctex_ok,
        warnings=tuple(warnings),
    )
