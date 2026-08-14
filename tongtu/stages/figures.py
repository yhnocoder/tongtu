"""figures 阶段：源码包内图源 → PNG 预渲染，外加 caption 与引用段落收集
（架构 §3 figures 行、§7 产物表、决策 9/14）。

本阶段是流水线里**唯一与翻译轨完全解耦**的一段：它只读 `src/`（外加 blocks.json 里
mask 已经解析好的图块与 caption 槽位），逐图以**源文件 sha256** 为缓存 key。翻译侧
任何返工——重译某块、换模型、改术语表——都不会让任何一张图重渲染（架构 §4）。

## 预渲染 PNG 是给谁看的

不是给 `zh.pdf` 看的。对 xelatex 而言 PDF 图是原生输入、EPS 走 epstopdf 链，编译侧零
处理（决策 9）；预渲染只服务两个消费者：**视觉模型**（读图必须吃位图）与**检验页/
索引**。视觉 API 对长边超过 ≈1568px 的图一律降采样，更高分辨率纯属浪费——故长边上限
是 `DEFAULT_MAX_LONG_EDGE`，矢量源按它反算 DPI，位图源**只缩不放大**（决策 14）。
单格式 PNG，`format` 字段为后续 WebP 留门。

## 渲染器可注入，且「本机没有工具」是一等公民

真渲染要外部工具（pdftocairo / epstopdf / ImageMagick），而开发机与纯文本层 CI 都
可能一个都没有。故渲染器是注入点（:class:`Renderer` 协议），默认实现
:func:`default_renderer` 探测工具，探不到就退到 :class:`PurePythonRenderer`：

* PNG 源在上限内 → 直接拷贝（零依赖也能出真 PNG）；
* PNG 源超上限 → 照样拷贝，但记 ``downscale_skipped`` 降级（纯 Python 做重采样不现实，
  **宁可标注也不假装**）；
* PDF / EPS / JPEG 等 → 记 ``missing_tool`` 跳过该图。

跳过的图**不进 figures.json**：schema 要求每条 figure 都有 `render.path/width/height`，
一张没渲出来的图没有 render 可填。它们进 :attr:`FiguresResult.skipped` 与 warnings，
由 report / export 负责让人看见（口径外决定，见模块末尾注释）。

## 引用提取的口径

`\\includegraphics` 的**每一次出现**都是一条 figure 记录（subfigure 块因此产出多条，
共享 `block_id`）。文件名解析按 LaTeX 的规矩：给了已知扩展名就用它，否则按
:data:`GRAPHICS_EXTENSIONS` 的顺序（pdf → png → jpg → …）在 `src/` 与
`\\graphicspath` 声明的目录里试。caption 与 label 按**块内位置**配对：图 → 其后第一个
必选 caption 槽位（subfigure 的子 caption 正在那儿），`[短标题]` 槽位不参与配对；label
取本图与下一张图之间的第一个 `\\label`，没有就回落到块级 label（revtex 那种把 label 写
进 caption 里的写法也照样命中——mask 把 caption 原文完整保留在 CAP 槽位里）。

`referenced_in` 从掩码流扫 `\\ref` 家族（含 `\\cref{a,b}` 逗号表与 `\\hyperref[a]`），
命中处映射回**段落**；给了 chunk 计划就顺带填 `chunk_id`。掩码流缺席时该字段为空——
figures 不为了引用段落去依赖翻译轨的任何产物。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from .. import CONTRACT_VERSION
from ..schema_check import SchemaError
from ..schema_check import check as schema_check
from ..texlex import Lexer, TexLexError, find_bracket_arg, read_group, skip_spaces
from ..workdir import Workdir
from .chunk import ChunkPlan, Paragraph, split_paragraphs
from .mask import CAPTION_TOKEN_RE, Block, Caption, MaskResult

__all__ = [
    "CACHE_NAME",
    "CACHE_VERSION",
    "DEFAULT_DPI",
    "DEFAULT_MAX_LONG_EDGE",
    "DEGRADED",
    "DOWNSCALE_SKIPPED",
    "FAILED",
    "FIGURES_DIRNAME",
    "FIGURES_JSON",
    "FIGURE_CATEGORIES",
    "GRAPHICS_COMMANDS",
    "GRAPHICS_EXTENSIONS",
    "MAX_DPI",
    "MISSING_FILE",
    "MISSING_TOOL",
    "OK",
    "RENDER_FAILED",
    "STAGE_PREFIX",
    "UNREADABLE",
    "DefaultRenderer",
    "FigureRecord",
    "FigureSpec",
    "FiguresResult",
    "Graphic",
    "PurePythonRenderer",
    "Reference",
    "RenderResult",
    "Renderer",
    "SkippedFigure",
    "collect_figures",
    "collect_references",
    "default_renderer",
    "eps_bounding_box",
    "figures",
    "iter_includegraphics",
    "parse_graphicspath",
    "pdf_page_size",
    "png_size",
    "resolve_graphic",
    "sha256_file",
    "source_format",
]


# --------------------------------------------------------------------- 常量

#: 预渲染产物目录（`build/` 内；export 阶段搬进 `out/figures/`）。
FIGURES_DIRNAME = "figures"

#: 元数据产物名（过 `docs/schemas/figures.schema.json`）。
FIGURES_JSON = "figures.json"

#: 逐图缓存文件名。**刻意不叫 `*.png`**——export 按 `*.png` + `figures.json` 取产物，
#: 缓存是 build 区的私事，不进产物包。
CACHE_NAME = "cache.json"

#: 缓存命中快照的文件名前缀（见 `_stage_cache_hits`）。跑完即被清扫。
STAGE_PREFIX = ".stage-"

#: 缓存结构版本。结构变了就 bump，旧缓存一律判过期（同 pipeline 的 manifest 纪律）。
CACHE_VERSION = 1

#: 预渲染长边上限（px）。视觉模型的有效上限 ≈1568，超出即被 API 降采样（决策 14）。
DEFAULT_MAX_LONG_EDGE = 1568

#: 矢量源的 DPI 安全上限。极小的 MediaBox（几十 pt 的图标）反算出来的 DPI 可以上千，
#: 那是给渲染器找麻烦；封顶后长边达不到上限也无妨——矢量源本就没有「原生分辨率」。
MAX_DPI = 600.0

#: 读不出页面尺寸时的兜底 DPI。
DEFAULT_DPI = 150.0

#: 认作「图块」的 blocks.json 块分类（mask 的 CATEGORIES 之一）。
#:
#: `unknown` 也在内：mask 对表外环境的默认是**保守整块掩码 + category=unknown**（架构
#: §3.1 第 2 条），`.sty` 里自定义的浮动体环境正落在这一格。一个块里真有
#: `\\includegraphics`，它就是张图——因分类没落表而让图从产物里消失，才是更坏的结果。
FIGURE_CATEGORIES: frozenset[str] = frozenset({"figure", "unknown"})

#: 取图命令。`\\includegraphics` 之外只收同样「一个必选参数就是文件名」的老写法。
GRAPHICS_COMMANDS: frozenset[str] = frozenset({"\\includegraphics", "\\includesvg"})

#: 无扩展名引用的解析顺序（LaTeX 的 `\\DeclareGraphicsExtensions` 默认序，pdf 优先）。
GRAPHICS_EXTENSIONS: tuple[str, ...] = (
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".eps",
    ".ps",
    ".gif",
    ".svg",
)

#: 扩展名 → schema 的 `source.format` 枚举。
_FORMATS: dict[str, str] = {
    ".pdf": "pdf",
    ".eps": "eps",
    ".epsf": "eps",
    ".ps": "ps",
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".gif": "gif",
    ".svg": "svg",
}

#: 引用段落片段的截断长度。
REF_TEXT_LIMIT = 400

#: 阶段状态。
OK = "ok"
DEGRADED = "degraded"  # 出了图，但有图被跳过或降级
FAILED = "failed"  # 自家产物不过 schema —— 这是本模块的 bug，不许悄悄放过

#: 降级/跳过原因（进 warnings、`FiguresResult.skipped` 与缓存记录）。
MISSING_FILE = "missing_file"  # `src/` 里找不到被引用的图
MISSING_TOOL = "missing_tool"  # 缺 pdftocairo / epstopdf / ImageMagick
DOWNSCALE_SKIPPED = "downscale_skipped"  # 超上限但没有缩图工具，原样拷贝并标注
RENDER_FAILED = "render_failed"  # 工具在，但这张图渲染失败
UNREADABLE = "unreadable"  # 文件读不出来 / 不是它自称的格式

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_MEDIABOX_RE = re.compile(
    rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]"
)
_BBOX_RE = re.compile(
    rb"%%(?:HiRes|Exact)?BoundingBox:\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)"
)

#: `\ref` 家族。命中即认作「此处引用了这些 label」。
REF_COMMANDS: frozenset[str] = frozenset(
    {
        "ref",
        "eqref",
        "autoref",
        "autopageref",
        "cref",
        "Cref",
        "crefrange",
        "Crefrange",
        "cpageref",
        "Cpageref",
        "labelcref",
        "nameref",
        "Nameref",
        "pageref",
        "subref",
        "vref",
        "Vref",
        "vpageref",
    }
)

_REF_RE = re.compile(r"\\([A-Za-z]+)\*?\s*\{([^{}]*)\}")
_HYPERREF_RE = re.compile(r"\\hyperref\s*\[([^\[\]]*)\]")
_LABEL_RE = re.compile(r"\\label\s*\*?\s*\{([^{}]*)\}")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*\{")
_WS_RE = re.compile(r"\s+")


# ----------------------------------------------------------------- 小工具


def sha256_file(path: str | os.PathLike[str]) -> str:
    """文件内容的小写 sha256（逐图缓存 key）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def png_size(data: bytes) -> tuple[int, int] | None:
    """解析 PNG 的 IHDR 取 (宽, 高)；不是 PNG 或头部残缺则 None。

    零依赖读尺寸的全部依据就是这 8 + 25 字节：签名 + 第一个 chunk 必须是 IHDR
    （PNG 规范硬性要求），宽高是它前 8 个字节的大端无符号整数。
    """
    if len(data) < 24 or not data.startswith(_PNG_MAGIC):
        return None
    if data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def pdf_page_size(data: bytes) -> tuple[float, float] | None:
    """从第一个 `/MediaBox` 取页面尺寸（pt）。对象流压缩掉了 MediaBox 就返回 None。"""
    match = _MEDIABOX_RE.search(data)
    if match is None:
        return None
    x0, y0, x1, y1 = (float(v) for v in match.groups())
    width, height = abs(x1 - x0), abs(y1 - y0)
    if width <= 0 or height <= 0:
        return None
    return width, height


