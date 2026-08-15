"""compile 阶段驱动器：回填 → 注入 → latexmk 回环 → 失败分诊（架构 §3 compile 行、决策 13）。

决策 13 把 assemble 并进了 compile：注入与适配的裁决者本来就是编译，修复会话的常见动作
就是「改注入配置 / 导言区 → 重编译」，回环天然跨越两者，合并后 loop 在这一个驱动器里闭合。
本模块因此负责一整条链：

    译文掩码流 → unmask 回填 → inject_cjk 注入 → build/zh/ 组装（资产 + 字体）
                                              → latexmk 回环 → 失败分诊 → 回退控制

## 失败分诊：全局问题 vs 坏段

首轮编不过时先分诊，因为两类失败的处方完全不同：

* **全局问题**（未知 documentclass、宏冲突、导言区错误）——判据有二：错误落在
  `\\begin{document}` 之前，或者**把译文整个换回原文（恒等回填）也编不过**。后者是关键的
  一刀：译文换回原文之后编译链上只剩注入与环境，与翻译无关。处方是关节⑥（`session`）
  拉起一次适配与修复会话，改完**直接回环重编译**（不重新组装，否则会清掉 agent 的改动）。
* **坏段**——恒等回填能编过，说明是某几段译文的问题。处方是二分定位 + 回退。

## 块 → 段落两级二分（架构 §3 末「chunk 的粒度哲学」）

翻译单元大（章节级），但**回退单元是段落**：validate 强制原译段落一一对应，任何块都能
确定性拆回段落对，于是「大块化不恶化退化粒度」。

    第一级：以块为单位二分，只把候选子集的译文放进去编，定位出坏块；
    第二级：坏块内以段落为单位二分（其余块保持译文、其余坏块整体回原文），定位到段。

二分用的是「半边单独试」的递归而非朴素折半查找——多个坏段并存时朴素折半会漏。两半单独
都编得过则判为交互型失败，整组回退（保守，不损坏）。每次探测都要花一次编译，故设
`budget`（默认 :data:`DEFAULT_BUDGET` 次）：**超限即把当前这一组整块回退**，不无限烧下去。

定位到坏段后：给了 `retranslate`（复用关节⑤）就先重译一次，编过则救活；否则（或仍编不过）
**回退原文段落**并记进 `CompileZhResult.fallbacks`——保证永远出 PDF（架构 §3 出口判据）。

## 「不比原文更糟」

真实论文里带几个 `!` 错误却照样出 PDF 的不少（baseline 因此也只判「出了 PDF」）。若恒等
回填的编译同样带错但出了 PDF，本模块把判据放宽为**错误数不超过恒等回填**——否则整篇论文
会因为原文自带的毛病被判成「翻译失败」，白白全量回退。

## 输入形态

`masked_translated_stream` 接受两种（translate 阶段 M3 落地时给的是第二种）：

* 一整个字符串——译文掩码流。此时块只有一个（id `whole`），原文取 `mask_result.masked`，
  第一级二分退化为空转，第二级段落二分照常；
* 一串 :class:`TranslatedChunk`（或等价的三元组 / dict）——每块带原文与译文，块 id 直接
  进回退清单，与 `chunks.json` 契约对齐。

**不接收 ChunkPlan**：compile 只需要「原文段 ↔ 译文段」这一层对应关系，块的章节树、token
数等分块信息与它无关，少一个依赖少一处耦合。

## 块区间落盘（`build/zh-spans.json`）

回填时 unmask 记下了每个块在输出里的字符区间，注入之后再经 `InjectResult.map_offset`
换算到 `zh.tex` 的坐标系——于是「块 → zh.tex 位置」这件事在本阶段是**已知量**。交付的那
一次编译写完 `zh.tex` 后，把这份区间落成 `build/zh-spans.json`，export 阶段的 anchors 直接
消费，不必再拿块内容去 `zh.tex` 里反查（caption 被翻译后块 tex 已不逐字节存在，反查只能
靠启发式）。

关节⑥的修复会话可以任意改动 `zh.tex`，此时区间随时失效。故落盘前**逐字节比对**磁盘上的
`zh.tex` 与本阶段最后写进去的那一份：不一致就不写、并删掉上一次的旧文件——anchors 那边
自会退回文本查找。宁可少一份精确输入，也不能给出一份错的。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..compiler import (
    DEFAULT_TIMEOUT,
    LOG_TAIL,
    AssetError,
    AssetLinks,
    Compiler,
    CompileRunResult,
    FixupRequest,
    SessionFn,
    latexmk_compiler,
    link_assets,
    link_fonts,
)
from ..validate import PARAGRAPH_SEP_RE
from ..workdir import Workdir
from .inject_cjk import ENGINE, AdaptationTable, InjectError, find_document_start, inject
from .mask import MaskResult
from .unmask import unmask_detail

__all__ = [
    "BadSegment",
    "CompileError",
    "CompileZhResult",
    "DEFAULT_BUDGET",
    "FAILED",
    "Fallback",
    "JOINT",
    "OK",
    "OK_WITH_FALLBACK",
    "RAW_NAME",
    "REASON",
    "SPANS_NAME",
    "STATUSES",
    "TranslatedChunk",
    "WHOLE_ID",
    "ZH_DIRNAME",
    "ZH_TEX",
    "compile_zh",
    "normalize_units",
    "paragraph_pieces",
]


class CompileError(ValueError):
    """调用方把输入喂错了（块清单形状不认识）。编译本身的失败一律走 status，不抛。"""


# --------------------------------------------------------------------- 常量

#: build 区里译文的编译目录与主文件名。
ZH_DIRNAME = "zh"
ZH_TEX = "zh.tex"

#: 回填之后、注入之前的中间产物（决策 13：仍落 `build/` 供调试）。
RAW_NAME = "zh-raw.tex"

#: 块在 `zh.tex` 里的字符区间（见模块文档「块区间落盘」）。不是产物契约的一部分，
#: 只是 compile 交给 export/anchors 的一份已知量，缺席即降级为文本查找。
SPANS_NAME = "zh-spans.json"

#: 编译日志归档位置（相对 `logs/`）。
LOG_NAME = "compile.log"

#: 只给一整个字符串时的块 id。
WHOLE_ID = "whole"

#: 二分的编译预算（次）。一篇论文几十个块、每块几十段，`k·log n` 量级的探测足够定位到
#: 三五个坏段；再多就说明失败是弥散的，整块回退比继续烧编译时间划算。
DEFAULT_BUDGET = 12

# 状态常量（与 `docs/schemas/report.schema.json` 的顶层 status 取值对齐）。
OK = "ok"
OK_WITH_FALLBACK = "ok_with_fallback"
FAILED = "failed"

STATUSES: tuple[str, ...] = (OK, OK_WITH_FALLBACK, FAILED)

#: 回退原因（report.schema.json 的 `fallbacks[].reason` 枚举值）。
REASON = "compile_failed"

#: 本阶段的 agent 关节（`tongtu.agent.JOINTS` 的 ⑥）。
JOINT = "fixup"

#: 一眼即知是全局问题的错误（省掉一次恒等回填探测）。都是「文档还没开始排版就炸了」。
GLOBAL_ERROR_HINTS: tuple[str, ...] = (
    "File `",
    "Option clash",
    "Package fontspec Error",
    "Package xeCJK Error",
    "Missing \\begin{document}",
    "Unknown option",
    "Class ",
)

#: 段落切分：与 `validate.PARAGRAPH_SEP_RE` **同一个正则**（分隔符入捕获组，便于原样重组）。
PARAGRAPH_SPLIT_RE = re.compile(f"({PARAGRAPH_SEP_RE.pattern})")


# ----------------------------------------------------------------- 输入与结果


@dataclass(frozen=True)
class TranslatedChunk:
    """一块的原文与译文（都是**掩码流**片段，占位符尚未回填）。

    `source` 与 `translation` 必须段落数相同——这是 validate 的第 4 层保证，也是段落级
    二分能成立的前提。不相同时本模块降级为整块回退并记警告，绝不猜。
    """

    id: str
    source: str
    translation: str
    section: str | None = None


@dataclass(frozen=True)
class Fallback:
    """一条回退记录，字段与 `report.schema.json` 的 `fallbacks[]` 条目一一对应。"""

    chunk_id: str
    paragraphs: tuple[int, ...] = ()
    """回退的段落序号（块内 0-based）；空 = 整块回退。"""

    reason: str = REASON
    detail: str = ""
    section: str | None = None

    def to_json(self) -> dict:
        data: dict = {"chunk_id": self.chunk_id, "reason": self.reason}
        if self.paragraphs:
            data["paragraphs"] = list(self.paragraphs)
        if self.detail:
            data["detail"] = self.detail
        if self.section:
            data["section"] = self.section
        return data


@dataclass(frozen=True)
class BadSegment:
    """二分定位出的坏段，喂给 `retranslate` 回调（关节⑤复用）。"""

    chunk_id: str
    para_index: int | None
    """块内段落序号；`None` 表示整块（段落对不上或块只有一段）。"""

    source: str
    translation: str
    detail: str = ""
    """编译日志里的第一个 `!` 错误——重译提示词的主要线索。"""

    section: str | None = None


#: 关节⑤的复用形状：拿一个坏段，返回新译文；返回 `None` / 空串 = 放弃，直接回退原文。
RetranslateFn = Callable[[BadSegment], "str | None"]


@dataclass(frozen=True)
class CompileZhResult:
    """compile 阶段的结构化结果。供 report / manifest 与编排器决策。"""

    status: str
    pdf: Path | None = None
    tex: Path | None = None
    raw_tex: Path | None = None
    spans_path: Path | None = None
    """`build/zh-spans.json` 的路径；区间不可信（关节⑥改过 `zh.tex`）或写不下去时为 None。

    刻意不进 :meth:`to_json`：`report.schema.json` 的 `compile` 段是封闭的，而这份文件是
    compile 交给 export 的中间量，不是报告内容。
    """

    build_dir: Path | None = None
    engine: str = ENGINE
    passes: int = 0
    """latexmk 被调用的总次数（首轮 + 分诊 + 二分 + 重译验证 + 终局）。"""

    probes: int = 0
    """其中计入 `budget` 的诊断 / 二分探测次数。"""

    budget_exhausted: bool = False
    fallbacks: tuple[Fallback, ...] = ()
    retranslated: tuple[str, ...] = ()
    """被重译救活的坏段标识（`c003#7` 形式）。"""

    inject: dict = field(default_factory=dict)
    """`InjectResult.to_json()`——注入分支、命中的适配条目、删包剥环境明细。"""

    error_count: int = 0
    first_error: str | None = None
    log_path: Path | None = None
    session_used: int = 0
    """关节⑥被拉起的次数（0 或 1）。"""

    assets: AssetLinks = field(default_factory=AssetLinks)
    warnings: tuple[str, ...] = ()
    message: str = ""

    @property
    def ok(self) -> bool:
        """出了包就算 ok（含有回退段的情形，退出码仍为 0——架构 §6）。"""
        return self.status in (OK, OK_WITH_FALLBACK)

    def to_json(self) -> dict:
        data: dict = {
            "status": self.status,
            "passed": self.ok,
            "engine": self.engine,
            "passes": self.passes,
            "probes": self.probes,
            "error_count": self.error_count,
            "session_used": self.session_used,
            "inject": dict(self.inject),
            "fallbacks": [f.to_json() for f in self.fallbacks],
        }
        if self.pdf is not None:
            data["pdf"] = self.pdf.name
        if self.first_error:
            data["first_error"] = self.first_error
        if self.log_path is not None:
            data["log_path"] = self.log_path.name
        if self.retranslated:
            data["retranslated"] = list(self.retranslated)
        if self.budget_exhausted:
            data["budget_exhausted"] = True
        if self.warnings:
            data["warnings"] = list(self.warnings)
        if self.message:
            data["message"] = self.message
        return data


# ------------------------------------------------------------------- 段落工具


def paragraph_pieces(text: str) -> tuple[list[str], list[int]]:
    """把文本切成「正文 / 分隔符」交替的碎片，并给出哪些碎片是段落。

    返回 `(pieces, para_at)`：`"".join(pieces) == text` 恒成立（原样重组的基础），
    `para_at[i]` 是第 i 段在 `pieces` 里的下标。段落的定义与 `validate.paragraph_count`
    **完全一致**（空行分隔、纯空白不算），因为「原译段落一一对应」正是那一层的保证——
    换一套切分规则就不再对齐了。
    """
    pieces = PARAGRAPH_SPLIT_RE.split(text)
    para_at = [i for i, piece in enumerate(pieces) if i % 2 == 0 and piece.strip()]
    return pieces, para_at


def _shift_spans(spans: Mapping[str, tuple[int, int]], injected) -> dict[str, tuple[int, int]]:
    """把回填侧的块区间换算到注入之后的 `zh.tex` 坐标系。

    注入是若干次切片改写（导言区插块、删包、剥环境），`InjectResult.map_offset` 就是那张
    换算表。止点按**最后一个字符**换算再加一：区间右端恰好落在插入点上时，直接换算会把
    注入块整个吞进来。空区间原样保留（长度为 0，怎么换算都还是空的）。

    换算给出的是块在 `zh.tex` 里**占据的区域**，而不是块文本本身的副本：注入点落在块内部
    时（前导区块就是如此——注入块插在 `\\documentclass` 之后）该区域连注入块一并算进去。
    这是如实描述，且前导区不产锚点，下游不受影响。
    """
    shifted: dict[str, tuple[int, int]] = {}
    for key, (start, end) in spans.items():
        if end <= start:
            shifted[key] = (injected.map_offset(start), injected.map_offset(start))
            continue
        first = injected.map_offset(start)
        last = injected.map_offset(end - 1) + 1
        shifted[key] = (first, max(first, last))
    return shifted


class _Original:
    """「这一块整个用原文」的哨兵（比 `None` 可读，也不与「空覆盖」混淆）。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可读性
        return "<original>"


