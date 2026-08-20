r"""章节树优先分块：把掩码文本按章节结构切成大小受控的 chunk 序列。

本模块是核心文本层，只用标准库与同层的 `masking`，不做文件读写。阶段驱动器在
`tongtu/stages/chunk.py`，设计是 `docs/stages/chunk.md`。

一次线性扫描产出后续全部决策所需的信息：环境的 `\begin` / `\end` 位置、标题命令位置、
appendix 标记位置、空行分隔符位置。词法规则复用 `masking` 的公开原语，本模块不另写第二份。
扫描的前提是掩码文本的一条性质——凡留在掩码文本里的 `\begin` / `\end` 一律配对：
non-translatable 环境、注释、verbatim 环境与 display math 在 mask 阶段已成 placeholder 或整块
摘除。配对失败（深度转负，或文件尾深度不为零）抛 `ChunkError`，不做保守回退。

定级两步取出首选层级：先取深度 0 处出现过的最浅层级，取不到时取全文出现过的最浅层级。
定级后，体内出现首选层级标题命令的环境标为透明，不计入深度；以透明集重算的深度称有效深度，
段落切分、标题边界与 appendix 标记识别都以有效深度 0 为准。透明集在定级时求出后固定，下分
沿用。

分块规则只有三条，没有针对具体命令名的分支：按首选层级划单元；单元超过 `SPLIT_ABOVE` 就
往更深层级拆；不足 `MERGE_BELOW` 的吸收相邻单元。一节就是一个 chunk——节间衔接在论文中本来
就弱，跨节保持一致的只有术语与记号，那由 glossary 与 brief 承担，把多节攒进一个 chunk 换不
到质量，只换到更少的会话次数。

chunk 首尾相接拼起来逐字符等于掩码文本：每个 chunk 是掩码文本的切片，不追加任何字符。
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from . import masking

#: token 估算系数：估算值 = 字符数除以它再向上取整。首次真实论文翻译之前用真实 tokenizer
#: 对掩码文本离线实测后冻结，这里的起步值与下面三个上限同为分块决策的输入。
CHARS_PER_TOKEN = 4

#: 下分线：单个单元超过它才向更深层级下分。一节就是一个 chunk，这条线只是安全阀——
#: 它约束的是单次翻译会话的输出量，不是分块目标。
SPLIT_ABOVE = 5000

#: 合并线：估算值低于它的 chunk 吸收相邻 chunk（同 part 且合并后不超过 SPLIT_ABOVE）。
#: 它收拾的是碎片（区界标记行、只有标题的过渡节、作者写得很短的节），不是把小节攒成大块。
MERGE_BELOW = 1500

#: 标题层级序列，由浅到深；星号变体等同于无星形式。
HEADING_LEVELS: tuple[str, ...] = ("part", "chapter", "section", "subsection", "subsubsection", "paragraph")

#: appendix 标记的两个命令名（`\appendices` 是 IEEEtran 的写法）与一个环境名（appendix 宏包）。
APPENDIX_COMMANDS: tuple[str, ...] = ("appendix", "appendices")
APPENDIX_ENVIRONMENT = "appendices"

#: 扫描里需要停下判断的字符：控制序列与 math 定界符。掩码文本里没有注释（mask 已把注释整块
#: 摘出），故不含 `%`。其余字符一律跳过，不逐字符步进。
SPECIAL_RE = re.compile(r"[\\$]")


class ChunkError(Exception):
    """分块失败：扫描的结构错误，或定级的兜底硬判据不通过。"""


class Part(StrEnum):
    """chunk 所属的区，按 chunk 起始偏移对照区界判定。"""

    FRONT = "front"
    BODY = "body"
    APPENDIX = "appendix"


class AppendixSource(StrEnum):
    r"""appendix 区起点的识别来路：`\appendix` / `\appendices` 命令，appendix 宏包的环境，或没有。"""

    COMMAND = "command"
    ENVIRONMENT = "environment"
    ABSENT = "absent"


@dataclass(frozen=True)
class Heading:
    """一条标题：命令名与它的参数原文。命令名即层级，取值来自 `HEADING_LEVELS`。"""

    level: str
    argument: str


@dataclass(frozen=True)
class DocumentHeading:
    r"""全文标题树的一条：命令名、参数原文与层级深度。

    `depth` 是相对深度：全文出现过的最浅层级记 1，往深一级加一（多数论文 section 是 1、
    subsection 是 2）。取相对值而不是 `HEADING_LEVELS` 的绝对下标，是因为论文用不用
    `\part` / `\chapter` 因文档类而异，绝对下标会让同样形状的两篇论文得出不同的数。
    """

    level: str
    argument: str
    depth: int


@dataclass(frozen=True)
class Chunk:
    """一个 chunk 在掩码文本中的位置与它的定级结论，内容即 `masked[start:end]`。"""

    start: int
    end: int
    part: Part
    headings: tuple[Heading, ...]
    internal_cuts: tuple[int, ...]


@dataclass(frozen=True)
class ChunkingOutcome:
    """分块结果：chunk 序列与定级结论。"""

    chunks: tuple[Chunk, ...]
    heading_level: str | None
    transparent_environments: tuple[str, ...]
    appendix_source: AppendixSource


def estimate_tokens(text: str) -> int:
    """token 估算：字符数除以 `CHARS_PER_TOKEN` 向上取整，只用于分块决策。"""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def paragraphs(text: str) -> list[str]:
    """按空行切分、逐段剥除首尾空白、丢弃空段，返回段落列表。

    分块的段落计数、validate 的段落比对与 translate 的 neighbors 取段共用这一条切分规则：
    同一条规则各写一遍，日后差一个字符就是三处口径不一致。
    """
    return [paragraph.strip() for paragraph in masking.BLANK_LINE_RE.split(text) if paragraph.strip()]


def count_paragraphs(text: str) -> int:
    """段落计数：全部非空段落。

    真实语料里连续空行常见，空段若计入，translate 的段落数比对就在要求译文保持同样多的连续
    空行，而模型合并连续空行是最常见的无害改动。含可译文本的段落是另一个口径，在
    `tongtu/validation.py`，两者的区别见 docs/stages/chunk.md 段落计数的两个口径节。
    """
    return len(paragraphs(text))


def translatable_chars(text: str) -> int:
    """剥除 placeholder 后的非空白字符数；纯 placeholder chunk 的判定依据。"""
    return sum(1 for character in masking.TOKEN_RE.sub("", text) if not character.isspace())


def document_headings(masked: str) -> tuple[DocumentHeading, ...]:
    """扫出全文有效深度 0 的标题，按文档序，每条带相对层级深度。

    标题结构是分块的输入而不是分块的产物：定级、切点与区界都建立在同一次扫描上。survey 用
    本函数把标题树写进 `brief.json`（translate 在 front chunk 的提示词里引用它），chunk 用
    同一份扫描切块，两个阶段因此不互相依赖，都只依赖 `masked.tex`。

    有效深度的口径与分块一致：体内出现首选层级标题的环境判为透明、不计入深度，故被
    `CJK*` 一类包裹环境裹住的标题照样在树里。全文一个标题命令都没有时返回空元组。
    """
    run = _chunk_run(masked)
    headings = [heading for heading in run.scan.headings if run.depth.at(heading.start) == 0]
    if not headings:
        return ()
    shallowest = min(HEADING_LEVELS.index(heading.level) for heading in headings)
    return tuple(
        DocumentHeading(
            level=heading.level,
            argument=heading.argument,
            depth=HEADING_LEVELS.index(heading.level) - shallowest + 1,
        )
        for heading in headings
    )


def split_document(masked: str) -> ChunkingOutcome:
    """把掩码文本切成 chunk 序列；扫描的结构错误与定级的兜底硬判据不通过时抛 `ChunkError`。"""
    return _chunk_run(masked).run()


def _chunk_run(masked: str) -> _ChunkRun:
    """扫描掩码文本；词法原语抛的 `MaskError` 在此转成 `ChunkError`。

    错在掩码文本的词法遍历，报错要落在调用方阶段的名下，不能让 manifest 的 message 指向
    mask。`document_headings` 与 `split_document` 都经由这里，survey 与 chunk 对同一份坏输入
    因此报同一类错。
    """
    try:
        return _ChunkRun(masked)
    except masking.MaskError as error:
        raise ChunkError(f"掩码文本的词法遍历失败：{error}") from error


# ------------------------------------------------------------------ 扫描层


@dataclass(frozen=True)
class _Heading:
    """一处标题命令：反斜杠偏移、层级（星号已去）、必选参数原文与所在环境深度。

    `depth` 是扫描当时的环境栈高度，即所有环境都计入的深度，定级第一步用它；透明集求出之后
    的有效深度另算（`_Depth`）。
    """

    start: int
    level: str
    argument: str
    depth: int


@dataclass(frozen=True)
class _Environment:
    r"""一个环境实例：环境名与它的体（`\begin` 之后到配对 `\end` 的反斜杠偏移）。"""

    name: str
    body_start: int
    body_end: int


@dataclass(frozen=True)
class _Scan:
    """一次线性扫描的产出。"""

    headings: tuple[_Heading, ...]
    environments: tuple[_Environment, ...]
    appendix_marks: tuple[tuple[int, AppendixSource], ...]
    paragraph_starts: tuple[int, ...]


def _scan(text: str) -> _Scan:
    r"""线性扫描掩码文本，收集标题、环境、appendix 标记与段落分隔符位置。

    inline math（`$…$` 与 `\(…\)`）与 `\verb` 的内容整段跳过，其中的 `\begin`（如 `$…$` 里的
    `pmatrix`）不参与计数。`\begin` / `\end` 用栈配对，深度转负或文件尾深度不为零即结构错误。
    """
    headings: list[_Heading] = []
    environments: list[_Environment] = []
    appendix_marks: list[tuple[int, AppendixSource]] = []
    stack: list[tuple[str, int]] = []
    position = 0
    while True:
        match = SPECIAL_RE.search(text, position)
        if match is None:
            break
        position = match.start()
        if text[position] == "$":
            # 掩码文本里的 display math 已整块成 placeholder，留下的 `$` 一律是 inline math。
            position = masking.find_inline_dollar_close(text, position + 1)
            continue
        name, after_name = masking.read_control_sequence(text, position)
        if name == "verb":
            position = masking.skip_verb(text, after_name)
        elif name == "(":
            position = masking.skip_to_delimiter(text, after_name, "\\)", "\\(")
        elif name == "begin":
            environment, after = masking.read_environment_name(text, after_name)
            if environment is None:
                position = after_name
                continue
            if environment == APPENDIX_ENVIRONMENT:
                appendix_marks.append((position, AppendixSource.ENVIRONMENT))
            stack.append((environment, after))
            position = after
        elif name == "end":
            environment, after = masking.read_environment_name(text, after_name)
            if environment is None:
                position = after_name
                continue
            if not stack:
                raise ChunkError(f"偏移 {position} 处的 \\end{{{environment}}} 没有对应的 \\begin，环境配对不上")
            opened, body_start = stack.pop()
            if opened != environment:
                raise ChunkError(f"偏移 {position} 处的 \\end{{{environment}}} 与未闭合的 \\begin{{{opened}}} 配对不上")
            environments.append(_Environment(name=environment, body_start=body_start, body_end=position))
            position = after
        elif name in HEADING_LEVELS:
            heading, position = _read_heading(text, name, position, after_name, len(stack))
            headings.append(heading)
        elif name in APPENDIX_COMMANDS:
            appendix_marks.append((position, AppendixSource.COMMAND))
            position = after_name
        else:
            position = after_name
    if stack:
        raise ChunkError(f"到文件尾仍未闭合的环境：{'、'.join(name for name, _ in stack)}")
    return _Scan(
        headings=tuple(headings),
        environments=tuple(environments),
        appendix_marks=tuple(sorted(appendix_marks)),
        paragraph_starts=tuple(match.end() for match in masking.BLANK_LINE_RE.finditer(text)),
    )


def _read_heading(text: str, level: str, start: int, after_name: int, depth: int) -> tuple[_Heading, int]:
    """读一处标题命令的星号、可选参数与必选参数，返回（标题记录、参数之后的位置）。

    必选参数整体跳过，其中的 math 与花括号不参与后续扫描；命令后没有必选参数组时参数记空串。
    """
    cursor = after_name
    if text[cursor : cursor + 1] == "*":
        cursor += 1
    cursor = masking.skip_optional_arguments(text, cursor)
    if text[cursor : cursor + 1] != "{":
        return _Heading(start=start, level=level, argument="", depth=depth), cursor
    end = masking.match_group(text, cursor)
    return _Heading(start=start, level=level, argument=text[cursor + 1 : end - 1], depth=depth), end


class _Depth:
    """按一组环境体区间查任意偏移的深度：区间端点排序后取前缀和，查询用二分。

    环境体是 `\\begin` 之后到配对 `\\end` 的反斜杠偏移，两个命令自身因此处于外层深度——
    `\\begin{appendices}` 在有效深度 0 处可被识别，靠的就是这个口径。
    """

    def __init__(self, environments: Iterable[_Environment], transparent: frozenset[str] = frozenset()) -> None:
        points: list[tuple[int, int]] = []
        for environment in environments:
            if environment.name in transparent:
                continue
            points.append((environment.body_start, 1))
            points.append((environment.body_end, -1))
        points.sort()
        self._offsets = [offset for offset, _ in points]
        self._depths: list[int] = []
        total = 0
        for _, delta in points:
            total += delta
            self._depths.append(total)

    def at(self, offset: int) -> int:
        index = bisect_right(self._offsets, offset)
        return self._depths[index - 1] if index else 0


# ------------------------------------------------------------------ 定级与分块


class _ChunkRun:
    """一次分块的全部状态：扫描结果、定级结论与两级深度。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.scan = _scan(text)
        self.level = _preferred_level(self.scan.headings)
        self.transparent = _transparent_environments(self.scan, self.level)
        self.depth = _Depth(self.scan.environments, self.transparent)

    def run(self) -> ChunkingOutcome:
        """定级、划区、聚合与 stray 合并，产出 chunk 序列。"""
        appendix_start, appendix_source = self._appendix_mark()
        if self.level is None:
            # 无标题退化路径整篇是单一 body 区，appendix 标记不划区；来路记 absent，
            # 免得 manifest 报了来路却没有 part 为 appendix 的 chunk。
            appendix_start, appendix_source = None, AppendixSource.ABSENT
        chunks: list[Chunk] = []
        for part, start, end in self._regions(appendix_start):
            for unit in self._region_units(part, start, end):
                chunks.extend(self._chunk(piece, part) for piece in self._expand(unit, self.level))
        return ChunkingOutcome(
            chunks=tuple(_merge_small(self.text, chunks)),
            heading_level=self.level,
            transparent_environments=tuple(sorted(self.transparent)),
            appendix_source=appendix_source,
        )

    # -------------------------------------------------------------- 区与单元

    def _appendix_mark(self) -> tuple[int | None, AppendixSource]:
        """取首个位于有效深度 0 的 appendix 标记，返回（起始偏移、来路）；没有则（None、absent）。"""
        for offset, source in self.scan.appendix_marks:
            if self.depth.at(offset) == 0:
                return offset, source
        return None, AppendixSource.ABSENT

    def _level_cuts(self) -> list[int]:
        """首选层级的标题切点：该层级标题命令自身的反斜杠偏移，取有效深度 0 处的。"""
        return [
            heading.start
            for heading in self.scan.headings
            if heading.level == self.level and self.depth.at(heading.start) == 0
        ]

    def _regions(self, appendix_start: int | None) -> list[tuple[Part, int, int]]:
        """划出 front / body / appendix 三个区，区之间不聚合；空区不产 chunk。

        无标题退化路径（全文一个标题命令都没有）整篇视为单一 body 区。存在标题命令、而有效
        深度 0 处一个首选层级标题都没有，是兜底硬判据，抛 `ChunkError`。
        """
        length = len(self.text)
        if self.level is None:
            return [(Part.BODY, 0, length)] if length else []
        cuts = self._level_cuts()
        if not cuts:
            raise ChunkError(
                f"全文有 {self.level} 标题，但有效深度 0 处一个也没有，"
                f"最外层未判透明的包裹环境是 {self._outermost_wrapper()}"
            )
        appendix = length if appendix_start is None else appendix_start
        body_start = min(cuts[0], appendix)
        bounds = [(Part.FRONT, 0, body_start), (Part.BODY, body_start, appendix), (Part.APPENDIX, appendix, length)]
        return _merge_blank_regions(self.text, [(part, start, end) for part, start, end in bounds if start < end])

    def _outermost_wrapper(self) -> str:
        """兜底硬判据的说明用：包住首个首选层级标题、起点最靠前的环境名。"""
        first = next(heading.start for heading in self.scan.headings if heading.level == self.level)
        wrappers = [
            environment
            for environment in self.scan.environments
            if environment.body_start <= first < environment.body_end
        ]
        return min(wrappers, key=lambda environment: environment.body_start).name if wrappers else "（无）"

    def _region_units(self, part: Part, start: int, end: int) -> list[tuple[int, int]]:
        """区内的单元序列：首选层级切点划出的节；无标题退化路径按段落单元划分。"""
        if self.level is None:
            return _merge_blank(self.text, _split_at((start, end), self._paragraph_cuts(start, end)))
        return _split_at((start, end), [cut for cut in self._level_cuts() if start < cut < end])

    def _paragraph_cuts(self, start: int, end: int) -> list[int]:
        """区间内有效深度 0 处的段落切点：段落单元首行行首。"""
        return [offset for offset in self.scan.paragraph_starts if start < offset < end and self.depth.at(offset) == 0]

    def _expand(self, unit: tuple[int, int], level: str | None) -> list[tuple[int, int]]:
        """超过 `SPLIT_ABOVE` 的单元沿层级序列向深层下分，退完标题层级仍超才用段落切点。

        段落单元仍超过时独占一个 chunk，不切开：不透明环境体内没有切点，超长的定理证明或列表
        整体成 chunk 是接受的终态，驱动器把它记进 manifest 的 `warnings`。
        """
        start, end = unit
        if estimate_tokens(self.text[start:end]) <= SPLIT_ABOVE:
            return [unit]
        deeper_levels = HEADING_LEVELS[HEADING_LEVELS.index(level) + 1 :] if level is not None else ()
        for deeper in deeper_levels:
            cuts = [
                heading.start
                for heading in self.scan.headings
                if heading.level == deeper and start < heading.start < end and self.depth.at(heading.start) == 0
            ]
            if cuts:
                return [piece for sub in _split_at(unit, cuts) for piece in self._expand(sub, deeper)]
        cuts = self._paragraph_cuts(start, end)
        return _merge_blank(self.text, _split_at(unit, cuts)) if cuts else [unit]

    def _chunk(self, unit: tuple[int, int], part: Part) -> Chunk:
        """把一个单元组装成一个 chunk；合并发生时由 `_join` 把两个 chunk 接起来。"""
        start, end = unit
        return Chunk(
            start=start,
            end=end,
            part=part,
            headings=tuple(
                Heading(level=heading.level, argument=heading.argument)
                for heading in self.scan.headings
                if start <= heading.start < end and self.depth.at(heading.start) == 0
            ),
            internal_cuts=(start,),
        )