def eps_bounding_box(data: bytes) -> tuple[float, float] | None:
    """从 `%%BoundingBox` 取 EPS 尺寸（pt）。"""
    match = _BBOX_RE.search(data)
    if match is None:
        return None
    x0, y0, x1, y1 = (float(v) for v in match.groups())
    width, height = abs(x1 - x0), abs(y1 - y0)
    if width <= 0 or height <= 0:
        return None
    return width, height


def source_format(path: str | os.PathLike[str]) -> str:
    """扩展名 → schema 的 `source.format` 枚举（认不出的一律 `other`）。"""
    return _FORMATS.get(Path(path).suffix.lower(), "other")


def _read_head(path: Path, limit: int = 1 << 16) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


def _source_size_pt(path: Path, fmt: str) -> tuple[float, float] | None:
    """源图的物理尺寸（pt）。位图没有「pt 尺寸」这回事，返回 None。"""
    if fmt == "pdf":
        return pdf_page_size(_read_head(path, 1 << 20))
    if fmt in ("eps", "ps"):
        return eps_bounding_box(_read_head(path))
    return None


def _dpi_for(size_pt: tuple[float, float] | None, max_long_edge: int) -> float:
    """矢量源按长边上限反算 DPI，并按 :data:`MAX_DPI` 封顶。"""
    if size_pt is None:
        return DEFAULT_DPI
    long_pt = max(size_pt)
    if long_pt <= 0:
        return DEFAULT_DPI
    return min(MAX_DPI, 72.0 * max_long_edge / long_pt)