ORIGINAL = _Original()


@dataclass
class _Unit:
    """一块及其段落切分（原文侧与译文侧各一份）。"""

    chunk: TranslatedChunk
    trans_pieces: list[str]
    trans_at: list[int]
    src_pieces: list[str]
    src_at: list[int]

    @classmethod
    def of(cls, chunk: TranslatedChunk) -> _Unit:
        trans_pieces, trans_at = paragraph_pieces(chunk.translation)
        src_pieces, src_at = paragraph_pieces(chunk.source)
        return cls(chunk, trans_pieces, trans_at, src_pieces, src_at)

    @property
    def id(self) -> str:
        return self.chunk.id

    @property
    def paragraph_count(self) -> int:
        return len(self.trans_at)

    @property
    def splittable(self) -> bool:
        """段落数对得上且不止一段——段落级二分的前提（validate 已保证前者）。"""
        return len(self.trans_at) == len(self.src_at) and len(self.trans_at) > 1

    def source_paragraph(self, index: int) -> str:
        return self.src_pieces[self.src_at[index]]

    def translation_paragraph(self, index: int) -> str:
        return self.trans_pieces[self.trans_at[index]]

    def render(self, state) -> str:
        """按取用方式渲染这一块。

        `state` 四形态：`None` = 全译；:data:`ORIGINAL` = 全原文；`dict[段序号, 文本]`
        = 段落级覆盖（二分与回退走这条）；`str` = 整块换成这段文本（整块重译走这条）。
        """
        if state is None:
            return self.chunk.translation
        if isinstance(state, _Original):
            return self.chunk.source
        if isinstance(state, str):
            return state
        if not state:
            return self.chunk.translation
        # 全部段落都换回原文时直接给原文，逐字节等于 `source`（恒等回填要的就是这个）。
        if len(state) == len(self.trans_at) == len(self.src_at) and all(
            state.get(i) == self.source_paragraph(i) for i in range(len(self.trans_at))
        ):
            return self.chunk.source
        pieces = list(self.trans_pieces)
        for index, text in state.items():
            pieces[self.trans_at[index]] = text
        return "".join(pieces)