def _preferred_level(headings: Sequence[_Heading]) -> str | None:
    """定级两步：先取深度 0 处出现过的最浅层级，取不到时取全文出现过的最浅层级。

    第一步的深度把所有环境都计入（透明集尚未求出），取自扫描时的环境栈，多数论文在这一步
    定级。全文一个标题命令都没有时返回 None，走无标题退化路径。
    """
    if not headings:
        return None
    top = [heading for heading in headings if heading.depth == 0]
    return min((heading.level for heading in top or headings), key=HEADING_LEVELS.index)


def _transparent_environments(scan: _Scan, level: str | None) -> frozenset[str]:
    """体内出现首选层级标题命令的环境名：它只做包裹，不构成语义单元，不计入深度。

    按文本判定，嵌套不影响：某个实例的体内有该层级标题，这个环境名就整体透明。
    """
    if level is None:
        return frozenset()
    starts = [heading.start for heading in scan.headings if heading.level == level]
    return frozenset(
        environment.name
        for environment in scan.environments
        if bisect_left(starts, environment.body_start) < bisect_left(starts, environment.body_end)
    )


def _split_at(unit: tuple[int, int], cuts: Sequence[int]) -> list[tuple[int, int]]:
    """按切点把一个区间切成相邻子区间，切点之前的内容归前一个。"""
    edges = [unit[0], *cuts, unit[1]]
    return [(edges[index], edges[index + 1]) for index in range(len(edges) - 1)]