# --------------------------------------------------------------- 渲染器接口


@dataclass(frozen=True)
class RenderResult:
    """一次渲染的结果。失败一律走 `ok=False` + `degradation`，不抛栈。"""

    ok: bool
    width_px: int = 0
    height_px: int = 0
    dpi: float | None = None
    upscaled: bool = False
    """位图源是否被放大——本实现恒为 False（决策 14：只缩不放大）。"""

    bytes: int = 0
    tool: str = ""
    """实际动手的工具名（`copy` = 纯拷贝，空 = 没渲）。"""

    degradation: str = ""
    """降级/失败原因（:data:`MISSING_TOOL` 等），空串表示干净成功。"""

    message: str = ""

    def to_cache(self) -> dict:
        data: dict = {
            "width_px": self.width_px,
            "height_px": self.height_px,
            "upscaled": self.upscaled,
            "bytes": self.bytes,
            "tool": self.tool,
        }
        if self.dpi is not None:
            data["dpi"] = self.dpi
        if self.degradation:
            data["degradation"] = self.degradation
        if self.message:
            data["message"] = self.message
        return data

    @classmethod
    def from_cache(cls, data: Mapping) -> "RenderResult":
        return cls(
            ok=True,
            width_px=int(data.get("width_px", 0)),
            height_px=int(data.get("height_px", 0)),
            dpi=data.get("dpi"),
            upscaled=bool(data.get("upscaled", False)),
            bytes=int(data.get("bytes", 0)),
            tool=str(data.get("tool", "")),
            degradation=str(data.get("degradation", "")),
            message=str(data.get("message", "")),
        )


class Renderer(Protocol):
    """把一张源图渲成 PNG。注入点：本机没工具、CI 没工具、测试要计数，都走这里。"""

    def render(self, src: Path, dst: Path, max_long_edge: int) -> RenderResult:
        """把 `src` 渲成 `dst`（PNG），长边不超过 `max_long_edge`。"""
        ...


@dataclass(frozen=True)
class PurePythonRenderer:
    """零依赖兜底渲染器：只会拷贝 PNG，别的一概如实报 `missing_tool`。

    「零依赖缩放」不现实（重采样得自己写，质量与速度都交代不过去），所以超上限的 PNG
    **原样拷贝**并记 :data:`DOWNSCALE_SKIPPED`——图还在、元数据诚实，代价只是喂给视觉
    模型时由 API 自己降采样。假装缩过才是不可接受的那一种。
    """

    def render(self, src: Path, dst: Path, max_long_edge: int) -> RenderResult:
        fmt = source_format(src)
        if fmt != "png":
            return RenderResult(
                ok=False,
                degradation=MISSING_TOOL,
                message=f"{fmt} 源需要外部渲染工具（pdftocairo / epstopdf / ImageMagick），本机没有",
            )
        try:
            data = src.read_bytes()
        except OSError as exc:
            return RenderResult(ok=False, degradation=UNREADABLE, message=f"读不出源图：{exc}")
        size = png_size(data)
        if size is None:
            return RenderResult(
                ok=False, degradation=UNREADABLE, message="PNG 头部残缺（读不到 IHDR）"
            )
        width, height = size
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        except OSError as exc:
            return RenderResult(ok=False, degradation=RENDER_FAILED, message=f"写不出 PNG：{exc}")
        degradation = message = ""
        if max(width, height) > max_long_edge:
            degradation = DOWNSCALE_SKIPPED
            message = (
                f"PNG 长边 {max(width, height)}px 超过上限 {max_long_edge}px，"
                "本机没有缩图工具（ImageMagick），已原样拷贝"
            )
        return RenderResult(
            ok=True,
            width_px=width,
            height_px=height,
            bytes=len(data),
            tool="copy",
            degradation=degradation,
            message=message,
        )


