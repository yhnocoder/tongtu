"""chunk 阶段：章节树优先分块（架构 §3 chunk 行与「chunk 的粒度哲学」、决策 12）。

输入是 mask 阶段产出的**掩码流**文本（`build/masked.tex`），输出是块清单结构
`ChunkPlan`。本模块是**纯文本变换**：不做任何 IO、不认识工作目录、不提供 CLI——
落盘（`build/chunks/<id>.tex` + `manifest.json`）由 chunk 阶段驱动器负责，
`ChunkPlan.chunk_files()` 与 `ChunkPlan.to_manifest()` 给出与之兼容的结构。

分块规则
--------

1. **章节树优先**。`\\section`（更准确地说：全文出现的**最浅**标题层级，book 类即
   `\\chapter`）是首选翻译单元。相邻小节按文档顺序**聚合**到软目标 token 数；
   超大节在其**子标题**（`\\subsection` → `\\subsubsection` → …）边界下分，仍超限
   才退到段落边界，直到不超过硬上限。
2. **绝不切入环境或段落内部**。掩码流里重环境已是 `⟦BLK-n⟧` 占位符，但散文环境
   （itemize、定理环境等）原样留着且内部可能含空行——分段器带环境深度计数，
   深度 > 0 的空行不分段，因此块边界永远落在环境之外。单个段落即使超过硬上限也
   独占一块而不被切开（回退粒度由 validate 的段落一一对应保证，见架构决策 12）。
3. **首块与附录**。`\\section` 之前的正文（前导区块 `⟦BLK-0⟧`、标题与摘要的
   `⟦CAP-n⟧` 行）自然成首块，且**不与正文章节聚合**（结构上自成一体）。
   `\\appendix` / `\\appendices` / `\\begin{appendices}` 之后的块标 `is_appendix`，
   且不与正文块聚合。
4. **夹缝与结尾小块**。聚合本身按文档顺序贪心到软目标；收尾再做一次**尾块回并**：
   token 数低于 `tail_min`（默认软目标的 1/4）的块，若与前一块同属正文/同属附录、
   前一块非首块、且合并后不超过硬上限，则并入前一块。
5. **glue 段落**（只含 `\\appendix`、`\\clearpage` 等结构性命令的段落）跟随**后一个**
   单元，使 `\\appendix` 与附录首节同块。

token 计数
----------

`estimate_tokens()` 是**零依赖近似估算**，不是任何具体分词器的真实计数，只用于
分块决策（软目标 / 硬上限），不进缓存 key、不做预算承诺。估算规则：

* `⟦BLK-n⟧` / `⟦CAP-n⟧` 占位符：每个 3 token；
* 控制序列（`\\alpha`、`\\{` 等）：每个 2 token；
* CJK 字符（中文注入变体与中文 fixture 会出现）：每字 1 token；
* 其余按空白分词，每词 `ceil(len/4)` token（下限 1）。

软目标 4000 / 硬上限 8000（掩码后散文 token 计）是架构附录 B 开放问题 1 的**起步
值**，待 fixture 校准；两者都是参数，调用方可覆盖。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

#: 软目标 token 数：小节聚合到此为止（开放问题 1 的起步值）。
SOFT_TARGET_TOKENS = 4000

#: 硬上限 token 数：超大节下分至不超过此值（单段超限除外）。
HARD_LIMIT_TOKENS = 8000

#: token 估算规则的版本号——规则一改，分块结果就变，写进 manifest 备查。
ESTIMATOR_VERSION = "approx-v1"

#: 块文件后缀（`build/chunks/c000.tex`）。
CHUNK_SUFFIX = ".tex"

_CHARS_PER_TOKEN = 4
_PLACEHOLDER_TOKENS = 3
_CONTROL_TOKENS = 2

#: 掩码占位符（mask 阶段的 `⟦BLK-n⟧` / `⟦CAP-n⟧`）。
PLACEHOLDER_RE = re.compile(r"⟦(?:BLK|CAP)-\d+⟧")

_CONTROL_RE = re.compile(r"\\(?:[A-Za-z@]+\*?|[^A-Za-z\s])")
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff65]")

#: 标题命令 → 层级（数字越小越浅）。`\paragraph` 在论文里常当伪标题用，
#: 作为下分的最后一级结构边界收进来。
HEADING_LEVELS: dict[str, int] = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}

_HEADING_RE = re.compile(
    r"\\(" + "|".join(sorted(HEADING_LEVELS, key=len, reverse=True)) + r")(\*?)(?![A-Za-z])"
)

#: 环境名：字母/`@` 起头，可含数字（`algorithm2e`），可带星号（`figure*`）。
_ENV_NAME = r"[A-Za-z@][A-Za-z@0-9]*\*?"

_BEGIN_RE = re.compile(r"\\begin\s*\{(" + _ENV_NAME + r")\}")
_END_RE = re.compile(r"\\end\s*\{(" + _ENV_NAME + r")\}")

#: 结构性环境：不算「环境深度」（其内部的空行照常分段、标题照常识别）。
TRANSPARENT_ENVS = frozenset({"document", "appendix", "appendices", "subappendices"})

#: 标志「此后是附录」的环境。
APPENDIX_ENVS = frozenset({"appendix", "appendices", "subappendices"})

#: 标志「此后是附录」的宏：`\appendix`（LaTeX 内核）与 `\appendices`（IEEEtran）。
_APPENDIX_RE = re.compile(r"\\appendi(?:x|ces)(?![A-Za-z])")

#: 空行分隔符（段落边界的候选位置）。容忍 CRLF 与行尾空白。
_BLANK_RE = re.compile(r"\n[ \t\r]*(?:\n[ \t\r]*)+")

_GLUE_COMMANDS = (
    "appendices",
    "appendix",
    "cleardoublepage",
    "clearpage",
    "newpage",
    "pagebreak",
    "bigskip",
    "medskip",
    "smallskip",
    "noindent",
    "par",
)
_GLUE_ENVS = "|".join(sorted(APPENDIX_ENVS, key=len, reverse=True))
_GLUE_RE = re.compile(
    r"(?:\s|\\(?:"
    + "|".join(_GLUE_COMMANDS)
    + r")(?![A-Za-z])|\\(?:begin|end)\s*\{(?:"
    + _GLUE_ENVS
    + r")\})*"
)

_HEADING_ARG_GAP = re.compile(r"[ \t]*\n?[ \t]*")


class ChunkError(ValueError):
    """分块参数非法。"""


# --------------------------------------------------------------------------- #
# token 估算
# --------------------------------------------------------------------------- #


def estimate_tokens(text: str) -> int:
    """掩码后散文的 token **近似估算**（见模块 docstring 的规则表）。

    刻意零依赖、无模型：分块只需要「大致多大」，估偏一两成不改变分块质量，
    而引入分词器依赖会违反零第三方依赖的约定（架构 §13）。
    """
    total = 0
    rest, n = PLACEHOLDER_RE.subn(" ", text)
    total += n * _PLACEHOLDER_TOKENS
    rest, n = _CONTROL_RE.subn(" ", rest)
    total += n * _CONTROL_TOKENS
    rest, n = _CJK_RE.subn(" ", rest)
    total += n
    for word in rest.split():
        total += max(1, math.ceil(len(word) / _CHARS_PER_TOKEN))
    return total


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Heading:
    """掩码流中的一个标题命令。"""

    command: str
    """标题命令名（不含反斜杠与星号），如 `section`。"""

    level: int
    """层级，见 `HEADING_LEVELS`。"""

    title: str
    """标题文本（空白已折叠；其中的 LaTeX 命令原样保留）。"""

    numbered: bool
    """是否带编号（`\\section*` 为 False）。"""

    path: tuple[str, ...]
    """累积编号路径，如 `("3", "3.2")`；附录为 `("A", "A.1")`；
    不编号的标题按同级出现序记作 `("*1",)`。"""

    titles: tuple[str, ...]
    """与 `path` 等长的标题文本路径。"""

    is_appendix: bool
    offset: int
    """在掩码流中的字符偏移。"""

    para_index: int
    """所在段落序号。"""

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "level": self.level,
            "title": self.title,
            "numbered": self.numbered,
            "path": list(self.path),
            "para": self.para_index,
        }


@dataclass(frozen=True)
class Paragraph:
    """掩码流中的一个段落（分块的原子单位）。

    段落 = 深度 0 的空行之间的一段文本；散文环境（itemize / 定理等）内部的空行
    不分段，因此一个「段落」可能整体包住一个环境。
    """

    index: int
    text: str
    """段落正文（首尾空白已去除）。"""

    start: int
    end: int
    """`text` 在掩码流中的起止偏移（`source[start:end] == text`）。"""

    tokens: int
    section_path: tuple[str, ...]
    section_titles: tuple[str, ...]
    is_appendix: bool
    is_glue: bool
    """是否只含结构性命令（`\\appendix`、`\\clearpage` 等）而无正文。"""

    heading: Heading | None
    """本段是否以标题命令起头（前面只允许空白与 glue 命令）。"""


@dataclass(frozen=True)
class Chunk:
    """一个翻译块 = 一段**完整的段落序列**。"""

    id: str
    """块 id，`c000` 风格（与 chunks.json 契约一致）。"""

    index: int
    section_path: tuple[str, ...]
    """所属章节路径 = 块内各段章节路径的最长公共前缀（glue 段不参与）。"""

    section_titles: tuple[str, ...]
    headings: tuple[Heading, ...]
    """块内起头的全部标题。"""

    para_start: int
    """起始段索引（含）。"""

    para_end: int
    """结束段索引（**不含**）。"""

    paragraph_count: int
    tokens: int
    is_appendix: bool
    is_front_matter: bool
    """是否为 `\\section` 之前的首块（前导区 + 标题 + 摘要）。"""

    part: int
    part_count: int
    """超大节下分时的分片序号 / 分片总数；未下分时均为 1。"""

    span: tuple[int, int]
    """块在掩码流中的字符区间。相邻块首尾相接、覆盖全文，故拼接可还原原文。"""

    text: str
    """`source[span[0]:span[1]]`，含块间的空行——拼接恒等于掩码流。"""

    prev_tail_para: int | None
    """前一块**末段**的段索引（供 translate 组装邻域原文上下文）。"""

    next_head_para: int | None
    """后一块**首段**的段索引。"""

    @property
    def body(self) -> str:
        """去掉首尾空白的块正文（写 `build/chunks/<id>.tex` 用）。"""
        return self.text.strip()

    @property
    def file(self) -> str:
        return f"{self.id}{CHUNK_SUFFIX}"

    def to_dict(self) -> dict:
        """manifest 条目（JSON 可序列化；字段名与 chunks.json 契约对齐）。"""
        return {
            "id": self.id,
            "index": self.index,
            "file": self.file,
            "section_path": list(self.section_path),
            "section_titles": list(self.section_titles),
            "headings": [h.to_dict() for h in self.headings],
            "para_start": self.para_start,
            "para_end": self.para_end,
            "paragraph_count": self.paragraph_count,
            "tokens": self.tokens,
            "is_appendix": self.is_appendix,
            "is_front_matter": self.is_front_matter,
            "part": self.part,
            "part_count": self.part_count,
            "span": list(self.span),
            "prev_tail_para": self.prev_tail_para,
            "next_head_para": self.next_head_para,
        }


@dataclass(frozen=True)
class ChunkPlan:
    """分块结果：块清单 + 段落表 + 生效参数。"""

    source: str
    paragraphs: tuple[Paragraph, ...]
    chunks: tuple[Chunk, ...]
    soft_target: int
    hard_limit: int
    tail_min: int

    def __len__(self) -> int:
        return len(self.chunks)

    def __iter__(self):
        return iter(self.chunks)

    def __getitem__(self, item) -> Chunk:
        return self.chunks[item]

    def reassemble(self) -> str:
        """按序拼接所有块——恒等于输入的掩码流（无丢段、无重复）。"""
        return "".join(chunk.text for chunk in self.chunks)

    def paragraphs_of(self, chunk: Chunk) -> tuple[Paragraph, ...]:
        return self.paragraphs[chunk.para_start : chunk.para_end]

    def chunk_files(self) -> dict[str, str]:
        """`{文件名: 块正文}`——驱动器照此写 `build/chunks/`。"""
        return {chunk.file: chunk.body + "\n" for chunk in self.chunks}

    def to_manifest(self) -> dict:
        """块清单 manifest（JSON 可序列化）。"""
        return {
            "estimator": ESTIMATOR_VERSION,
            "soft_target_tokens": self.soft_target,
            "hard_limit_tokens": self.hard_limit,
            "tail_min_tokens": self.tail_min,
            "paragraph_count": len(self.paragraphs),
            "chunk_count": len(self.chunks),
            "tokens": sum(chunk.tokens for chunk in self.chunks),
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }


# --------------------------------------------------------------------------- #
# 词法扫描：段落边界 + 标题树
# --------------------------------------------------------------------------- #


@dataclass
class _RawHeading:
    command: str
    level: int
    title: str
    numbered: bool
    offset: int
    end: int
    is_appendix: bool
    parent: int | None
    counter: int


def _find_closing(text: str, i: int, opener: str, closer: str) -> int | None:
    """`text[i] == opener`，返回配对的 closer 下标；跳过转义字符。"""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _parse_heading(text: str, match: re.Match[str]) -> tuple[str, str, bool, int] | None:
    """解析标题命令的参数，返回 `(命令名, 标题文本, 是否编号, 结束偏移)`。

    支持可选参数 `\\section[short]{Title}` 与标题内的嵌套花括号；找不到必选参数
    （如宏定义里裸出现的 `\\section`）返回 None，调用方跳过。
    """
    command, star = match.group(1), match.group(2) == "*"
    i = _HEADING_ARG_GAP.match(text, match.end()).end()
    if i < len(text) and text[i] == "[":
        close = _find_closing(text, i, "[", "]")
        if close is None:
            return None
        i = _HEADING_ARG_GAP.match(text, close + 1).end()
    if i >= len(text) or text[i] != "{":
        return None
    close = _find_closing(text, i, "{", "}")
    if close is None:
        return None
    title = re.sub(r"\s+", " ", text[i + 1 : close]).strip()
    return command, title, not star, close + 1


def _letter(n: int) -> str:
    """1 → A，2 → B，…，27 → AA（附录编号）。"""
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _scan(text: str) -> tuple[list[tuple[int, int]], list[_RawHeading], int | None]:
    """一遍扫描：深度 0 的空行分隔符、深度 0 的标题（含编号所需的树关系）、附录起点。"""
    events: list[tuple[int, int, str, object]] = []
    for m in _BEGIN_RE.finditer(text):
        events.append((m.start(), 0, "begin", m.group(1)))
    for m in _END_RE.finditer(text):
        events.append((m.start(), 0, "end", m.group(1)))
    for m in _HEADING_RE.finditer(text):
        events.append((m.start(), 1, "heading", m))
    for m in _APPENDIX_RE.finditer(text):
        events.append((m.start(), 1, "appendix", None))
    for m in _BLANK_RE.finditer(text):
        events.append((m.start(), 2, "blank", m))
    events.sort(key=lambda e: (e[0], e[1]))

    separators: list[tuple[int, int]] = []
    headings: list[_RawHeading] = []
    stack: list[int] = []  # _RawHeading 下标构成的祖先链
    counters: dict[int, int] = {}
    star_counters: dict[int, int] = {}
    depth = 0
    is_appendix = False
    appendix_offset: int | None = None
    consumed = 0  # 已被标题参数吃掉的偏移（避免标题文本里的 begin/end 干扰）

    for pos, _, kind, payload in events:
        if pos < consumed:
            continue
        if kind == "begin":
            name = payload
            if name in APPENDIX_ENVS and not is_appendix and depth == 0:
                is_appendix = True
                appendix_offset = pos
                counters.clear()
                star_counters.clear()
            if name not in TRANSPARENT_ENVS:
                depth += 1
        elif kind == "end":
            if payload not in TRANSPARENT_ENVS:
                depth = max(0, depth - 1)
        elif kind == "blank":
            if depth == 0:
                separators.append((payload.start(), payload.end()))
        elif kind == "appendix":
            if depth == 0 and not is_appendix:
                is_appendix = True
                appendix_offset = pos
                counters.clear()
                star_counters.clear()
        elif kind == "heading":
            if depth != 0:
                continue
            parsed = _parse_heading(text, payload)
            if parsed is None:
                continue
            command, title, numbered, end = parsed
            level = HEADING_LEVELS[command]
            while stack and headings[stack[-1]].level >= level:
                stack.pop()
            for table in (counters, star_counters):
                for lv in [lv for lv in table if lv > level]:
                    del table[lv]
            if numbered:
                counters[level] = counters.get(level, 0) + 1
                counter = counters[level]
            else:
                star_counters[level] = star_counters.get(level, 0) + 1
                counter = star_counters[level]
            headings.append(
                _RawHeading(
                    command=command,
                    level=level,
                    title=title,
                    numbered=numbered,
                    offset=pos,
                    end=end,
                    is_appendix=is_appendix,
                    parent=stack[-1] if stack else None,
                    counter=counter,
                )
            )
            stack.append(len(headings) - 1)
            consumed = end
    return separators, headings, appendix_offset


def _number_headings(
    raw: list[_RawHeading], primary_level: int | None
) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """把扫描结果渲染成累积编号路径。附录的**最浅**层级用字母（A、B、…）。"""
    paths: list[tuple[str, ...]] = []
    titles: list[tuple[str, ...]] = []
    for item in raw:
        if not item.numbered:
            local = f"*{item.counter}"
        elif item.is_appendix and item.level == primary_level:
            local = _letter(item.counter)
        else:
            local = str(item.counter)
        if item.parent is None:
            path, title_path = (local,), (item.title,)
        else:
            parent_path, parent_titles = paths[item.parent], titles[item.parent]
            path = parent_path + (f"{parent_path[-1]}.{local}",)
            title_path = parent_titles + (item.title,)
        paths.append(path)
        titles.append(title_path)
    return list(zip(paths, titles))


# --------------------------------------------------------------------------- #
# 分段
# --------------------------------------------------------------------------- #


def _paragraph_spans(text: str, separators: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """按深度 0 的空行切段，返回各段（去首尾空白后的）起止偏移。"""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for sep_start, sep_end in separators + [(len(text), len(text))]:
        piece = text[cursor:sep_start]
        stripped = piece.strip()
        if stripped:
            start = cursor + (len(piece) - len(piece.lstrip()))
            spans.append((start, start + len(stripped)))
        cursor = sep_end
    return spans


def split_paragraphs(masked: str) -> tuple[Paragraph, ...]:
    """把掩码流切成段落（不分块）。散文环境内部的空行不分段。"""
    return _build_paragraphs(masked)[0]


def _build_paragraphs(text: str) -> tuple[tuple[Paragraph, ...], int | None]:
    separators, raw_headings, appendix_offset = _scan(text)
    spans = _paragraph_spans(text, separators)
    levels = {h.level for h in raw_headings}
    primary_level = min(levels) if levels else None
    numbering = _number_headings(raw_headings, primary_level)

    # 标题 → 所在段落序号
    heading_para: list[int] = []
    para_index = 0
    for item in raw_headings:
        while para_index + 1 < len(spans) and spans[para_index + 1][0] <= item.offset:
            para_index += 1
        heading_para.append(para_index if spans else 0)

    headings = [
        Heading(
            command=item.command,
            level=item.level,
            title=item.title,
            numbered=item.numbered,
            path=numbering[i][0],
            titles=numbering[i][1],
            is_appendix=item.is_appendix,
            offset=item.offset,
            para_index=heading_para[i],
        )
        for i, item in enumerate(raw_headings)
    ]
    by_offset = {h.offset: h for h in headings}

    paragraphs: list[Paragraph] = []
    cursor = 0  # 指向下一个尚未越过的标题
    for index, (start, end) in enumerate(spans):
        while cursor < len(headings) and headings[cursor].offset <= start:
            cursor += 1
        context = headings[cursor - 1] if cursor else None
        body = text[start:end]
        glue_end = start + _GLUE_RE.match(body).end()
        lead = by_offset.get(glue_end)
        if lead is not None and lead.offset < end:
            context = lead
        else:
            lead = None
        paragraphs.append(
            Paragraph(
                index=index,
                text=body,
                start=start,
                end=end,
                tokens=estimate_tokens(body),
                section_path=context.path if context else (),
                section_titles=context.titles if context else (),
                is_appendix=context.is_appendix if context else False,
                is_glue=_GLUE_RE.fullmatch(body) is not None,
                heading=lead,
            )
        )
    _mark_appendix(paragraphs, appendix_offset)
    return tuple(paragraphs), primary_level


def _mark_appendix(paragraphs: list[Paragraph], marker: int | None) -> None:
    """附录起点之后的段落一律标附录（含 `\\appendix` 本身所在的 glue 段）。"""
    if marker is None:
        return
    for i, para in enumerate(paragraphs):
        if para.end > marker and not para.is_appendix:
            paragraphs[i] = replace(para, is_appendix=True)


# --------------------------------------------------------------------------- #
# 组块
# --------------------------------------------------------------------------- #


def _tokens(paragraphs: list[Paragraph]) -> int:
    return sum(p.tokens for p in paragraphs)


def _is_appendix(paragraphs: list[Paragraph]) -> bool:
    for para in paragraphs:
        if not para.is_glue:
            return para.is_appendix
    return paragraphs[0].is_appendix if paragraphs else False


@dataclass
class _Group:
    paragraphs: list[Paragraph]
    unit_ids: list[int]
    closed: bool
    is_front_matter: bool

    @property
    def tokens(self) -> int:
        return _tokens(self.paragraphs)

    @property
    def is_appendix(self) -> bool:
        return _is_appendix(self.paragraphs)


def _units(paragraphs: tuple[Paragraph, ...], primary_level: int | None) -> list[list[Paragraph]]:
    """按最浅标题层级切出翻译单元；结尾的 glue 段落跟随后一个单元。"""
    units: list[list[Paragraph]] = []
    current: list[Paragraph] = []
    for para in paragraphs:
        starts_unit = (
            primary_level is not None
            and para.heading is not None
            and para.heading.level == primary_level
        )
        if starts_unit:
            glue_tail: list[Paragraph] = []
            while current and current[-1].is_glue:
                glue_tail.insert(0, current.pop())
            if current:
                units.append(current)
            current = glue_tail
        current.append(para)
    if current:
        units.append(current)
    return units


def _subunits(
    paragraphs: list[Paragraph], level: int | None
) -> list[tuple[int | None, list[Paragraph]]]:
    """按**更深一级**的标题把单元切成子单元；首个子标题之前的段落自成一段。"""
    child_levels = {
        p.heading.level
        for p in paragraphs[1:]
        if p.heading is not None and (level is None or p.heading.level > level)
    }
    if not child_levels:
        return [(level, list(paragraphs))]
    child_level = min(child_levels)
    groups: list[tuple[int, list[Paragraph]]] = []
    current: list[Paragraph] = []
    for i, para in enumerate(paragraphs):
        starts = i > 0 and para.heading is not None and para.heading.level == child_level
        if starts:
            glue_tail: list[Paragraph] = []
            while current and current[-1].is_glue:
                glue_tail.insert(0, current.pop())
            if current:
                groups.append((child_level, current))
            current = glue_tail
        current.append(para)
    if current:
        groups.append((child_level, current))
    return groups


def _split_unit(
    paragraphs: list[Paragraph], level: int | None, soft: int, hard: int
) -> list[list[Paragraph]]:
    """超大单元下分：先按子标题，仍超限再按段落边界。段落绝不切开。"""
    if _tokens(paragraphs) <= hard:
        return [list(paragraphs)]
    subunits = _subunits(paragraphs, level)
    if len(subunits) <= 1:
        return _split_at_paragraphs(paragraphs, soft)
    parts: list[list[Paragraph]] = []
    current: list[Paragraph] = []
    for child_level, sub in subunits:
        pieces = _split_unit(sub, child_level, soft, hard)
        if len(pieces) > 1:
            if current:
                parts.append(current)
                current = []
            parts.extend(pieces)
            continue
        if current and _tokens(current) + _tokens(sub) <= soft:
            current.extend(sub)
        else:
            if current:
                parts.append(current)
            current = list(sub)
    if current:
        parts.append(current)
    return parts


def _split_at_paragraphs(paragraphs: list[Paragraph], soft: int) -> list[list[Paragraph]]:
    """最后手段：按段落边界贪心填到软目标。单段超限则独占一块（不切开）。"""
    parts: list[list[Paragraph]] = []
    current: list[Paragraph] = []
    for para in paragraphs:
        if current and _tokens(current) + para.tokens > soft:
            parts.append(current)
            current = []
        current.append(para)
    if current:
        parts.append(current)
    return parts


def _common_prefix(paths: list[tuple[str, ...]]) -> int:
    if not paths:
        return 0
    length = min(len(p) for p in paths)
    for i in range(length):
        if any(p[i] != paths[0][i] for p in paths):
            return i
    return length


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def chunk_masked(
    masked: str,
    *,
    soft_target: int = SOFT_TARGET_TOKENS,
    hard_limit: int = HARD_LIMIT_TOKENS,
    tail_min: int | None = None,
) -> ChunkPlan:
    """把掩码流切成翻译块（章节树优先）。

    参数
    ----
    masked:
        mask 阶段产出的掩码流文本。
    soft_target:
        软目标 token 数——相邻小节聚合到此为止。
    hard_limit:
        硬上限 token 数——超大节在子标题 / 段落边界下分至不超过此值；
        单个段落超限时独占一块（段落不可拆）。
    tail_min:
        尾块回并阈值，默认 `soft_target // 4`。

    返回
    ----
    `ChunkPlan`：块清单 + 段落表 + 生效参数；`plan.reassemble()` 恒等于 `masked`。
    """
    if soft_target <= 0:
        raise ChunkError(f"soft_target 需为正数：{soft_target}")
    if hard_limit < soft_target:
        raise ChunkError(f"hard_limit（{hard_limit}）不得小于 soft_target（{soft_target}）")
    threshold = soft_target // 4 if tail_min is None else tail_min
    if threshold < 0:
        raise ChunkError(f"tail_min 不得为负：{tail_min}")

    paragraphs, primary_level = _build_paragraphs(masked)
    if not paragraphs:
        return ChunkPlan(
            source=masked,
            paragraphs=(),
            chunks=(),
            soft_target=soft_target,
            hard_limit=hard_limit,
            tail_min=threshold,
        )

    units = _units(paragraphs, primary_level)
    has_front_matter = bool(units) and units[0][0].heading is None

    groups: list[_Group] = []
    for unit_id, unit in enumerate(units):
        front = has_front_matter and unit_id == 0
        parts = _split_unit(unit, primary_level, soft_target, hard_limit)
        if front or len(parts) > 1:
            for part in parts:
                groups.append(_Group(part, [unit_id], closed=True, is_front_matter=front))
            continue
        (only,) = parts
        last = groups[-1] if groups else None
        if (
            last is not None
            and not last.closed
            and not last.is_front_matter
            and last.is_appendix == _is_appendix(only)
            and last.tokens + _tokens(only) <= soft_target
        ):
            last.paragraphs.extend(only)
            last.unit_ids.append(unit_id)
        else:
            groups.append(_Group(only, [unit_id], closed=False, is_front_matter=False))

    # 尾块 / 夹缝小块回并
    for i in range(len(groups) - 1, 0, -1):
        group, prev = groups[i], groups[i - 1]
        if (
            group.tokens < threshold
            and not group.is_front_matter
            and not prev.is_front_matter
            and group.is_appendix == prev.is_appendix
            and prev.tokens + group.tokens <= hard_limit
        ):
            prev.paragraphs.extend(group.paragraphs)
            prev.unit_ids.extend(group.unit_ids)
            groups.pop(i)

    return ChunkPlan(
        source=masked,
        paragraphs=paragraphs,
        chunks=_build_chunks(masked, paragraphs, groups),
        soft_target=soft_target,
        hard_limit=hard_limit,
        tail_min=threshold,
    )


def _build_chunks(
    source: str, paragraphs: tuple[Paragraph, ...], groups: list[_Group]
) -> tuple[Chunk, ...]:
    """给块编号、算章节路径与邻域，并把源码切成首尾相接的区间（拼接可还原）。"""
    parts_of: dict[int, list[int]] = {}
    for i, group in enumerate(groups):
        parts_of.setdefault(group.unit_ids[0], []).append(i)

    starts = [group.paragraphs[0].start for group in groups]
    bounds = [0] + starts[1:] + [len(source)]

    chunks: list[Chunk] = []
    for i, group in enumerate(groups):
        members = group.paragraphs
        para_start = members[0].index
        para_end = members[-1].index + 1
        body_paths = [p.section_path for p in members if not p.is_glue] or [
            p.section_path for p in members
        ]
        shared = _common_prefix(body_paths)
        anchor = next((p for p in members if not p.is_glue), members[0])
        siblings = parts_of[group.unit_ids[0]]
        headings = tuple(
            h
            for h in (p.heading for p in members)
            if h is not None
        )
        span = (bounds[i], bounds[i + 1])
        chunks.append(
            Chunk(
                id=f"c{i:03d}",
                index=i,
                section_path=anchor.section_path[:shared],
                section_titles=anchor.section_titles[:shared],
                headings=headings,
                para_start=para_start,
                para_end=para_end,
                paragraph_count=len(members),
                tokens=group.tokens,
                is_appendix=group.is_appendix,
                is_front_matter=group.is_front_matter,
                part=siblings.index(i) + 1,
                part_count=len(siblings),
                span=span,
                text=source[span[0] : span[1]],
                prev_tail_para=para_start - 1 if para_start > 0 else None,
                next_head_para=para_end if para_end < len(paragraphs) else None,
            )
        )
    return tuple(chunks)


__all__ = [
    "CHUNK_SUFFIX",
    "ESTIMATOR_VERSION",
    "HARD_LIMIT_TOKENS",
    "HEADING_LEVELS",
    "SOFT_TARGET_TOKENS",
    "Chunk",
    "ChunkError",
    "ChunkPlan",
    "Heading",
    "Paragraph",
    "chunk_masked",
    "estimate_tokens",
    "split_paragraphs",
]