def normalize_units(masked_translated_stream, mask_result: MaskResult) -> tuple[TranslatedChunk, ...]:
    """把各种形状的译文输入规范成块序列（见模块文档「输入形态」）。

    认得：`str`、`TranslatedChunk`、`(id, source, translation)` 三元组、
    含 `id`/`source`/`translation` 键的 mapping（`original`/`text` 作别名）。
    """
    if isinstance(masked_translated_stream, str):
        return (
            TranslatedChunk(
                id=WHOLE_ID,
                source=mask_result.masked,
                translation=masked_translated_stream,
            ),
        )
    if isinstance(masked_translated_stream, Mapping):
        raise CompileError("译文块清单不能是 mapping（块的顺序是有意义的）：请给 TranslatedChunk 序列")
    if not isinstance(masked_translated_stream, Sequence) and not isinstance(masked_translated_stream, Iterable):
        raise CompileError(f"无法识别的译文输入：{type(masked_translated_stream).__name__}")

    units: list[TranslatedChunk] = []
    for index, item in enumerate(masked_translated_stream):
        if isinstance(item, TranslatedChunk):
            units.append(item)
        elif isinstance(item, Mapping):
            try:
                units.append(
                    TranslatedChunk(
                        id=str(item.get("id") or f"c{index:03d}"),
                        source=item["source"] if "source" in item else item["original"],
                        translation=(item["translation"] if "translation" in item else item["text"]),
                        section=item.get("section"),
                    )
                )
            except KeyError as exc:
                raise CompileError(f"第 {index} 块缺字段 {exc}") from exc
        elif isinstance(item, (tuple, list)) and len(item) == 3:
            units.append(TranslatedChunk(id=str(item[0]), source=item[1], translation=item[2]))
        else:
            raise CompileError(f"第 {index} 块的形状不认识：{type(item).__name__}")
    if not units:
        raise CompileError("译文块清单为空")
    return tuple(units)