@dataclass(frozen=True)
class DefaultRenderer:
    """探测外部工具的默认渲染器；工具缺席的那条路一律委托给 :class:`PurePythonRenderer`。

    * PDF → `pdftocairo -png -r <dpi> -singlefile`（DPI 按 MediaBox 反算）；
    * EPS / PS → `epstopdf` 转成临时 PDF 再走上面那条（决策 9 的「EPS 走 epstopdf 链」）；
    * PNG → 上限内直接拷贝；超上限用 ImageMagick `-resize WxH>`（`>` = 只缩不放大）；
    * 其余位图（JPEG/GIF）→ ImageMagick 转 PNG。
    """

    pdftocairo: str | None = None
    epstopdf: str | None = None
    magick: tuple[str, ...] | None = None
    timeout: float = 120.0
    max_dpi: float = MAX_DPI
    fallback: PurePythonRenderer = field(default_factory=PurePythonRenderer)

    @classmethod
    def detect(cls, **kwargs) -> "DefaultRenderer":
        """按 PATH 探测工具链。显式传参可覆盖任何一项（测试用）。"""
        magick = next(
            ((found,) for found in map(shutil.which, ("magick", "convert")) if found), None
        )
        defaults = {
            "pdftocairo": shutil.which("pdftocairo"),
            "epstopdf": shutil.which("epstopdf"),
            "magick": magick,
        }
        defaults.update(kwargs)
        return cls(**defaults)

    # ---- 内部

    def _run(self, argv: Sequence[str]) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{argv[0]} 跑不起来：{exc}"
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return False, f"{Path(argv[0]).name} 退出码 {proc.returncode}：{tail[-1] if tail else ''}"
        return True, ""

    def _finish(self, dst: Path, tool: str, dpi: float | None) -> RenderResult:
        """渲染工具号称成功之后，唯一的裁决者是产出的 PNG 本身。"""
        if not dst.is_file():
            return RenderResult(
                ok=False, degradation=RENDER_FAILED, message=f"{tool} 没有产出 {dst.name}"
            )
        data = dst.read_bytes()
        size = png_size(data)
        if size is None:
            return RenderResult(
                ok=False, degradation=RENDER_FAILED, message=f"{tool} 产出的不是可解析的 PNG"
            )
        return RenderResult(
            ok=True,
            width_px=size[0],
            height_px=size[1],
            dpi=dpi,
            bytes=len(data),
            tool=tool,
        )

    def _render_pdf(self, pdf: Path, dst: Path, max_long_edge: int, tool_note: str) -> RenderResult:
        if not self.pdftocairo:
            return RenderResult(
                ok=False,
                degradation=MISSING_TOOL,
                message="缺 pdftocairo（poppler-utils），矢量图无法预渲染",
            )
        dpi = min(self.max_dpi, _dpi_for(_source_size_pt(pdf, "pdf"), max_long_edge))
        dst.parent.mkdir(parents=True, exist_ok=True)
        ok, message = self._run(
            [
                self.pdftocairo,
                "-png",
                "-r",
                f"{dpi:g}",
                "-singlefile",
                str(pdf),
                str(dst.with_suffix("")),
            ]
        )
        if not ok:
            return RenderResult(ok=False, degradation=RENDER_FAILED, message=message)
        return self._finish(dst, tool_note, dpi)

    def _render_bitmap(self, src: Path, dst: Path, max_long_edge: int) -> RenderResult:
        if self.magick is None:
            return self.fallback.render(src, dst, max_long_edge)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # `WxH>` 是 ImageMagick 的「只缩不放大」几何后缀——决策 14 的位图纪律直接写进参数。
        ok, message = self._run(
            [
                *self.magick,
                str(src),
                "-resize",
                f"{max_long_edge}x{max_long_edge}>",
                str(dst),
            ]
        )
        if not ok:
            return RenderResult(ok=False, degradation=RENDER_FAILED, message=message)
        return self._finish(dst, Path(self.magick[0]).name, None)

    # ---- 协议

    def render(self, src: Path, dst: Path, max_long_edge: int) -> RenderResult:
        fmt = source_format(src)
        if fmt == "pdf":
            return self._render_pdf(src, dst, max_long_edge, "pdftocairo")
        if fmt in ("eps", "ps"):
            if not self.epstopdf:
                return RenderResult(
                    ok=False,
                    degradation=MISSING_TOOL,
                    message="缺 epstopdf，EPS/PS 无法转 PDF 再渲染",
                )
            with tempfile.TemporaryDirectory(prefix="tongtu-eps-") as tmp:
                pdf = Path(tmp) / (src.stem + ".pdf")
                ok, message = self._run([self.epstopdf, f"--outfile={pdf}", str(src)])
                if not ok:
                    return RenderResult(ok=False, degradation=RENDER_FAILED, message=message)
                return self._render_pdf(pdf, dst, max_long_edge, "epstopdf+pdftocairo")
        if fmt == "png":
            data = _read_head(src, 32)
            size = png_size(data)
            if size is not None and max(size) <= max_long_edge:
                return self.fallback.render(src, dst, max_long_edge)  # 上限内：拷贝即可
            return self._render_bitmap(src, dst, max_long_edge)
        if fmt in ("jpg", "gif"):
            return self._render_bitmap(src, dst, max_long_edge)
        return RenderResult(
            ok=False,
            degradation=MISSING_TOOL,
            message=f"{fmt} 源本期不预渲染（SVG 视觉 API 不接受，见决策 14）",
        )


def default_renderer() -> DefaultRenderer:
    """按 PATH 探测一次工具链，得到默认渲染器。"""
    return DefaultRenderer.detect()


# ------------------------------------------------------------- 源码侧提取


@dataclass(frozen=True)
class Graphic:
    """块内的一次 `\\includegraphics`（尚未解析到真实文件）。"""

    command: str
    argument: str
    """必选参数原文（可能没有扩展名，可能带子目录）。"""

    options: tuple[str, ...] = ()
    """可选参数原文（`[width=...]`、老式的两个 `[llx,lly][urx,ury]` 都收）。"""

    offset: int = 0
    """在块 tex 里的起始偏移（与 caption / label 配对靠它）。"""


def iter_includegraphics(
    tex: str, *, verbatim_envs: Iterable[str] = ()
) -> tuple[Graphic, ...]:
    """按词法扫出块内全部取图命令（注释与 verbatim 体内的不算）。

    识别 `\\includegraphics*`、任意多个可选参数、必选参数里的换行与嵌套花括号；
    参数不配平就跳过该处（宁可漏一张图，也不让整块解析崩掉）。
    """
    found: list[Graphic] = []
    lexer = Lexer(tex, verbatim_envs=frozenset(verbatim_envs))
    for tok in lexer:
        if tok.kind != "control":
            continue
        command = tex[tok.start : tok.end]
        if command not in GRAPHICS_COMMANDS:
            continue
        i = tok.end
        if tex[i : i + 1] == "*":
            i += 1
        options: list[str] = []
        while True:
            j = skip_spaces(tex, i)
            if tex[j : j + 1] != "[":
                break
            try:
                close = find_bracket_arg(tex, j)
            except TexLexError:
                break
            options.append(tex[j + 1 : close])
            i = close + 1
        group = read_group(tex, i)
        if group is None:
            continue
        argument, after = group
        found.append(
            Graphic(
                command=command,
                argument=argument.strip(),
                options=tuple(options),
                offset=tok.start,
            )
        )
        lexer.pos = after
    return tuple(found)