def _merge_blank(text: str, units: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """把内容全是空白的单元并入相邻单元：连续空行之间的空段不该独占 chunk。"""
    merged: list[tuple[int, int]] = []
    for unit in units:
        if merged and not text[unit[0] : unit[1]].strip():
            merged[-1] = (merged[-1][0], unit[1])
        else:
            merged.append(unit)
    if len(merged) > 1 and not text[merged[0][0] : merged[0][1]].strip():
        merged[1] = (merged[0][0], merged[1][1])
        del merged[0]
    return merged


def _merge_blank_regions(text: str, bounds: Sequence[tuple[Part, int, int]]) -> list[tuple[Part, int, int]]:
    """内容全是空白的区并入下一个区（没有下一个则并入前一个），区界仍是强制切点。

    空白区独占一个 chunk 会得到零段落 chunk，出口判据据此判失败，而这样的输入结构完全合法
    （文件以空行开头、首个首选层级标题在其后）。并入之后区界只少了一个没有内容的边。
    """
    merged: list[tuple[Part, int, int]] = []
    pending: int | None = None
    for part, start, end in bounds:
        if pending is not None:
            start, pending = pending, None
        if text[start:end].strip():
            merged.append((part, start, end))
        else:
            pending = start
    if pending is None:
        return merged
    if merged:
        part, start, _ = merged[-1]
        merged[-1] = (part, start, bounds[-1][2])
        return merged
    return [(bounds[0][0], pending, bounds[-1][2])]


def _merge_small(text: str, chunks: Sequence[Chunk]) -> list[Chunk]:
    """不足 `MERGE_BELOW` 的 chunk 与相邻 chunk 合并：先正序吸收后一个，再倒序并回前一个。

    正序一遍收拾区首的碎片（`\\appendix` 这类区界标记行自成一个单元，它后面才是第一节），
    倒序一遍收拾区尾的碎片（致谢、结论），倒序遍历使连续碎片级联合并。跨区不合并：混合
    chunk 会让 `part` 字段失去含义，代价是每篇的 front 区与很小的 appendix 区各留一个不足
    `MERGE_BELOW` 的 chunk。
    """
    merged: list[Chunk] = []
    for chunk in chunks:
        if merged and _tokens(text, merged[-1]) < MERGE_BELOW and _joinable(text, merged[-1], chunk):
            merged[-1] = _join(merged[-1], chunk)
        else:
            merged.append(chunk)
    index = len(merged) - 1
    while index >= 1:
        if _tokens(text, merged[index]) < MERGE_BELOW and _joinable(text, merged[index - 1], merged[index]):
            merged[index - 1] = _join(merged[index - 1], merged[index])
            del merged[index]
        index -= 1
    return merged


def _tokens(text: str, chunk: Chunk) -> int:
    """一个 chunk 的 token 估算值。"""
    return estimate_tokens(text[chunk.start : chunk.end])


def _joinable(text: str, first: Chunk, second: Chunk) -> bool:
    """两个相邻 chunk 能否合并：同一个区，且合并后不超过 `SPLIT_ABOVE`。"""
    return first.part is second.part and estimate_tokens(text[first.start : second.end]) <= SPLIT_ABOVE


def _join(first: Chunk, second: Chunk) -> Chunk:
    """把相邻两个 chunk 接成一个，两侧的标题与单元起点按序拼接。"""
    return Chunk(
        start=first.start,
        end=second.end,
        part=first.part,
        headings=first.headings + second.headings,
        internal_cuts=first.internal_cuts + second.internal_cuts,
    )
