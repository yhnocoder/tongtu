r"""LaTeX 掩码的词法状态机：mask 把不该翻译的部分换成 placeholder，unmask 原样换回。

本模块是核心文本层，只用标准库，不引第三方依赖，也不做文件读写（环境分类表的内容由调用
方读进来，本模块只给出它的默认路径与解析函数）。阶段驱动器在 `tongtu/stages/mask.py`。

掩码文本的形态是 placeholder 与待译文本相间的单一字符序列，两种 placeholder 进入它的方式
不同，这个区别是往返恒等的基础：

- `⟦BLK-n⟧` 是等位替换，精确顶替被摘除的字符区间，不增删其他字符；
- `⟦CAP-n⟧` 是插入行，在 block token 之后插入「换行 + `⟦CAP-n⟧ ` + 单行文本 + 换行」这一
  段完整字符，unmask 删除的正是这一段。

成块对象六类：preamble（文件头到注释外首次出现的 `\begin{document}`，含）、分类为掩码的
environment、注释（整行注释的连续行合并成一块，行尾注释单独成块）、display math（`\[…\]`
与 `$$…$$`）、正文里的元信息命令（`\title` 等，命令起至必选参数组闭合）、postamble（流内
遇到的 `\end{document}` 起至文件尾）。inline math（`$…$` 与 `\(…\)`）是句子成分，留在掩码
文本里。

环境分类两遍式：第一遍词法扫描枚举全部 `\begin{X}` 环境名并收集 `\newtheorem` /
`\newenvironment` 声明，名字全部分类完毕后第二遍执行掩码。分类按四级下沉，前一级给出结论
即停：文档自带声明 → 分类表 → agent 判断（推迟实现）→ 保守默认整块掩码。星号变体在声明与
分类表里都查不到时按去掉星号的名字再查一次。`document` 是结构标记，不参与分类。

词法遍历中的结构错误（配对不上的环境、不平衡的花括号、找不到配对定界符的 math）一律抛
`MaskError`，不做保守回退：论文编译通过却解析不动，是词法状态机的缺陷，要在第一次 LLM
调用之前暴露。
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: placeholder 的哨兵字符。源码本身含它们即哨兵冲突，由调用方转 mask_failed。
SENTINEL_OPEN = "⟦"
SENTINEL_CLOSE = "⟧"

#: 两类 placeholder 的 id 前缀。
BLOCK_ID_PREFIX = "BLK"
CAPTION_ID_PREFIX = "CAP"

#: 掩码文本里的 placeholder 形状，unmask 据此定位与校验。
TOKEN_RE = re.compile(rf"{SENTINEL_OPEN}({BLOCK_ID_PREFIX}|{CAPTION_ID_PREFIX})-([0-9]+){SENTINEL_CLOSE}")

#: 环境分类表的路径。这份数据文件在包目录内（不像 fonts/ 与 skill/ 那样在仓库根经
#: force-include 打包），仓库布局与 wheel 布局下位置相同，故按包相对路径定位。
ENVIRONMENTS_TABLE_PATH = Path(__file__).resolve().parent / "data" / "environments.json"

#: 结构标记，不参与环境分类，也不成块。
DOCUMENT_ENVIRONMENT = "document"

#: 前导区里抽 abstract 槽位的环境名（个别文档类要求摘要写在 `\begin{document}` 之前）。
ABSTRACT_ENVIRONMENT = "abstract"

#: 正文里成块的元信息命令。revtex 与部分会议模板把标题、作者写在 `\begin{document}` 之后，
#: 留在掩码文本里就会被翻译，违反「标题保留英文原题」，作者与机构名同理。
METADATA_COMMANDS: frozenset[str] = frozenset({"title", "author", "date", "affiliation", "email"})

#: 抽 caption 槽位的命令名。`\captionof` 的第一个必选参数是类型，文本在第二个。
CAPTION_COMMAND = "caption"
CAPTION_OF_COMMAND = "captionof"

#: 环境声明命令 → 它给出的 decided_by 取值。
DECLARATION_COMMANDS: dict[str, str] = {
    "newtheorem": "newtheorem",
    "newenvironment": "newenvironment",
    "renewenvironment": "newenvironment",
}

#: 环境名的字符集：字母、数字与 `@`，可带尾随星号。
ENVIRONMENT_NAME_RE = re.compile(r"[A-Za-z0-9@]+\*?")

#: 收集 block 内 `\label` 参数的匹配式（参数不含嵌套花括号）。
LABEL_RE = re.compile(r"\\label\s*\{([^{}]*)\}")

#: 顶层扫描里需要停下判断的字符：控制序列、注释与 math 定界符。其余字符一律原样留在掩码
#: 文本里，用 `search` 一次跳过，不逐字符步进。
TOP_LEVEL_SPECIAL_RE = re.compile(r"[\\%$]")

#: 环境体内扫描需要停下判断的字符：环境体内不做 math 处理，只管控制序列与注释。
BODY_SPECIAL_RE = re.compile(r"[\\%]")

#: 段落切分（abstract 的多段规范化用）：空行分段。
BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")

#: 连续空白折叠为单个空格。
WHITESPACE_RUN_RE = re.compile(r"\s+")

#: 规范化单行文本时剥除的注释：未转义的 `%` 起至行尾（含行尾换行）。
COMMENT_TAIL_RE = re.compile(r"(?<!\\)%[^\n]*\n?")

#: 多段 abstract 规范化后各段之间的连接串。
PARAGRAPH_JOINER = " \\par "

#: 自检报告首处差异时两侧上下文摘录的字符数。
DIFFERENCE_CONTEXT_CHARS = 60


class MaskError(Exception):
    """词法遍历、往返自检或 unmask 完整性检查失败。"""


class EnvironmentClass(StrEnum):
    """环境的两种分类结论：留在掩码文本里，或整块掩掉。"""

    TEXT = "text"
    NON_TRANSLATABLE = "non_translatable"


class BlockCategory(StrEnum):
    """block 的类别。

    前八个来自环境分类表，`UNKNOWN` 由保守默认产生，末四个由结构性成块产生（display math
    记 `MATH`）。两个消费方：survey view 按它决定 backfill 还是保持 placeholder，figures 按
    它取图 block；`CODE` 的环境同时以 verbatim 语义扫描，体内的 `%` 与 `\\begin` 不解析。
    """

    MATH = "math"
    TABLE = "table"
    FIGURE = "figure"
    TIKZ = "tikz"
    CODE = "code"
    ALGORITHM = "algorithm"
    BIBLIOGRAPHY = "bibliography"
    BOX = "box"
    UNKNOWN = "unknown"
    PREAMBLE = "preamble"
    POSTAMBLE = "postamble"
    COMMENT = "comment"
    METADATA = "metadata"


class DecidedBy(StrEnum):
    """环境分类结论的来源，四级下沉各占一个取值（agent 判断推迟实现，暂无取值）。"""

    NEWTHEOREM = "newtheorem"
    NEWENVIRONMENT = "newenvironment"
    TABLE = "table"
    DEFAULT = "default"


class CaptionKind(StrEnum):
    """caption 槽位的两种来源：前导区的 abstract 环境体，与 block 内的 caption 命令。"""

    CAPTION = "caption"
    ABSTRACT = "abstract"


@dataclass(frozen=True)
class TableEntry:
    """环境分类表的一条：class 与 category（class 为 text 时 category 为 None）。"""

    classification: EnvironmentClass
    category: BlockCategory | None


@dataclass
class EnvironmentDecision:
    """一个环境名的分类结论与两个计数。

    `occurrences` 是第一遍词法扫描枚举到的 `\\begin{X}` 次数，`blocks` 是第二遍实际成块的
    次数。嵌在已掩 block 内部的环境 `blocks` 为 0——将来接 agent 分类时只需对成块数大于 0
    的未知环境提问。
    """

    classification: EnvironmentClass
    category: BlockCategory | None
    decided_by: DecidedBy
    occurrences: int = 0
    blocks: int = 0


@dataclass(frozen=True)
class Block:
    """一个被摘出去的 block。

    `tex` 是**带 CAP 槽位的形式**：block 内的 caption 必选参数已换成 `⟦CAP-n⟧`，原始文本可
    由槽位处代入 caption 原文重建。`start` / `end` 是在原文解码后字符序列中的偏移，`line`
    是起始行号（1 起，排查用）。
    """

    id: str
    category: BlockCategory
    environment: str
    decided_by: DecidedBy | None
    labels: tuple[str, ...]
    tex: str
    start: int
    end: int
    line: int


@dataclass(frozen=True)
class Caption:
    """一个 caption 槽位。

    `tex` 是原始文本（block 的 `tex` 里该处是 `⟦CAP-n⟧`），`masked_text` 是掩码文本里的单行
    形态，unmask 拿它作回填判定的比较基准：流中取出的文本与它相同即视为未翻译。
    """

    id: str
    block_id: str
    kind: CaptionKind
    tex: str
    masked_text: str


@dataclass(frozen=True)
class MaskOutcome:
    """一次掩码的全部产出：掩码文本、两类记录与环境分类结论一览。"""

    masked: str
    blocks: tuple[Block, ...]
    captions: tuple[Caption, ...]
    environments: dict[str, EnvironmentDecision]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class UnmaskOutcome:
    """一次 unmask 的产出。

    `fallbacks` 是流中找不到 token、按原文回填的 caption id；`translated` 是流中文本与
    `masked_text` 不同、按流中文本回填的 caption id 与该文本（compile 阶段的 caption 译文
    由此取得）。
    """

    text: str
    fallbacks: tuple[str, ...]
    translated: dict[str, str]


def block_token(block_id: str) -> str:
    """block id 拼成掩码文本里的 token。"""
    return f"{SENTINEL_OPEN}{block_id}{SENTINEL_CLOSE}"


def parse_environment_table(content: str) -> dict[str, TableEntry]:
    """解析环境分类表的内容：环境名 → class 与 category（仅 non_translatable 需要）。

    表的形状是「环境名 → 对象」的单层映射。取值不在词表里、non_translatable 缺 category、
    text 多给 category 都抛 `MaskError`——这份表是仓库内的数据文件，写错要立刻暴露。
    """
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as error:
        raise MaskError(f"环境分类表不是合法 JSON：{error}") from error
    if not isinstance(raw, dict):
        raise MaskError("环境分类表的顶层要是「环境名 → 对象」的映射")
    table: dict[str, TableEntry] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise MaskError(f"环境分类表的 {name} 条目要是对象")
        try:
            classification = EnvironmentClass(entry.get("class"))
        except ValueError as error:
            raise MaskError(f"环境分类表的 {name} 条目 class 取值不在词表里：{entry.get('class')!r}") from error
        category_value = entry.get("category")
        if classification is EnvironmentClass.NON_TRANSLATABLE:
            try:
                category = BlockCategory(category_value)
            except ValueError as error:
                raise MaskError(f"环境分类表的 {name} 条目 category 取值不在词表里：{category_value!r}") from error
        else:
            if category_value is not None:
                raise MaskError(f"环境分类表的 {name} 条目是 text，不该带 category")
            category = None
        table[name] = TableEntry(classification=classification, category=category)
    return table


def mask_document(text: str, table: Mapping[str, TableEntry]) -> MaskOutcome:
    """对解码后的原文执行两遍式掩码，返回掩码文本与两类记录。

    第一遍枚举环境名并收集声明，第二遍按分类结论执行掩码。哨兵冲突、结构错误一律抛
    `MaskError`。往返自检不在这里做，由驱动器调 `verify_roundtrip`。
    """
    _check_sentinels(text)
    occurrences, declared = _enumerate_environments(text, table)
    environments = {name: _decide(name, declared, table, count) for name, count in sorted(occurrences.items())}
    return _MaskRun(text, environments, declared, table).run()


def unmask(masked: str, blocks: Sequence[Block], captions: Sequence[Caption]) -> UnmaskOutcome:
    """把掩码文本换回完整 TeX：删除 CAP 插入的整行、把 BLK token 换回 block 内容。

    语义在 mask 阶段定死，本阶段的自检与 compile 阶段的 backfill 共用这一份实现：

    1. 逐 CAP 槽位在流中找「前导换行 + 含该 token 的行 + 行尾换行」，删除这一段并取出行内
       文本（token 后去掉一个空格，token 前的空白容忍）；
    2. 取出的文本与掩码时写出的单行形态相同 → 回填原始文本，不同 → 视为已翻译、回填该文本，
       流中找不到该 token 的行 → 回填原始文本并记回退；
    3. `⟦BLK-n⟧` 换回填好的 block 内容；
    4. 完整性检查，任一不满足即抛 `MaskError`：输出无残留哨兵字符；每个 block 的 token 恰好
       使用一次；每个 CAP token 在流中至多出现一次；流中出现记录里没有的 token。
    """
    filled, fallbacks, translated, stream = _restore_captions(masked, captions)
    text = _restore_blocks(stream, blocks, filled)
    residual = [ch for ch in (SENTINEL_OPEN, SENTINEL_CLOSE) if ch in text]
    if residual:
        raise MaskError(f"unmask 的输出里仍有哨兵字符 {'、'.join(residual)}，掩码文本里有残缺的 placeholder")
    return UnmaskOutcome(text=text, fallbacks=tuple(fallbacks), translated=translated)


def verify_roundtrip(source: str, outcome: MaskOutcome) -> None:
    """往返自检：对未翻译的掩码文本跑 unmask，与原文逐字符比对，不等即抛 `MaskError`。"""
    restored = unmask(outcome.masked, outcome.blocks, outcome.captions).text
    if restored == source:
        return
    raise MaskError(_describe_difference(source, restored))


# ------------------------------------------------------------------ 第一遍：枚举与声明


def _enumerate_environments(text: str, table: Mapping[str, TableEntry]) -> tuple[dict[str, int], dict[str, str]]:
    r"""第一遍词法扫描：枚举全部 `\begin{X}` 环境名，并收集环境声明命令给出的名字。

    注释里与 category 为 code 的环境体内的 `\begin` 不计入；`document` 不计入（结构标记）。
    code 的判定只查分类表：声明出来的环境一律是 text，未知环境走保守默认，两者都不是 code。
    """
    occurrences: dict[str, int] = {}
    declared: dict[str, str] = {}
    position = 0
    length = len(text)
    while position < length:
        match = TOP_LEVEL_SPECIAL_RE.search(text, position)
        if match is None:
            break
        position = match.start()
        if text[position] == "%":
            position = _skip_comment(text, position)
            continue
        if text[position] == "$":
            position += 1
            continue
        name, after_name = _read_control_sequence(text, position)
        if name == "verb":
            position = _skip_verb(text, after_name)
            continue
        if name == "begin":
            environment, after = _read_environment_name(text, after_name)
            if environment is None:
                position = after_name
                continue
            if environment != DOCUMENT_ENVIRONMENT:
                occurrences[environment] = occurrences.get(environment, 0) + 1
            entry = _table_lookup(table, environment)
            if entry is not None and entry.category is BlockCategory.CODE:
                position = _skip_code_environment(text, after, environment)
            else:
                position = after
            continue
        if name in DECLARATION_COMMANDS:
            declared_name, after = _read_declaration_name(text, after_name)
            if declared_name is not None:
                declared.setdefault(declared_name, DECLARATION_COMMANDS[name])
            position = after
            continue
        position = after_name
    return occurrences, declared


def _decide(
    name: str, declared: Mapping[str, str], table: Mapping[str, TableEntry], occurrences: int
) -> EnvironmentDecision:
    """按四级下沉给一个环境名定分类：文档自带声明 → 分类表 → （agent 判断推迟）→ 保守默认。

    星号变体在声明与分类表里都查不到时按去掉星号的名字再查一次（`figure*` 继承 `figure`）。
    """
    for candidate in _lookup_names(name):
        source = declared.get(candidate)
        if source is not None:
            return EnvironmentDecision(
                classification=EnvironmentClass.TEXT,
                category=None,
                decided_by=DecidedBy(source),
                occurrences=occurrences,
            )
        entry = table.get(candidate)
        if entry is not None:
            return EnvironmentDecision(
                classification=entry.classification,
                category=entry.category,
                decided_by=DecidedBy.TABLE,
                occurrences=occurrences,
            )
    return EnvironmentDecision(
        classification=EnvironmentClass.NON_TRANSLATABLE,
        category=BlockCategory.UNKNOWN,
        decided_by=DecidedBy.DEFAULT,
        occurrences=occurrences,
    )


def _lookup_names(name: str) -> Iterator[str]:
    """一个环境名的查表顺序：先原样，星号变体再补一次去掉星号的名字。"""
    yield name
    if name.endswith("*"):
        yield name[:-1]


def _table_lookup(table: Mapping[str, TableEntry], name: str) -> TableEntry | None:
    """按查表顺序在分类表里找一个环境名，找不到返回 None。"""
    for candidate in _lookup_names(name):
        entry = table.get(candidate)
        if entry is not None:
            return entry
    return None


# ------------------------------------------------------------------ 第二遍：掩码


@dataclass
class _CaptionSlot:
    """环境体扫描顺带记下的 caption 槽位：必选参数内容的起止偏移与它的种类。"""

    start: int
    end: int
    kind: CaptionKind


class _MaskRun:
    """第二遍词法扫描：按分类结论摘出 block、抽出 caption 槽位、拼出掩码文本。

    输出按「上一个 block 结束处到本 block 起始处的原文 + block token + 本 block 的 caption
    插入行」逐段拼接，block 之间的字符原样保留，等位替换与插入行两条规则由此兑现。
    """

    def __init__(
        self,
        text: str,
        environments: dict[str, EnvironmentDecision],
        declared: Mapping[str, str],
        table: Mapping[str, TableEntry],
    ) -> None:
        self.text = text
        self.length = len(text)
        self.environments = environments
        self.declared = declared
        self.table = table
        self.blocks: list[Block] = []
        self.captions: list[Caption] = []
        self.warnings: list[str] = []
        self.output: list[str] = []
        self.cursor = 0
        self.line_starts = _line_starts(text)

    def run(self) -> MaskOutcome:
        position = self._mask_preamble()
        self._mask_body(position)
        self.output.append(self.text[self.cursor :])
        return MaskOutcome(
            masked="".join(self.output),
            blocks=tuple(self.blocks),
            captions=tuple(self.captions),
            environments=self.environments,
            warnings=tuple(self.warnings),
        )

    # -------------------------------------------------------------- preamble

    def _mask_preamble(self) -> int:
        r"""文件头到注释外首次出现的 `\begin{document}`（含）整体成 BLK-0，返回它的结束位置。

        前导区里的 `\begin{abstract}…\end{abstract}` 抽 abstract 槽位（个别文档类要求摘要
        写在 `\begin{document}` 之前）；`\title` 不抽槽位，标题保留英文原题。注释外找不到
        `\begin{document}` 即结构错误。
        """
        end = self._find_begin_document()
        if end is None:
            raise MaskError("注释外找不到 \\begin{document}，判定不出前导区的范围")
        slots = self._preamble_abstract_slots(end)
        self._emit_block(0, end, BlockCategory.PREAMBLE, environment="", decided_by=None, slots=slots)
        return end

    def _find_begin_document(self) -> int | None:
        r"""在注释外找首次出现的 `\begin{document}`，返回它的结束位置；找不到返回 None。"""
        position = 0
        while position < self.length:
            match = TOP_LEVEL_SPECIAL_RE.search(self.text, position)
            if match is None:
                return None
            position = match.start()
            if self.text[position] == "%":
                position = _skip_comment(self.text, position)
                continue
            if self.text[position] == "$":
                position += 1
                continue
            name, after_name = _read_control_sequence(self.text, position)
            if name == "verb":
                position = _skip_verb(self.text, after_name)
                continue
            if name == "begin":
                environment, after = _read_environment_name(self.text, after_name)
                if environment == DOCUMENT_ENVIRONMENT:
                    return after
                position = after_name if environment is None else after
                continue
            position = after_name
        return None

    def _preamble_abstract_slots(self, preamble_end: int) -> list[_CaptionSlot]:
        r"""在前导区里找注释外的 `\begin{abstract}`，环境体抽成一个 abstract 槽位。

        找不到返回空清单；找到多个只取第一个（一份文档只有一个摘要）。
        """
        position = 0
        while position < preamble_end:
            match = TOP_LEVEL_SPECIAL_RE.search(self.text, position)
            if match is None or match.start() >= preamble_end:
                return []
            position = match.start()
            if self.text[position] == "%":
                position = _skip_comment(self.text, position)
                continue
            if self.text[position] == "$":
                position += 1
                continue
            name, after_name = _read_control_sequence(self.text, position)
            if name == "verb":
                position = _skip_verb(self.text, after_name)
                continue
            if name != "begin":
                position = after_name
                continue
            environment, after = _read_environment_name(self.text, after_name)
            if environment is None:
                position = after_name
                continue
            if environment != ABSTRACT_ENVIRONMENT:
                position = after
                continue
            body_end, _ = self._scan_environment_body(after, environment, category=None, collect_captions=False)
            close_start = self._environment_close_start(body_end, environment)
            return [_CaptionSlot(start=after, end=close_start, kind=CaptionKind.ABSTRACT)]
        return []

    def _environment_close_start(self, environment_end: int, name: str) -> int:
        r"""由环境的结束位置回推它的 `\end{name}` 起始位置，即环境体的结束位置。"""
        close = self.text.rfind("\\end", 0, environment_end)
        if close < 0:
            raise MaskError(f"环境 {name} 的 \\end 定位失败")
        return close

    # -------------------------------------------------------------- 正文

    def _mask_body(self, position: int) -> None:
        r"""从前导区之后扫到文件尾：注释、math、环境、元信息命令与 postamble 逐个成块。

        text 环境的 `\begin` / `\end` 与体内内容留在掩码文本里，体内继续扫描，嵌套的
        non-translatable 环境照常成块。
        """
        seen_end_document = False
        while position < self.length:
            match = TOP_LEVEL_SPECIAL_RE.search(self.text, position)
            if match is None:
                break
            position = match.start()
            character = self.text[position]
            if character == "%":
                position = self._mask_comment(position)
                continue
            if character == "$":
                position = self._mask_dollar(position)
                continue
            name, after_name = _read_control_sequence(self.text, position)
            if name == "verb":
                position = _skip_verb(self.text, after_name)
                continue
            if name == "[":
                position = self._mask_display_math(position, after_name, "\\]")
                continue
            if name == "(":
                position = _skip_to_delimiter(self.text, after_name, "\\)", "\\(")
                continue
            if name == "begin":
                position = self._mask_environment(position, after_name)
                continue
            if name == "end":
                environment, after = _read_environment_name(self.text, after_name)
                if environment == DOCUMENT_ENVIRONMENT:
                    self._emit_block(
                        position, self.length, BlockCategory.POSTAMBLE, environment="", decided_by=None, slots=[]
                    )
                    seen_end_document = True
                    break
                position = after_name if environment is None else after
                continue
            if name in METADATA_COMMANDS:
                position = self._mask_metadata(position, after_name)
                continue
            position = after_name
        if not seen_end_document:
            self.warnings.append("扫描中没有在流内遇到 \\end{document}，不设 postamble block")

    def _mask_comment(self, position: int) -> int:
        """注释成块：整行注释的连续行合并为一块，行尾注释单独成块。

        整行注释指该行注释外的部分为空或纯空白，块起自首行行首、含行间换行、不含末行换行，
        token 因此独占一行；行尾注释的块从 `%` 起至行尾，不含换行。
        """
        line_start = self.text.rfind("\n", 0, position) + 1
        line_end = _line_end(self.text, position)
        if line_start >= self.cursor and not self.text[line_start:position].strip():
            end = line_end
            next_start = line_end + 1
            while next_start <= self.length:
                following_end = _line_end(self.text, next_start)
                if not self.text[next_start:following_end].lstrip(" \t").startswith("%"):
                    break
                end = following_end
                next_start = following_end + 1
            self._emit_block(line_start, end, BlockCategory.COMMENT, environment="", decided_by=None, slots=[])
            return end
        self._emit_block(position, line_end, BlockCategory.COMMENT, environment="", decided_by=None, slots=[])
        return line_end

    def _mask_dollar(self, position: int) -> int:
        r"""未转义的 `$`：下一字符也是 `$` 则按 display math 收块，否则是 inline math，跳过。

        inline math 是句子成分，留在掩码文本里；其间不做注释与环境处理，只找配对的未转义
        `$`。找不到配对定界符是结构错误。
        """
        if self.text[position + 1 : position + 2] == "$":
            end = _find_display_dollar_close(self.text, position + 2)
            self._emit_block(position, end, BlockCategory.MATH, environment="", decided_by=None, slots=[])
            return end
        return _find_inline_dollar_close(self.text, position + 1)

    def _mask_display_math(self, start: int, body_start: int, closing: str) -> int:
        r"""`\[ … \]` 整段成块，category 记 math。"""
        end = _skip_to_delimiter(self.text, body_start, closing, "\\[")
        self._emit_block(start, end, BlockCategory.MATH, environment="", decided_by=None, slots=[])
        return end

    def _mask_environment(self, start: int, after_name: int) -> int:
        r"""`\begin{X}`：分类为掩码的环境整段成块，text 环境留在掩码文本里、体内继续扫描。"""
        environment, after = _read_environment_name(self.text, after_name)
        if environment is None or environment == DOCUMENT_ENVIRONMENT:
            return after_name if environment is None else after
        decision = self._decision_for(environment)
        if decision.classification is EnvironmentClass.TEXT:
            return after
        end, slots = self._scan_environment_body(
            after, environment, decision.category, collect_captions=decision.category is not BlockCategory.CODE
        )
        self._emit_block(
            start,
            end,
            decision.category or BlockCategory.UNKNOWN,
            environment=environment,
            decided_by=decision.decided_by,
            slots=slots,
        )
        decision.blocks += 1
        return end

    def _mask_metadata(self, start: int, after_name: int) -> int:
        """正文里的元信息命令：命令起至必选参数组闭合（含前面的可选参数）整体成块。

        命令后没有必选参数组时不成块（没有可译文本），扫描从命令名之后继续。
        """
        position = _skip_optional_arguments(self.text, after_name)
        if position >= self.length or self.text[position] != "{":
            return after_name
        end = _match_group(self.text, position)
        self._emit_block(start, end, BlockCategory.METADATA, environment="", decided_by=None, slots=[])
        return end

    # -------------------------------------------------------------- 环境体扫描

    def _scan_environment_body(
        self, body_start: int, name: str, category: BlockCategory | None, *, collect_captions: bool
    ) -> tuple[int, list[_CaptionSlot]]:
        r"""扫到配对的 `\end{name}`，返回（环境的结束位置、体内的 caption 槽位）。

        同名嵌套计数；体内 category 为 code 的子环境直接跳到它的 `\end`；体内注释跳过（注释
        里的 `\end` 不算配对，注释里的 caption 也不抽）。category 为 code 的环境体按 verbatim
        语义处理：体内的 `%` 与 `\begin` 不解析，直接找字面的 `\end{name}`。到文件尾仍未配对
        是结构错误。
        """
        if category is BlockCategory.CODE:
            return _skip_code_environment(self.text, body_start, name), []
        slots: list[_CaptionSlot] = []
        depth = 1
        position = body_start
        while position < self.length:
            match = BODY_SPECIAL_RE.search(self.text, position)
            if match is None:
                break
            position = match.start()
            if self.text[position] == "%":
                position = _skip_comment(self.text, position)
                continue
            command, after_name = _read_control_sequence(self.text, position)
            if command == "verb":
                position = _skip_verb(self.text, after_name)
                continue
            if command == "begin":
                environment, after = _read_environment_name(self.text, after_name)
                if environment is None:
                    position = after_name
                    continue
                if environment == name:
                    depth += 1
                    position = after
                    continue
                nested = self._decision_for(environment)
                position = (
                    _skip_code_environment(self.text, after, environment)
                    if nested.category is BlockCategory.CODE
                    else after
                )
                continue
            if command == "end":
                environment, after = _read_environment_name(self.text, after_name)
                if environment is None:
                    position = after_name
                    continue
                if environment == name:
                    depth -= 1
                    if depth == 0:
                        return after, slots
                position = after
                continue
            if collect_captions and command in (CAPTION_COMMAND, CAPTION_OF_COMMAND):
                slot, position = _read_caption_slot(self.text, command, after_name)
                if slot is not None:
                    slots.append(slot)
                continue
            position = after_name
        raise MaskError(f"环境 {name}（起于第 {self._line_of(body_start)} 行）到文件尾仍未找到配对的 \\end")

    def _decision_for(self, name: str) -> EnvironmentDecision:
        """取一个环境名的分类结论；第一遍没枚举到的（例如只出现在 inline math 里）就地补上。"""
        decision = self.environments.get(name)
        if decision is None:
            decision = _decide(name, self.declared, self.table, occurrences=0)
            self.environments[name] = decision
        return decision

    # -------------------------------------------------------------- 落块与落槽位

    def _emit_block(
        self,
        start: int,
        end: int,
        category: BlockCategory,
        *,
        environment: str,
        decided_by: DecidedBy | None,
        slots: list[_CaptionSlot],
    ) -> None:
        """记一个 block：抽出它的 caption 槽位，写出 token 与 caption 插入行。"""
        block_id = f"{BLOCK_ID_PREFIX}-{len(self.blocks)}"
        captions = [self._emit_caption(block_id, slot) for slot in slots]
        raw = self.text[start:end]
        self.blocks.append(
            Block(
                id=block_id,
                category=category,
                environment=environment,
                decided_by=decided_by,
                labels=tuple(LABEL_RE.findall(raw)),
                tex=_apply_slots(self.text, start, end, slots, [caption.id for caption in captions]),
                start=start,
                end=end,
                line=self._line_of(start),
            )
        )
        self.output.append(self.text[self.cursor : start])
        self.output.append(block_token(block_id))
        for caption in captions:
            self.output.append(f"\n{block_token(caption.id)} {caption.masked_text}\n")
        self.cursor = end

    def _emit_caption(self, block_id: str, slot: _CaptionSlot) -> Caption:
        """记一个 caption 槽位：原始文本与它在掩码文本里的单行形态。"""
        raw = self.text[slot.start : slot.end]
        caption = Caption(
            id=f"{CAPTION_ID_PREFIX}-{len(self.captions)}",
            block_id=block_id,
            kind=slot.kind,
            tex=raw,
            masked_text=_single_line_text(raw),
        )
        self.captions.append(caption)
        return caption

    def _line_of(self, position: int) -> int:
        """字符偏移对应的行号（1 起）。"""
        return bisect_right(self.line_starts, position)


# ------------------------------------------------------------------ 词法原语


def _read_control_sequence(text: str, position: int) -> tuple[str, int]:
    r"""读位置 position 处的控制序列（该处是反斜杠），返回（名字、结束位置）。

    反斜杠之后是字母时名字取最长字母串（`\begin` → `begin`），否则名字是紧随的单个字符
    （`\%` → `%`，`\[` → `[`，`\\` → `\`）。反斜杠位于文件尾时名字为空串。
    """
    start = position + 1
    if start >= len(text):
        return "", start
    if text[start].isascii() and text[start].isalpha():
        end = start
        while end < len(text) and text[end].isascii() and text[end].isalpha():
            end += 1
        return text[start:end], end
    return text[start], start + 1


def _read_environment_name(text: str, position: int) -> tuple[str | None, int]:
    r"""读 `\begin` / `\end` 之后的 `{环境名}`，返回（环境名、`}` 之后的位置）。

    环境名字符集为字母、数字与 `@`，可带尾随星号。花括号里不是这个形状（例如宏定义里的
    `\begin{#1}`）时返回 (None, position)，调用方按「不是环境」继续扫描。
    """
    cursor = _skip_argument_space(text, position)
    if cursor >= len(text) or text[cursor] != "{":
        return None, position
    match = ENVIRONMENT_NAME_RE.match(text, cursor + 1)
    if match is None or match.end() >= len(text) or text[match.end()] != "}":
        return None, position
    return match.group(0), match.end() + 1


def _read_declaration_name(text: str, position: int) -> tuple[str | None, int]:
    r"""读环境声明命令的第一个必选参数（被声明的环境名），返回（名字、参数之后的位置）。

    `\newtheorem*` / `\newenvironment*` 的星号先跳过。参数不是合法环境名形状时返回
    (None, position)。
    """
    cursor = _skip_argument_space(text, position)
    if cursor < len(text) and text[cursor] == "*":
        cursor = _skip_argument_space(text, cursor + 1)
    if cursor >= len(text) or text[cursor] != "{":
        return None, position
    match = ENVIRONMENT_NAME_RE.match(text, cursor + 1)
    if match is None or match.end() >= len(text) or text[match.end()] != "}":
        return None, position
    return match.group(0), match.end() + 1


def _read_caption_slot(text: str, command: str, position: int) -> tuple[_CaptionSlot | None, int]:
    r"""读 caption 命令的必选参数，返回（槽位、命令之后的位置）。

    `\caption` 与 `\caption*` 的可选参数 `[短标题]` 不抽；`\captionof{类型}` 的第一个必选
    参数是类型，文本在第二个。没有必选参数组时返回 (None, …)，该处不成槽位。
    """
    cursor = position
    if cursor < len(text) and text[cursor] == "*":
        cursor += 1
    if command == CAPTION_OF_COMMAND:
        cursor = _skip_argument_space(text, cursor)
        if cursor >= len(text) or text[cursor] != "{":
            return None, cursor
        cursor = _match_group(text, cursor)
    cursor = _skip_optional_arguments(text, cursor)
    if cursor >= len(text) or text[cursor] != "{":
        return None, cursor
    end = _match_group(text, cursor)
    return _CaptionSlot(start=cursor + 1, end=end - 1, kind=CaptionKind.CAPTION), end


def _skip_comment(text: str, position: int) -> int:
    """从 `%` 跳到行尾，返回换行符的位置（没有换行符则是文件尾）。"""
    return _line_end(text, position)


def _skip_verb(text: str, position: int) -> int:
    r"""跳过 `\verb` 的定界符与体内内容，返回结束定界符之后的位置。

    `\verb*` 的星号先跳过；定界符是紧随其后的单个字符，体内内容不解析，只为定位结束定界符。
    定界符不在同一行内闭合时停在行尾。
    """
    cursor = position
    if cursor < len(text) and text[cursor] == "*":
        cursor += 1
    if cursor >= len(text):
        return cursor
    delimiter = text[cursor]
    line_end = _line_end(text, cursor)
    close = text.find(delimiter, cursor + 1, line_end)
    return line_end if close < 0 else close + 1


def _skip_code_environment(text: str, body_start: int, name: str) -> int:
    r"""按 verbatim 语义跳过 category 为 code 的环境体：直接找字面的 `\end{name}`。"""
    marker = f"\\end{{{name}}}"
    close = text.find(marker, body_start)
    if close < 0:
        raise MaskError(f"环境 {name} 到文件尾仍未找到配对的 {marker}")
    return close + len(marker)


def _skip_argument_space(text: str, position: int) -> int:
    """跳过命令与它的参数之间的空白：空格与制表符，至多跨一个换行。"""
    cursor = position
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    if cursor < len(text) and text[cursor] == "\n":
        cursor += 1
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1
    return cursor


def _skip_optional_arguments(text: str, position: int) -> int:
    """跳过命令的可选参数（可以有多组 `[…]`），返回它们之后的位置。"""
    cursor = position
    while True:
        cursor = _skip_argument_space(text, cursor)
        if cursor >= len(text) or text[cursor] != "[":
            return cursor
        cursor = _match_bracket(text, cursor)


def _match_group(text: str, position: int) -> int:
    """从 `{` 起匹配到配对的 `}`，返回它之后的位置。

    转义的花括号（`\\{`）不计入配对，注释里的花括号跳过。到文件尾仍未配对即不平衡，抛
    `MaskError`。
    """
    return _match_delimited(text, position, "{", "}")


def _match_bracket(text: str, position: int) -> int:
    """从 `[` 起匹配到配对的 `]`，返回它之后的位置。"""
    return _match_delimited(text, position, "[", "]")


def _match_delimited(text: str, position: int, opening: str, closing: str) -> int:
    """匹配一组配对的定界符，返回结束定界符之后的位置；配对不上抛 `MaskError`。"""
    depth = 0
    cursor = position
    length = len(text)
    while cursor < length:
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "%":
            cursor = _skip_comment(text, cursor)
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    raise MaskError(f"从偏移 {position} 起的 {opening}…{closing} 到文件尾仍未配对，花括号不平衡")


def _skip_to_delimiter(text: str, position: int, closing: str, opening: str) -> int:
    r"""跳到 `\]` 或 `\)` 这类反斜杠定界符之后，返回它之后的位置。

    逐个控制序列步进，`\\` 因此不会被误读成定界符的反斜杠。找不到即结构错误。
    """
    cursor = position
    length = len(text)
    while cursor < length:
        index = text.find("\\", cursor)
        if index < 0:
            break
        if text.startswith(closing, index):
            return index + len(closing)
        cursor = index + 2
    raise MaskError(f"从偏移 {position} 起的 {opening} 到文件尾仍未找到配对的 {closing}")


def _find_inline_dollar_close(text: str, position: int) -> int:
    r"""找 inline math 的配对未转义 `$`，返回它之后的位置；找不到即结构错误。"""
    cursor = position
    length = len(text)
    while cursor < length:
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "$":
            return cursor + 1
        cursor += 1
    raise MaskError(f"从偏移 {position - 1} 起的 inline math 到文件尾仍未找到配对的 $")


def _find_display_dollar_close(text: str, position: int) -> int:
    r"""找 display math `$$ … $$` 的结束定界符，返回它之后的位置；找不到即结构错误。"""
    cursor = position
    length = len(text)
    while cursor < length:
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "$" and text[cursor + 1 : cursor + 2] == "$":
            return cursor + 2
        cursor += 1
    raise MaskError(f"从偏移 {position - 2} 起的 display math 到文件尾仍未找到配对的 $$")


def _line_end(text: str, position: int) -> int:
    """本行换行符的位置；没有换行符则是文件尾。"""
    index = text.find("\n", position)
    return len(text) if index < 0 else index


def _line_starts(text: str) -> list[int]:
    """各行起始偏移的清单，供行号查询。"""
    starts = [0]
    index = text.find("\n")
    while index >= 0:
        starts.append(index + 1)
        index = text.find("\n", index + 1)
    return starts


# ------------------------------------------------------------------ 文本变换


def _check_sentinels(text: str) -> None:
    """源码本身含哨兵字符即哨兵冲突：placeholder 无从与原文区分。"""
    present = [character for character in (SENTINEL_OPEN, SENTINEL_CLOSE) if character in text]
    if present:
        raise MaskError(f"原文里出现了 placeholder 的哨兵字符 {'、'.join(present)}，与掩码文本的形态冲突")


def _apply_slots(text: str, start: int, end: int, slots: Sequence[_CaptionSlot], caption_ids: Sequence[str]) -> str:
    """取 block 的原文并把各 caption 槽位换成它的 token，得到带槽位形式的 tex。"""
    parts: list[str] = []
    cursor = start
    for slot, caption_id in zip(slots, caption_ids, strict=True):
        parts.append(text[cursor : slot.start])
        parts.append(block_token(caption_id))
        cursor = slot.end
    parts.append(text[cursor:end])
    return "".join(parts)


def _single_line_text(raw: str) -> str:
    """把 caption 或 abstract 的原始文本规范成掩码文本里的单行形态。

    剥除注释、连续空白折叠为单个空格、去除首尾空白；多段（按空行切段）逐段同样规范化后以
    ` \\par ` 连接。
    """
    paragraphs = [_normalize_whitespace(part) for part in BLANK_LINE_RE.split(raw)]
    return PARAGRAPH_JOINER.join(part for part in paragraphs if part)


def _normalize_whitespace(raw: str) -> str:
    """剥除注释、连续空白折叠为单个空格、去除首尾空白。"""
    return WHITESPACE_RUN_RE.sub(" ", COMMENT_TAIL_RE.sub("", raw)).strip()


def _describe_difference(source: str, restored: str) -> str:
    """往返自检不等时的说明：首处差异的字符偏移与两侧上下文摘录。"""
    limit = min(len(source), len(restored))
    position = limit
    for index in range(limit):
        if source[index] != restored[index]:
            position = index
            break
    start = max(0, position - DIFFERENCE_CONTEXT_CHARS)
    stop = position + DIFFERENCE_CONTEXT_CHARS
    return (
        f"往返自检不恒等：首处差异在字符偏移 {position}（原文 {len(source)} 字符，"
        f"还原 {len(restored)} 字符）；原文 {source[start:stop]!r}；还原 {restored[start:stop]!r}"
    )


# ------------------------------------------------------------------ unmask


def _restore_captions(
    masked: str, captions: Sequence[Caption]
) -> tuple[dict[str, str], list[str], dict[str, str], str]:
    """逐 CAP 槽位删除它的插入行并定出回填文本，返回（回填表、回退清单、译文表、剩余的流）。"""
    filled: dict[str, str] = {}
    fallbacks: list[str] = []
    translated: dict[str, str] = {}
    stream = masked
    for caption in captions:
        token = block_token(caption.id)
        occurrences = stream.count(token)
        if occurrences > 1:
            raise MaskError(f"{token} 在流中出现 {occurrences} 次，每个 caption token 至多出现一次")
        if occurrences == 0:
            filled[caption.id] = caption.tex
            fallbacks.append(caption.id)
            continue
        index = stream.index(token)
        previous_newline = stream.rfind("\n", 0, index)
        following_newline = stream.find("\n", index)
        line_start = 0 if previous_newline < 0 else previous_newline + 1
        line_stop = len(stream) if following_newline < 0 else following_newline
        segment_start = 0 if previous_newline < 0 else previous_newline
        segment_stop = len(stream) if following_newline < 0 else following_newline + 1
        line = stream[line_start:line_stop]
        text = line[line.index(token) + len(token) :]
        if text.startswith(" "):
            text = text[1:]
        if text == caption.masked_text:
            filled[caption.id] = caption.tex
        else:
            filled[caption.id] = text
            translated[caption.id] = text
        stream = stream[:segment_start] + stream[segment_stop:]
    return filled, fallbacks, translated, stream


def _restore_blocks(stream: str, blocks: Sequence[Block], filled: Mapping[str, str]) -> str:
    """把流里的 BLK token 换回填好 caption 的 block 内容，并校验 token 的使用次数。"""
    block_by_id = {block.id: block for block in blocks}
    used: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        token_id = f"{match.group(1)}-{match.group(2)}"
        block = block_by_id.get(token_id)
        if block is None:
            raise MaskError(f"流中的 {match.group(0)} 在 blocks 记录里不存在")
        used[token_id] = used.get(token_id, 0) + 1
        return _fill_slots(block.tex, filled)

    text = TOKEN_RE.sub(replace, stream)
    for block in blocks:
        count = used.get(block.id, 0)
        if count != 1:
            raise MaskError(f"{block_token(block.id)} 在流中使用了 {count} 次，每个 block 的 token 要恰好使用一次")
    return text


def _fill_slots(tex: str, filled: Mapping[str, str]) -> str:
    """把 block 的带槽位 tex 里的 CAP token 换成回填文本。"""

    def replace(match: re.Match[str]) -> str:
        token_id = f"{match.group(1)}-{match.group(2)}"
        text = filled.get(token_id)
        if text is None:
            raise MaskError(f"block 内的 {match.group(0)} 在 captions 记录里不存在")
        return text

    return TOKEN_RE.sub(replace, tex)