def parse_graphicspath(tex: str) -> tuple[str, ...]:
    """解析 `\\graphicspath{{figs/}{img/}}` 的目录列表（按声明顺序）。"""
    paths: list[str] = []
    for match in _GRAPHICSPATH_RE.finditer(tex):
        group = read_group(tex, match.end() - 1)
        if group is None:
            continue
        body = group[0]
        pos = 0
        while pos < len(body):
            inner = read_group(body, pos)
            if inner is None:
                break
            entry = inner[0].strip()
            if entry:
                paths.append(entry)
            pos = inner[1]
    seen: set[str] = set()
    return tuple(p for p in paths if not (p in seen or seen.add(p)))


def resolve_graphic(
    argument: str,
    roots: Sequence[Path],
    *,
    extensions: Sequence[str] = GRAPHICS_EXTENSIONS,
) -> Path | None:
    """把 `\\includegraphics` 的参数解析成真实文件（LaTeX 的查找规矩）。

    1. 参数原样在各 root 下试（有扩展名、且文件真在，就是它）；
    2. 否则按 `extensions` 顺序逐个补扩展名再试——LaTeX 正是这样把
       `{figures/pipeline}` 解析成 `figures/pipeline.pdf` 的（pdf 优先于 png）。

    `..` 逃出 root 的引用一律判为找不到（`src/` 之外的东西不进产物包）。
    """
    name = argument.strip().removeprefix("./")
    if not name:
        return None
    for root in roots:
        base = root / name
        candidates = [base] if base.suffix else []
        candidates += [base.with_name(base.name + ext) for ext in extensions]
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                candidate.resolve().relative_to(root.resolve())
            except (OSError, ValueError):
                continue
            return candidate
    return None


def _normalize_blocks(blocks) -> tuple[list[Block], list[Caption]]:
    """接受 `MaskResult`、blocks.json 的 dict，或裸块序列（同 survey / unmask 的约定）。"""
    if isinstance(blocks, MaskResult):
        return list(blocks.blocks), list(blocks.captions)
    if isinstance(blocks, Mapping):
        return (
            [b if isinstance(b, Block) else Block.from_json(b) for b in blocks.get("blocks", ())],
            [
                c if isinstance(c, Caption) else Caption.from_json(c)
                for c in blocks.get("captions", ())
            ],
        )
    if isinstance(blocks, Sequence):
        return [b if isinstance(b, Block) else Block.from_json(b) for b in blocks], []
    raise TypeError(f"无法识别的块清单类型：{type(blocks).__name__}")


@dataclass(frozen=True)
class _Slot:
    """块 tex 里一个 CAP 槽位的位置（配对用）。"""

    offset: int
    caption_id: str
    optional: bool
    """是否 `\\caption[短标题]` 的那个可选槽位（进目录用，不当图注）。"""


def _caption_slots(block: Block, captions: Mapping[str, Caption]) -> tuple[_Slot, ...]:
    """块 tex 里的 CAP 槽位（mask 把 caption 正文换成了占位符，位置正好可用）。"""
    slots: list[_Slot] = []
    for match in CAPTION_TOKEN_RE.finditer(block.tex):
        caption = captions.get(match.group(0))
        if caption is None:
            continue
        if caption.kind not in ("caption", "captionof"):
            continue
        j = match.start() - 1
        while j >= 0 and block.tex[j] in " \t\r\n":
            j -= 1
        slots.append(
            _Slot(
                offset=match.start(),
                caption_id=caption.id,
                optional=j >= 0 and block.tex[j] == "[",
            )
        )
    return tuple(slots)


def _label_events(block: Block, captions: Mapping[str, Caption]) -> tuple[tuple[int, str], ...]:
    """块内 `\\label` 的 (位置, 名字)。

    caption 正文在 block.tex 里已被换成 CAP 占位符，revtex 惯用的
    `\\caption{\\label{fig:x}…}` 因此在 block.tex 里看不见——故 caption 原文也要扫一遍，
    命中的 label 记在该 caption 槽位的位置上。
    """
    events: list[tuple[int, str]] = [
        (m.start(), m.group(1).strip()) for m in _LABEL_RE.finditer(block.tex)
    ]
    for match in CAPTION_TOKEN_RE.finditer(block.tex):
        caption = captions.get(match.group(0))
        if caption is None:
            continue
        for inner in _LABEL_RE.finditer(caption.text):
            events.append((match.start(), inner.group(1).strip()))
    return tuple(sorted(e for e in events if e[1]))


@dataclass(frozen=True)
class Reference:
    """全文里引用了某图的一处（`referenced_in` 的一条）。"""

    paragraph: int
    section: str = ""
    text: str = ""
    chunk_id: str | None = None

    def to_json(self) -> dict:
        data: dict = {"paragraph": self.paragraph}
        if self.chunk_id:
            data["chunk_id"] = self.chunk_id
        if self.section:
            data["section"] = self.section
        if self.text:
            data["text"] = self.text
        return data