# --------------------------------------------------------------------- 驱动器


class _BudgetExhausted(RuntimeError):
    """二分预算用完——当前这一组整块回退（不是错误，是设计好的降级）。"""


class _Driver:
    """一次 compile 回环的可变状态：组装、编译、计数、判据。"""

    def __init__(
        self,
        workdir: Workdir,
        units: Sequence[TranslatedChunk],
        mask_result: MaskResult,
        *,
        compiler: Compiler,
        adaptation: AdaptationTable | None,
        fonts: str | os.PathLike[str] | None,
        budget: int,
    ) -> None:
        self.workdir = workdir
        self.units = [_Unit.of(chunk) for chunk in units]
        self.mask_result = mask_result
        self.compiler = compiler
        self.adaptation = adaptation
        self.fonts = fonts
        self.budget = max(0, budget)

        self.build_dir = workdir.build / ZH_DIRNAME
        self.tex = self.build_dir / ZH_TEX
        self.raw_tex = workdir.build / RAW_NAME
        self.spans_file = workdir.build / SPANS_NAME
        self.passes = 0
        self.probes = 0
        self.exhausted = False
        self.tolerate: int | None = None
        self.warnings: list[str] = []
        self.inject_json: dict = {}
        self.engine = ENGINE
        self.assets = AssetLinks()
        self.last: CompileRunResult | None = None
        self.tex_text = ""
        self.block_spans: dict[str, tuple[int, int]] = {}
        self._unmask_warned = False

    # -- 组装 ---------------------------------------------------------------

    def setup(self) -> None:
        """建 build/zh/、链资产与字体。字体缺失只记警告——编译才是裁决者。"""
        self.build_dir.mkdir(parents=True, exist_ok=True)
        self.assets = link_assets(
            self.workdir.src,
            self.build_dir,
            root=self.workdir.path,
            skip=frozenset({ZH_TEX, RAW_NAME}),
        )
        self.warnings.extend(self.assets.warnings)
        try:
            link_fonts(self.build_dir, self.fonts)
        except AssetError as exc:
            self.warnings.append(f"{exc}（中文可能排不出来，先编了再说）")
        else:
            if "fonts" in self.assets.linked or "fonts" in self.assets.copied:
                self.warnings.append(
                    "源码里也有 fonts/ 目录，已被仓库字体目录覆盖（inject_cjk 的 Path={fonts/} 指的是仓库字体）"
                )

    def stream(self, states: Sequence) -> str:
        return "".join(unit.render(state) for unit, state in zip(self.units, states, strict=True))

    def write(self, states: Sequence) -> None:
        """掩码流 → unmask 回填 → inject 注入 → 落 `build/zh-raw.tex` 与 `build/zh/zh.tex`。"""
        # 宽松回填：占位符残缺时不抛栈，留给编译当场暴露——这一阶段的裁决者是编译，
        # 而且二分正好能把「哪一段把占位符弄坏了」定位出来。
        detail = unmask_detail(self.stream(states), self.mask_result, strict=False)
        if not self._unmask_warned and (detail.missing or detail.duplicated):
            self._unmask_warned = True
            if detail.missing:
                self.warnings.append(f"译文流丢了 {len(detail.missing)} 个块占位符")
            if detail.duplicated:
                self.warnings.append(f"译文流重复使用了 {len(detail.duplicated)} 个块占位符")
        self.raw_tex.parent.mkdir(parents=True, exist_ok=True)
        self.raw_tex.write_text(detail.text, encoding="utf-8")

        injected = inject(detail.text, adaptation=self.adaptation)
        self.inject_json = injected.to_json()
        self.engine = injected.engine
        for warning in injected.warnings:
            if warning not in self.warnings:
                self.warnings.append(f"inject: {warning}")
        self.tex_text = injected.text
        self.block_spans = _shift_spans(detail.block_spans, injected)
        self.tex.write_text(injected.text, encoding="utf-8")

    # -- 块区间 -------------------------------------------------------------

    def save_spans(self) -> Path | None:
        """把块区间落 `build/zh-spans.json`（见模块文档）。

        磁盘上的 `zh.tex` 与本阶段最后写进去的那一份对不上（关节⑥改过）时不写，并删掉
        旧文件——错的区间比没有区间更糟。写不下去（只读盘）同样如实返回 None。
        """
        try:
            if not self.block_spans or self.tex.read_text(encoding="utf-8") != self.tex_text:
                self.spans_file.unlink(missing_ok=True)
                return None
            self.spans_file.parent.mkdir(parents=True, exist_ok=True)
            self.spans_file.write_text(
                json.dumps(
                    {
                        "tex": ZH_TEX,
                        "blocks": {key: [start, end] for key, (start, end) in sorted(self.block_spans.items())},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            return None
        return self.spans_file

    # -- 编译 ---------------------------------------------------------------

    def run(self) -> CompileRunResult:
        """原地编译当前的 `build/zh/zh.tex`（修复会话之后走这条，不重新组装）。"""
        result = self.compiler(self.tex, self.build_dir)
        self.passes += 1
        self.last = result
        return result

    def compile(self, states: Sequence) -> CompileRunResult:
        self.write(states)
        return self.run()

    def charged(self, states: Sequence) -> CompileRunResult:
        """计入二分预算的一次编译。预算用完抛 :class:`_BudgetExhausted`。"""
        if self.exhausted or self.probes >= self.budget:
            self.exhausted = True
            raise _BudgetExhausted
        self.probes += 1
        return self.compile(states)

    def passed(self, result: CompileRunResult) -> bool:
        """编译算不算过。`tolerate` 生效时改判「不比原文更糟」（见模块文档）。"""
        if self.tolerate is None:
            return result.ok
        return result.has_pdf and result.error_count <= self.tolerate

    def relax(self, identity: CompileRunResult) -> None:
        """原文自身即带错却出了 PDF → 放宽判据为「错误数不超过它」。"""
        self.tolerate = identity.error_count
        self.warnings.append(
            f"恒等回填（全原文）自身即带 {identity.error_count} 个 ! 错误但出了 PDF，判据放宽为「译文不比原文更糟」"
        )

    def probe_fails(self, states: Sequence) -> bool:
        """探测：这个配置**编不过**吗（True = 编不过）。"""
        return not self.passed(self.charged(states))

    # -- 二分 ---------------------------------------------------------------

    def locate(self, items: list, probe: Callable[[list], bool]) -> list:
        """递归二分定位坏项。`probe(subset)` 为 True 表示只放这一组进去就编不过。

        前提：这一组整体已知编不过（首轮全译失败，或上一层已探测过）。**两半分别单独试**
        而不是朴素折半查找——多个坏项并存时朴素折半会漏掉后一半里的。

        两处省编译（每次探测都是一次真编译，省一次是一次）：

        * 前一半单独编得过、且还没找到任何坏项时，坏项必在后一半（或是交互型失败），
          直接下钻不再验证；
        * 单元素组不验证，直接判坏——它的正确性由**终局编译**兜底：终局若没兜住，
          调用方会把坏块整块回退再编一次。

        两半单独都编得过 → 交互型失败，整组判坏（保守，不损坏）。
        预算耗尽 → 当前组整组判坏（对应「超限则整块回退」）。
        """
        if len(items) <= 1 or self.exhausted:
            return list(items)
        mid = len(items) // 2
        halves = (items[:mid], items[mid:])
        bad: list = []
        for position, part in enumerate(halves):
            if position == len(halves) - 1 and not bad:
                bad.extend(self.locate(part, probe))
                break
            try:
                failed = probe(part)
            except _BudgetExhausted:
                self.exhausted = True
                bad.extend(part)
                continue
            if failed:
                bad.extend(self.locate(part, probe))
        return bad if bad else list(items)

    # -- 收尾 ---------------------------------------------------------------

    def archive_log(self) -> Path | None:
        if self.last is None or not self.last.log:
            return None
        try:
            self.workdir.logs.mkdir(parents=True, exist_ok=True)
            path = self.workdir.logs / LOG_NAME
            path.write_text(self.last.log, encoding="utf-8")
            return path
        except OSError:
            return self.last.log_path


# ----------------------------------------------------------------- 分诊辅助


def is_preamble_error(result: CompileRunResult, tex: str) -> bool:
    """错误是否落在前导区（全局问题的判据之一，省掉一次恒等回填探测）。

    两条线索：第一个 `!` 错误的措辞（缺文件、选项冲突这类与译文无关的），以及紧随其后的
    `l.<N>` 行号是否早于 `\\begin{document}` 所在行。
    """
    summary = result.summary
    first = summary.first_error or ""
    if first and any(hint in first for hint in GLOBAL_ERROR_HINTS):
        return True
    if summary.error_line is None or not tex:
        return False
    start = find_document_start(tex)
    if start is None:
        return False
    return summary.error_line <= tex.count("\n", 0, start) + 1


def _fixup_prompt(driver: _Driver, result: CompileRunResult, reason: str) -> str:
    """关节⑥的提示词：documentclass 适配与编译修复合一（决策 13）。"""
    return (
        "翻译后的论文编译失败，需要你做 documentclass 适配 / 编译修复。\n\n"
        f"- 主文件：{driver.tex}\n"
        f"- 编译目录：{driver.build_dir}（`src/` 资产与仓库 fonts/ 已链接在此）\n"
        f"- 引擎：latexmk -{driver.engine}\n"
        f"- 分诊结论：{reason}\n"
        f"- 第一个错误：{result.first_error or '（日志里没有 ! 错误）'}\n\n"
        "这是全局问题（不是某一段译文的问题），常见成因：documentclass 不认识 xeCJK 的"
        "注入块、宏包冲突、导言区被适配表改坏。你可以：\n"
        "- 改 `build/zh/zh.tex` 的导言区（注入块夹在 tongtu 标记注释之间）；\n"
        "- 或改 `tongtu/data/documentclass.json` 适配表，让这一类 documentclass 以后都对"
        "（成功的适配按促升规则沉淀成数据条目，别把一次性 hack 写进编排器）；\n"
        "- 不要改动正文译文，也不要删掉中文支持；\n"
        "- 改完不用自己下结论——脚本会重新编译一次，编译是唯一的裁决者。\n\n"
        f"日志尾部：\n{result.log_tail}\n"
    )


def _apply(states: list, key: tuple[int, int | None], text: str) -> None:
    unit_index, para_index = key
    if para_index is None:
        states[unit_index] = text
        return
    current = states[unit_index]
    overrides = dict(current) if isinstance(current, dict) else {}
    overrides[para_index] = text
    states[unit_index] = overrides


def _copy_states(states: Sequence) -> list:
    return [dict(state) if isinstance(state, dict) else state for state in states]


# ------------------------------------------------------------------- 阶段入口


def compile_zh(
    workdir: Workdir,
    masked_translated_stream,
    mask_result: MaskResult,
    *,
    compiler: Compiler | None = None,
    retranslate: RetranslateFn | None = None,
    session: SessionFn | None = None,
    adaptation: AdaptationTable | None = None,
    budget: int = DEFAULT_BUDGET,
    fonts: str | os.PathLike[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> CompileZhResult:
    """回填 + 注入 + 编译回环，产出 `build/zh/zh.pdf`。

    :param masked_translated_stream: 译文掩码流（整串）或译文块序列，见模块文档。
    :param mask_result: mask 的产物；`masked` 同时充当二分时的原文侧。
    :param compiler: 注入的编译器（默认真 latexmk -xelatex）。全部分诊 / 二分 / 回退逻辑
        都只经由这个接口触碰 TeX，故可以在没有 TeX 的机器上完整单测。
    :param retranslate: 关节⑤复用——坏段先重译一次；`None`（默认）直接回退原文。
    :param session: 关节⑥——全局问题时拉起一次适配与修复会话，改完直接回环重编译。
    :param adaptation: documentclass 适配表（默认读数据文件）。
    :param budget: 二分探测的编译次数上限；超限则当前组整块回退。
    """
    units = normalize_units(masked_translated_stream, mask_result)
    driver = _Driver(
        workdir,
        units,
        mask_result,
        compiler=compiler if compiler is not None else latexmk_compiler(ENGINE, timeout=timeout),
        adaptation=adaptation,
        fonts=fonts,
        budget=budget,
    )
    joined = "".join(unit.source for unit in units)
    if joined != mask_result.masked:
        driver.warnings.append("块原文拼接与 mask 的掩码流不一致（分块或译文流对不上），二分仍按块原文进行")

    fallbacks: list[Fallback] = []
    retranslated: list[str] = []
    session_used = 0

    def finish(status: str, *, message: str = "") -> CompileZhResult:
        last = driver.last
        return CompileZhResult(
            status=status,
            pdf=last.pdf if last is not None and last.has_pdf else None,
            tex=driver.tex,
            raw_tex=driver.raw_tex,
            spans_path=driver.save_spans(),
            build_dir=driver.build_dir,
            engine=driver.engine,
            passes=driver.passes,
            probes=driver.probes,
            budget_exhausted=driver.exhausted,
            fallbacks=tuple(fallbacks),
            retranslated=tuple(retranslated),
            inject=dict(driver.inject_json),
            error_count=last.error_count if last is not None else 0,
            first_error=last.first_error if last is not None else None,
            log_path=driver.archive_log(),
            session_used=session_used,
            assets=driver.assets,
            warnings=tuple(driver.warnings),
            message=message,
        )

    driver.setup()
    translated_states: list = [None] * len(units)
    original_states: list = [ORIGINAL] * len(units)

    try:
        first = driver.compile(translated_states)
    except InjectError as exc:
        # 注入失败与译文无关（找不到 documentclass、适配表损坏），二分毫无意义。
        return finish(FAILED, message=f"inject_cjk 失败：{exc}")

    if driver.passed(first):
        return finish(OK)

    if first.missing_tool:
        return finish(FAILED, message=first.message)

    def global_fixup(states: list, result: CompileRunResult, reason: str) -> CompileZhResult:
        """关节⑥：拉起一次修复会话，改完**原地**重编译（不重新组装，保住 agent 的改动）。"""
        nonlocal session_used
        if session is None:
            return finish(
                FAILED,
                message=(f"{reason}；没有修复会话（关节⑥）可用，无法继续（第一个错误：{result.first_error}）"),
            )
        driver.write(states)  # 让 agent 看到要交付的那一份，而不是二分中途的配置
        session(
            FixupRequest(
                joint=JOINT,
                prompt=_fixup_prompt(driver, result, reason),
                workdir=workdir,
                build_dir=driver.build_dir,
                tex=driver.tex,
                engine=driver.engine,
                log=result.log[-LOG_TAIL:],
                first_error=result.first_error,
                attempt=1,
            )
        )
        session_used = 1
        after = driver.run()
        if driver.passed(after):
            return finish(OK_WITH_FALLBACK if fallbacks else OK)
        return finish(
            FAILED,
            message=(f"{reason}；修复会话之后仍编不过（第一个错误：{after.first_error or result.first_error}）"),
        )

    # --- 分诊：全局问题 vs 坏段 ------------------------------------------
    if is_preamble_error(first, driver.tex_text):
        return global_fixup(translated_states, first, "错误落在前导区")

    try:
        identity = driver.charged(original_states)
    except _BudgetExhausted:
        # 预算连分诊都不够（budget=0）：直接交付恒等回填——保证出 PDF，全部块记回退。
        identity = driver.compile(original_states)
        if driver.passed(identity):
            fallbacks.extend(
                Fallback(
                    chunk_id=unit.id,
                    reason=REASON,
                    detail="二分预算为 0，未定位坏段，整篇回退原文",
                    section=unit.chunk.section,
                )
                for unit in driver.units
            )
            return finish(OK_WITH_FALLBACK)
        return global_fixup(translated_states, identity, "预算不足以分诊，且恒等回填也编不过")

    if not driver.passed(identity) and identity.has_pdf and identity.error_count:
        driver.relax(identity)
        if driver.passed(first):
            # 放宽后首轮其实就是过的，回到全译配置交付。
            final = driver.compile(translated_states)
            if driver.passed(final):
                return finish(OK)

    if not driver.passed(identity):
        return global_fixup(translated_states, identity, "换回恒等回填（全原文）也编不过")

    # --- 第一级：块二分 ---------------------------------------------------
    def unit_probe(subset: list[int]) -> bool:
        states = [None if i in set(subset) else ORIGINAL for i in range(len(units))]
        return driver.probe_fails(states)

    bad_units = driver.locate(list(range(len(units))), unit_probe)

    # --- 第二级：坏块内的段落二分 ----------------------------------------
    base: list = [None] * len(units)
    for index in bad_units:
        base[index] = ORIGINAL

    bad_paragraphs: dict[int, list[int] | None] = {}
    for index in bad_units:
        unit = driver.units[index]
        if driver.exhausted:
            # 预算超限 → 不再下钻，整块回退（保守，且保证还能出 PDF）
            bad_paragraphs[index] = None
            continue
        if not unit.splittable:
            if unit.paragraph_count and len(unit.trans_at) != len(unit.src_at):
                driver.warnings.append(
                    f"块 {unit.id} 的原译段落数对不上（{len(unit.src_at)} vs {len(unit.trans_at)}），只能整块回退"
                )
            bad_paragraphs[index] = None
            continue

        paragraphs = list(range(unit.paragraph_count))

        def para_probe(subset: list[int], _index: int = index, _unit: _Unit = unit) -> bool:
            keep = set(subset)
            states = _copy_states(base)
            states[_index] = {p: _unit.source_paragraph(p) for p in range(_unit.paragraph_count) if p not in keep}
            return driver.probe_fails(states)

        bad_paragraphs[index] = driver.locate(paragraphs, para_probe)

    # --- 终局配置：坏段回退原文 ------------------------------------------
    final_states: list = [None] * len(units)
    segments: list[tuple[int, int | None]] = []
    for index in bad_units:
        unit = driver.units[index]
        paragraphs = bad_paragraphs.get(index)
        if paragraphs is None:
            final_states[index] = ORIGINAL
            segments.append((index, None))
            continue
        final_states[index] = {p: unit.source_paragraph(p) for p in paragraphs}
        segments.extend((index, p) for p in paragraphs)

    detail = first.first_error or ""

    # --- 坏段重译一次（关节⑤复用）----------------------------------------
    saved: set[tuple[int, int | None]] = set()
    if retranslate is not None and segments and not driver.exhausted:
        candidates: dict[tuple[int, int | None], str] = {}
        for key in segments:
            index, para = key
            unit = driver.units[index]
            segment = BadSegment(
                chunk_id=unit.id,
                para_index=para,
                source=unit.chunk.source if para is None else unit.source_paragraph(para),
                translation=(unit.chunk.translation if para is None else unit.translation_paragraph(para)),
                detail=detail,
                section=unit.chunk.section,
            )
            try:
                text = retranslate(segment)
            except Exception as exc:  # 回调炸了不该拖垮编译：回退原文继续
                driver.warnings.append(f"重译 {_label(unit.id, para)} 抛异常（{exc}），回退原文")
                continue
            if text and text.strip() and text != segment.translation:
                candidates[key] = text

        if candidates:
            trial = _copy_states(final_states)
            for key, text in candidates.items():
                _apply(trial, key, text)
            try:
                if not driver.probe_fails(trial):
                    saved.update(candidates)
                    final_states = trial
                elif len(candidates) > 1:
                    # 一起上没过：逐段试，救一个是一个（预算允许的范围内）。
                    for key, text in candidates.items():
                        single = _copy_states(final_states)
                        _apply(single, key, text)
                        if not driver.probe_fails(single):
                            saved.add(key)
                            final_states = single
            except _BudgetExhausted:
                driver.warnings.append("重译验证时预算耗尽，未被验证的重译一律回退原文")

    # 同一块的多个坏段合成一条记录——`report.schema.json` 的 `fallbacks[].paragraphs`
    # 本就是数组，`paragraphs` 缺省即整块回退。
    by_chunk: dict[int, list[int | None]] = {}
    for index, para in segments:
        if (index, para) in saved:
            retranslated.append(_label(driver.units[index].id, para))
            continue
        by_chunk.setdefault(index, []).append(para)
    for index, paras in by_chunk.items():
        unit = driver.units[index]
        fallbacks.append(
            Fallback(
                chunk_id=unit.id,
                paragraphs=tuple(p for p in paras if p is not None),
                reason=REASON,
                detail=detail,
                section=unit.chunk.section,
            )
        )

    # --- 终局编译（不计预算：这一次是交付，不是探测）----------------------
    final = driver.compile(final_states)
    if driver.passed(final):
        return finish(OK_WITH_FALLBACK if fallbacks else OK)

    # 二分的结论没兜住（交互型失败）→ 坏块整块回退再试一次。
    escalated: list = [None] * len(units)
    for index in bad_units:
        escalated[index] = ORIGINAL
    if escalated != final_states:
        final = driver.compile(escalated)
        if driver.passed(final):
            retranslated.clear()
            fallbacks = [
                Fallback(
                    chunk_id=driver.units[index].id,
                    reason=REASON,
                    detail="；".join(filter(None, (detail, "段落级回退未兜住，整块回退"))),
                    section=driver.units[index].chunk.section,
                )
                for index in bad_units
            ]
            return finish(OK_WITH_FALLBACK)

    return global_fixup(escalated, final, "坏段回退之后仍编不过")


def _label(chunk_id: str, para: int | None) -> str:
    return chunk_id if para is None else f"{chunk_id}#{para}"