def _snippet(text: str, limit: int = REF_TEXT_LIMIT) -> str:
    collapsed = _WS_RE.sub(" ", text).strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _iter_ref_hits(masked: str) -> Iterable[tuple[int, str]]:
    """掩码流里的 (位置, label)——`\\cref{a,b}` 的逗号表拆开，`\\hyperref[a]` 也收。"""
    for match in _REF_RE.finditer(masked):
        if match.group(1) not in REF_COMMANDS:
            continue
        for label in match.group(2).split(","):
            name = label.strip()
            if name:
                yield match.start(), name
    for match in _HYPERREF_RE.finditer(masked):
        name = match.group(1).strip()
        if name:
            yield match.start(), name


def collect_references(
    masked: str,
    labels: Iterable[str],
    *,
    plan: ChunkPlan | None = None,
    paragraphs: Sequence[Paragraph] | None = None,
) -> dict[str, tuple[Reference, ...]]:
    """`label → 引用它的段落清单`。给了 chunk 计划就顺带填 `chunk_id`。

    同一段里引用同一个 label 多次只记一条——`referenced_in` 是给检验页做跳转的，
    重复条目对它毫无意义。
    """
    wanted = {label for label in labels if label}
    if not wanted or not masked:
        return {}
    paras = tuple(paragraphs) if paragraphs is not None else (
        plan.paragraphs if plan is not None else split_paragraphs(masked)
    )
    if not paras:
        return {}
    starts = [p.start for p in paras]

    def locate(offset: int) -> Paragraph | None:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        para = paras[lo]
        return para if para.start <= offset else None

    chunk_of: dict[int, str] = {}
    if plan is not None:
        for chunk in plan.chunks:
            for index in range(chunk.para_start, chunk.para_end):
                chunk_of[index] = chunk.id

    hits: dict[str, dict[int, Reference]] = {}
    for offset, label in _iter_ref_hits(masked):
        if label not in wanted:
            continue
        para = locate(offset)
        if para is None:
            continue
        bucket = hits.setdefault(label, {})
        if para.index in bucket:
            continue
        bucket[para.index] = Reference(
            paragraph=para.index,
            section=para.section_titles[-1] if para.section_titles else "",
            text=_snippet(para.text),
            chunk_id=chunk_of.get(para.index),
        )
    return {label: tuple(bucket[k] for k in sorted(bucket)) for label, bucket in hits.items()}


@dataclass(frozen=True)
class FigureSpec:
    """一张图的**源码侧**元数据（还没渲染）。"""

    id: str
    graphic: Graphic
    block_id: str | None = None
    label: str | None = None
    caption: str = ""
    caption_id: str | None = None
    path: Path | None = None
    """`src/` 内解析到的真实文件；None = 找不到。"""

    rel_path: str = ""
    """相对 `src/` 的路径（进 `source.path`）。"""

    format: str = "other"
    sha256: str = ""
    size_pt: tuple[float, float] | None = None
    referenced_in: tuple[Reference, ...] = ()

    @property
    def found(self) -> bool:
        return self.path is not None

    def source_json(self) -> dict:
        data: dict = {"path": self.rel_path or self.graphic.argument, "format": self.format}
        if self.sha256:
            data["sha256"] = self.sha256
        if self.size_pt is not None:
            data["width_pt"], data["height_pt"] = self.size_pt
        return data


def collect_figures(
    workdir: Workdir,
    blocks,
    *,
    masked: str | None = None,
    plan: ChunkPlan | None = None,
    src: str | os.PathLike[str] | None = None,
    graphicspath: Sequence[str] = (),
    categories: Iterable[str] | None = FIGURE_CATEGORIES,
    warnings: list[str] | None = None,
) -> tuple[FigureSpec, ...]:
    """从图块里收集全部 `\\includegraphics`，解析到 `src/` 下的真实文件。

    :param blocks: `MaskResult` / blocks.json 的 dict / 裸块序列。
    :param masked: 掩码流。给了才有 `referenced_in`（figures 不为此依赖翻译轨产物）。
    :param plan: chunk 计划。给了才有 `referenced_in[].chunk_id`。
    :param src: 覆盖 `src/` 位置（默认 `workdir.src`）。
    :param graphicspath: 额外的图片搜索目录；源码里的 `\\graphicspath` 自动叠加。
    :param categories: 扫哪些块分类（默认 :data:`FIGURE_CATEGORIES`）；`None` = 全扫。

    返回**源码顺序**的 spec 列表（`fig-001`、`fig-002`…）；找不到文件的也在列，
    由调用方决定怎么报（`figures()` 记 skipped + warning）。
    """
    notes = warnings if warnings is not None else []
    block_list, caption_list = _normalize_blocks(blocks)
    caption_map = {c.placeholder: c for c in caption_list}
    caption_by_id = {c.id: c for c in caption_list}
    wanted = None if categories is None else frozenset(categories)
    root = Path(src) if src is not None else workdir.src

    scan_text = "\n".join([masked or "", *(b.tex for b in block_list)])
    roots = [root]
    for entry in (*parse_graphicspath(scan_text), *graphicspath):
        candidate = root / entry
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)

    specs: list[FigureSpec] = []
    for block in block_list:
        if wanted is not None and block.category not in wanted:
            continue
        graphics = iter_includegraphics(block.tex)
        if not graphics:
            continue
        slots = _caption_slots(block, caption_map)
        labels = _label_events(block, caption_map)
        for position, graphic in enumerate(graphics):
            next_offset = (
                graphics[position + 1].offset if position + 1 < len(graphics) else len(block.tex)
            )
            caption_id = _pair_caption(graphic.offset, slots)
            caption = caption_by_id.get(caption_id) if caption_id else None
            label = _pair_label(graphic.offset, next_offset, labels) or block.label
            path = resolve_graphic(graphic.argument, roots)
            digest = ""
            if path is not None:
                try:
                    digest = sha256_file(path)
                except OSError as exc:
                    # hash 是缓存 key，读不出来就不能当它存在——空 key 会让两张图互相顶替。
                    notes.append(f"读不出 {graphic.argument!r}（{exc}），当作缺图处理")
                    path = None
            spec = FigureSpec(
                id=f"fig-{len(specs) + 1:03d}",
                graphic=graphic,
                block_id=block.id,
                label=label,
                caption=(caption.text.strip() if caption else ""),
                caption_id=caption_id,
                path=path,
                rel_path=_relative(path, root),
                format=source_format(path) if path is not None else source_format(graphic.argument),
                sha256=digest,
                size_pt=_source_size_pt(path, source_format(path)) if path is not None else None,
            )
            if path is None:
                notes.append(
                    f"{spec.id}：`src/` 下找不到 {graphic.argument!r}"
                    f"（试过 {', '.join(GRAPHICS_EXTENSIONS)} 与 graphicspath）"
                )
            specs.append(spec)

    if masked:
        stray = len(iter_includegraphics(masked))
        if stray:
            notes.append(
                f"掩码流里还有 {stray} 处 \\includegraphics 不在图块内（正文里的裸图），本阶段不收集"
            )
        wanted_labels = {s.label for s in specs if s.label}
        refs = collect_references(masked, wanted_labels, plan=plan)
        specs = [
            spec if not spec.label else replace(spec, referenced_in=refs.get(spec.label, ()))
            for spec in specs
        ]
    return tuple(specs)


def _relative(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _pair_caption(offset: int, slots: Sequence[_Slot]) -> str | None:
    """图 → caption：其后第一个必选槽位；没有就取其前最后一个（caption 写在图上方）。"""
    mandatory = [s for s in slots if not s.optional] or list(slots)
    after = [s for s in mandatory if s.offset >= offset]
    if after:
        return after[0].caption_id
    before = [s for s in mandatory if s.offset < offset]
    return before[-1].caption_id if before else None


def _pair_label(offset: int, next_offset: int, labels: Sequence[tuple[int, str]]) -> str | None:
    """图 → label：本图与下一张图之间的第一个 `\\label`（subfigure 的子 label 正在那儿）。"""
    for position, name in labels:
        if offset <= position < next_offset:
            return name
    return None


# ------------------------------------------------------------------ 阶段


@dataclass(frozen=True)
class FigureRecord:
    """一条渲染完成的 figure 元数据（figures.json 的数组元素）。"""

    spec: FigureSpec
    render: RenderResult
    file: str
    """`build/figures/` 内的文件名。"""

    cached: bool = False

    def to_json(self) -> dict:
        spec = self.spec
        render: dict = {
            "path": f"{FIGURES_DIRNAME}/{self.file}",
            "format": "png",
            "width_px": self.render.width_px,
            "height_px": self.render.height_px,
            "upscaled": self.render.upscaled,
            "bytes": self.render.bytes,
        }
        if self.render.dpi is not None:
            render["dpi"] = self.render.dpi
        data: dict = {
            "id": spec.id,
            "label": spec.label,
            "block_id": spec.block_id,
            "caption": {"source": spec.caption, "translation": ""},
            "source": spec.source_json(),
            "render": render,
        }
        if spec.referenced_in:
            data["referenced_in"] = [r.to_json() for r in spec.referenced_in]
        return data


@dataclass(frozen=True)
class SkippedFigure:
    """没能进 figures.json 的图（没渲出来就没有 `render`，schema 容不下它）。"""

    id: str
    reason: str
    source: str
    message: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "reason": self.reason,
            "source": self.source,
            "message": self.message,
        }


@dataclass(frozen=True)
class FiguresResult:
    """figures 阶段的结构化结果。"""

    status: str
    records: tuple[FigureRecord, ...] = ()
    skipped: tuple[SkippedFigure, ...] = ()
    rendered: int = 0
    cached: int = 0
    max_long_edge: int = DEFAULT_MAX_LONG_EDGE
    out_dir: Path | None = None
    json_path: Path | None = None
    warnings: tuple[str, ...] = ()
    message: str = ""

    @property
    def ok(self) -> bool:
        """降级也算成功——少一张预渲染图不该拦下整个产物包（图源本身照进 zh.pdf）。"""
        return self.status in (OK, DEGRADED)

    @property
    def degraded(self) -> bool:
        return self.status == DEGRADED

    def to_figures_json(self) -> dict:
        """按 docs/schemas/figures.schema.json 组装 figures.json 的内容。"""
        return {
            "contract_version": CONTRACT_VERSION,
            "max_long_edge_px": self.max_long_edge,
            "figures": [r.to_json() for r in self.records],
        }

    def to_json(self) -> dict:
        """manifest / report 用的摘要（不含逐图元数据——那是 figures.json 的活）。"""
        data: dict = {
            "status": self.status,
            "figures": len(self.records),
            "rendered": self.rendered,
            "cached": self.cached,
            "skipped": [s.to_json() for s in self.skipped],
            "max_long_edge_px": self.max_long_edge,
        }
        if self.warnings:
            data["warnings"] = list(self.warnings)
        if self.message:
            data["message"] = self.message
        return data


def _read_cache(path: Path, max_long_edge: int) -> dict[str, dict]:
    """读逐图缓存；版本或长边上限对不上就整份作废（重渲比误用便宜）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("cache_version") != CACHE_VERSION:
        return {}
    if data.get("max_long_edge_px") != max_long_edge:
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _write_cache(path: Path, entries: Mapping[str, dict], max_long_edge: int) -> None:
    payload = {
        "cache_version": CACHE_VERSION,
        "max_long_edge_px": max_long_edge,
        "entries": dict(entries),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _stage_cache_hits(
    out: Path, specs: Sequence[FigureSpec], cache: Mapping[str, dict]
) -> dict[str, Path]:
    """把「要换个名字才能用」的缓存文件先快照出来。

    id 是按源码顺序编的，前面插一张图就会让后面全部平移：`fig-002.png` 这一轮成了
    `fig-001.png`，而 `fig-001.png` 这一轮又要写别的图。两张图**互换 id** 时，先写的
    那张会当场毁掉后写那张要读的文件——图还在缓存里，内容却已经被覆盖，且悄无声息。
    故：任何需要改名的命中都先复制到 `.stage-<hash>.png`，读写就此错开。
    """
    staged: dict[str, Path] = {}
    for spec in specs:
        entry = cache.get(spec.sha256)
        if spec.path is None or entry is None or spec.sha256 in staged:
            continue
        current = out / str(entry.get("file", ""))
        if not current.is_file() or current.name == f"{spec.id}.png":
            continue
        stage = out / f"{STAGE_PREFIX}{spec.sha256[:12]}.png"
        try:
            shutil.copyfile(current, stage)
        except OSError:
            continue
        staged[spec.sha256] = stage
    return staged


def _schema_errors(instance, name: str, warnings: list[str]) -> list[str]:
    try:
        return schema_check(instance, name)
    except SchemaError as exc:
        warnings.append(f"跳过 {name}.json 的 schema 校验：{exc}")
        return []


def figures(
    workdir: Workdir,
    blocks,
    *,
    masked: str | None = None,
    plan: ChunkPlan | None = None,
    renderer: Renderer | None = None,
    max_long_edge: int = DEFAULT_MAX_LONG_EDGE,
    src: str | os.PathLike[str] | None = None,
    out_dir: str | os.PathLike[str] | None = None,
    graphicspath: Sequence[str] = (),
    force: bool = False,
) -> FiguresResult:
    """figures 阶段入口：收集 → 预渲染 → 写 `build/figures/`（PNG + figures.json）。

    :param renderer: 注入的渲染器，默认 :func:`default_renderer`（探测外部工具，
        探不到就退到纯 Python 兜底）。
    :param force: 忽略逐图缓存，全部重渲。

    出口判据是机械的：figures.json 过 `docs/schemas/figures.schema.json`。渲染不出来的
    图不进清单，但一定进 `skipped` 与 warnings——**少一张图从不拦流水线**，图源本身
    照样进 zh.pdf（决策 9：编译侧零处理）。
    """
    warnings: list[str] = []
    specs = collect_figures(
        workdir,
        blocks,
        masked=masked,
        plan=plan,
        src=src,
        graphicspath=graphicspath,
        warnings=warnings,
    )

    out = Path(out_dir) if out_dir is not None else workdir.build / FIGURES_DIRNAME
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / CACHE_NAME
    cache = {} if force else _read_cache(cache_path, max_long_edge)
    render = renderer if renderer is not None else default_renderer()

    records: list[FigureRecord] = []
    skipped: list[SkippedFigure] = []
    fresh: dict[str, dict] = {}
    staged = _stage_cache_hits(out, specs, cache)
    rendered = cached = 0

    for spec in specs:
        if spec.path is None:
            skipped.append(
                SkippedFigure(
                    id=spec.id,
                    reason=MISSING_FILE,
                    source=spec.graphic.argument,
                    message="`src/` 下找不到该文件",
                )
            )
            continue
        target = out / f"{spec.id}.png"
        # 本轮刚渲过的同一份源图（同一张图被 \includegraphics 两次）也算命中。
        entry = fresh.get(spec.sha256) or cache.get(spec.sha256)
        result: RenderResult | None = None
        from_cache = False
        if entry is not None:
            hit = staged.get(spec.sha256) or (out / str(entry.get("file", "")))
            if hit.is_file():
                if hit != target:
                    # 图没变、但 id 变了（前面插了一张图）——挪个名字，不算重渲。
                    shutil.copyfile(hit, target)
                result = RenderResult.from_cache(entry)
                from_cache = True
        if result is None:
            result = render.render(spec.path, target, max_long_edge)

        if not result.ok:
            skipped.append(
                SkippedFigure(
                    id=spec.id,
                    reason=result.degradation or RENDER_FAILED,
                    source=spec.rel_path or spec.graphic.argument,
                    message=result.message,
                )
            )
            warnings.append(f"{spec.id}（{spec.rel_path}）没有预渲染：{result.message}")
            target.unlink(missing_ok=True)
            continue

        if from_cache:
            cached += 1
        else:
            rendered += 1
        if result.degradation and not from_cache:
            warnings.append(f"{spec.id}（{spec.rel_path}）降级：{result.message}")
        records.append(
            FigureRecord(spec=spec, render=result, file=target.name, cached=from_cache)
        )
        fresh[spec.sha256] = {**result.to_cache(), "file": target.name}

    # 清扫：上一轮留下的 PNG（id 平移、图被删）与本轮的快照文件都不该活到下一轮。
    keep = {r.file for r in records} | {CACHE_NAME, FIGURES_JSON}
    for stale in list(out.iterdir()):
        if stale.is_file() and stale.name not in keep:
            stale.unlink(missing_ok=True)

    outcome = FiguresResult(
        status=DEGRADED if (skipped or any(r.render.degradation for r in records)) else OK,
        records=tuple(records),
        skipped=tuple(skipped),
        rendered=rendered,
        cached=cached,
        max_long_edge=max_long_edge,
        out_dir=out,
        json_path=out / FIGURES_JSON,
        warnings=tuple(warnings),
    )

    document = outcome.to_figures_json()
    errors = _schema_errors(document, "figures", warnings)
    (out / FIGURES_JSON).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_cache(cache_path, fresh, max_long_edge)

    if errors:
        # 自家组装的产物不合契约 = 本模块的 bug（不是论文的问题），当场判失败。
        return replace(
            outcome,
            status=FAILED,
            warnings=tuple(warnings),
            message="figures.json 不通过 schema 校验：" + errors[0],
        )
    return replace(outcome, warnings=tuple(warnings))
