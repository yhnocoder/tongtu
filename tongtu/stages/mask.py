"""mask 阶段：环境完备枚举 → 分类表三级掩码 → 往返自检（架构 §3.1、决策 10）。

出口判据是机械的：`unmask(mask(x)) == x` 逐字节恒等，且 `blocks.json` 完整。本模块只做
纯文本变换，不碰文件系统、不接 CLI（阶段驱动器另接）。

## 掩码流与无损还原怎么兼得

v2 的掩码是**有损**的：散文里的注释被剥掉、caption 被单行化——恒等自检根本不可能成立。
本实现把两件事拆开，由**同一份 blocks.json** 支撑两个视图：

* **掩码流**（`MaskResult.masked`）是给 LLM 看的*投影*：重环境换成 `⟦BLK-n⟧`、注释换成
  同样的 `⟦BLK-n⟧`（category=`comment`）、caption 被单行化摘成 `⟦CAP-k⟧ …` 行；
* **blocks.json** 保存每个块的**逐字节原文**（`block.tex`）、每个 caption 槽位的原文
  （`caption.text`）以及它在流中的展示文本（`caption.stream_text`）。

于是「掩码流丢掉的东西」全部可从 blocks.json 精确还原。两条规则保证恒等：

1. **注释是块，不是垃圾**。散文里的注释整段（连续的整行注释合并为一块）换成占位符，
   回填即逐字节复原；LLM 也再无机会把 `%` 挪位置而注释掉正文。
2. **caption「未改动 ⇒ 回填原文」**。unmask 比对流中 `⟦CAP-k⟧` 行的文本与
   `caption.stream_text`：相同（或为空）说明没人翻译过它，回填 `caption.text` 原文；
   不同才当译文。默认参数下自检即恒等，不需要给自检开后门。

`⟦CAP-k⟧` 行的插入与删除也必须逐字节可逆。规则：块占位符之后插入 `"\\n" + 各行 +
"\\n"`，unmask 删除时连首尾换行一起删；若原文块的紧邻下一个字符是换行，则把这个换行
**吸收进块的 tex**（块 span 相应 +1），让插入的行尾换行顶替它——掩码流因此不会多出空行，
`\\end{figure}` 在行中间（如行内公式里的矩阵）时也照样恒等。v2 在 caption 行前后各留一个
换行，等于在段落中间插入空行、把一段拆成两段。

## 什么留在流里

散文、行内公式（`$…$`）、`\\cite` 与各种行内命令、散文环境的 `\\begin`/`\\end` 包裹一律留
在掩码流里——它们是翻译的上下文，掩掉反而让模型看不懂句子。行间的 `\\[…\\]` 与 `$$…$$`
同属数学移位而非环境，也留在流里；它们与行内公式是同一词法类别，且 survey 的通读视图本来
就要把数学类块回填回去（架构 §3：记号约定住在行间公式里）。它们的完整性由 validate 的控制
序列 multiset 比对与编译回环兜底（架构 §3.1 第 4 条）。

## 分类从哪来

`\\begin{X}` 的**枚举**不需要任何先验知识（词法扫一遍即可，verbatim 体与注释内的不算）；
**分类**才需要知识，来源按优先级：

1. `\\newtheorem` / `\\newenvironment` 等**声明**（文档自带的知识，最具体，优先级最高）；
2. `tongtu/data/environments.json` **分类表**（可促升的确定性知识）；
3. **关节③**：`arbiter` 回调（M3 接 agent；本期默认 None）；
4. **保守默认**：整块掩码、`category=unknown`——只降覆盖率，绝不损坏。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence

from .. import CONTRACT_VERSION
from ..texlex import (
    BEGIN_RE,
    Lexer,
    TexLexError,
    find_balanced,
    find_bracket_arg,
    find_env_end,
    line_number,
    line_starts,
    skip_comment,
    strip_comments_inline,
)

__all__ = [
    "MaskError",
    "Block",
    "Caption",
    "EnvironmentInfo",
    "EnvQuery",
    "EnvRule",
    "EnvironmentTable",
    "MaskResult",
    "BLOCK_TOKEN",
    "CAPTION_TOKEN",
    "TOKEN_RE",
    "CATEGORIES",
    "SURVEY_RESTORE_CATEGORIES",
    "load_environment_table",
    "enumerate_environments",
    "parse_environment_declarations",
    "classify_environments",
    "mask",
    "roundtrip_check",
    "roundtrip_diff",
]


class MaskError(ValueError):
    """掩码无法进行（分类表损坏等）。源码本身的畸形只记警告并降级，不抛这个。"""


#: 占位符字面（沿用 v2：`⟦`/`⟧` 在 LaTeX 源码里不会自然出现，且 xelatex 下可见）。
BLOCK_TOKEN = "⟦BLK-{}⟧"
CAPTION_TOKEN = "⟦CAP-{}⟧"

TOKEN_RE = re.compile(r"⟦(BLK|CAP)-(\d+)⟧")
BLOCK_TOKEN_RE = re.compile(r"⟦BLK-\d+⟧")
CAPTION_TOKEN_RE = re.compile(r"⟦CAP-\d+⟧")

#: 块分类，与 docs/schemas/blocks.schema.json 的 block.category 枚举一致。
CATEGORIES = (
    "preamble",
    "math",
    "table",
    "figure",
    "algorithm",
    "code",
    "tikz",
    "theorem",
    "comment",
    "other",
    "unknown",
)

#: survey 通读视图里回填原文的块分类（架构 §3：记号约定住在行间公式里）。
SURVEY_RESTORE_CATEGORIES = frozenset({"math"})

#: 声明命令 → 家族。`document` 家族的第二个必选参数是 xparse 的参数规格，要多跳一组。
_THEOREM_DECLS = frozenset({"newtheorem", "declaretheorem"})
_ENV_DECLS = frozenset({"newenvironment", "renewenvironment", "provideenvironment"})
_XPARSE_ENV_DECLS = frozenset(
    {
        "NewDocumentEnvironment",
        "RenewDocumentEnvironment",
        "ProvideDocumentEnvironment",
        "DeclareDocumentEnvironment",
    }
)

#: 从块内抽取 caption 槽位的命令。
_CAPTION_CS = frozenset({"\\caption", "\\captionof"})

_DATA_FILE = "data/environments.json"


# --------------------------------------------------------------------- 分类表


@dataclass(frozen=True)
class EnvRule:
    """分类表里的一条：环境名 → 掩码策略。"""

    classification: str  # "prose" | "heavy"
    category: str | None = None  # heavy 才有意义
    verbatim: bool = False


@dataclass(frozen=True)
class EnvironmentTable:
    """环境分类表（`tongtu/data/environments.json` 的内存形态）。"""

    rules: Mapping[str, EnvRule]
    version: int = 0
    starred_inherit: bool = True

    def lookup(self, name: str) -> EnvRule | None:
        """查表；`starred_inherit` 时 `figure*` 回落到 `figure`。"""
        rule = self.rules.get(name)
        if rule is None and self.starred_inherit and name.endswith("*"):
            rule = self.rules.get(name[:-1])
        return rule

    @property
    def verbatim_envs(self) -> frozenset[str]:
        """环境体不参与词法解析的环境名（含星号变体）。"""
        names = {n for n, r in self.rules.items() if r.verbatim}
        if self.starred_inherit:
            names |= {n + "*" for n in names}
        return frozenset(names)


def _parse_table(raw: Mapping) -> EnvironmentTable:
    rules: dict[str, EnvRule] = {}
    entries = raw.get("environments")
    if not isinstance(entries, dict):
        raise MaskError("分类表缺少 environments 映射")
    for name, entry in entries.items():
        classification = entry.get("classification")
        if classification not in ("prose", "heavy"):
            raise MaskError(f"分类表条目 {name!r} 的 classification 非法：{classification!r}")
        category = entry.get("category")
        if classification == "heavy":
            category = category or "other"
            if category not in CATEGORIES:
                raise MaskError(f"分类表条目 {name!r} 的 category 非法：{category!r}")
        elif category is not None:
            raise MaskError(f"分类表条目 {name!r} 是散文环境，不应有 category")
        rules[name] = EnvRule(
            classification=classification,
            category=category,
            verbatim=bool(entry.get("verbatim", False)),
        )
    return EnvironmentTable(
        rules=MappingProxyType(rules),
        version=int(raw.get("version", 0)),
        starred_inherit=bool(raw.get("starred_inherit", True)),
    )


@lru_cache(maxsize=1)
def load_environment_table() -> EnvironmentTable:
    """读打包进 wheel 的分类表（`tongtu/data/environments.json`）。"""
    text = files("tongtu").joinpath(_DATA_FILE).read_text(encoding="utf-8")
    return _parse_table(json.loads(text))


# ----------------------------------------------------------------- 枚举与分类


@dataclass(frozen=True)
class EnvQuery:
    """交给关节③（`arbiter` 回调）的一次提问。"""

    name: str
    count: int
    sample: str  # 首次出现处的源码片段（截断），供 agent 判断


#: 关节③：未知环境的外部裁决回调。返回 "prose" / "heavy"，或 None 表示「不知道」。
EnvArbiter = Callable[[EnvQuery], str | None]


@dataclass(frozen=True)
class EnvironmentInfo:
    """一个环境名的枚举与分类结论（进 blocks.json 的 environments 数组）。"""

    name: str
    classification: str  # "prose" | "heavy"
    decided_by: str  # table / newtheorem / newenvironment / agent / default
    count: int
    category: str | None = None

    def to_json(self) -> dict:
        data = {
            "name": self.name,
            "classification": self.classification,
            "decided_by": self.decided_by,
            "count": self.count,
        }
        if self.category is not None:
            data["category"] = self.category
        return data


def enumerate_environments(
    src: str, verbatim_envs: Iterable[str] = ()
) -> dict[str, tuple[int, int]]:
    """完备枚举全文的 `\\begin{X}`：名字 → (出现次数, 首次出现偏移)。

    这一步**不需要任何先验知识**（架构 §3.1 第 2 条）：词法扫一遍，注释里的、`\\verb`
    里的、verbatim 环境体里的 `\\begin{X}` 都不算数（它们不是环境）。重环境内部的嵌套
    环境照常计入——枚举面向「文档里出现过哪些环境名」，不是「哪些会被单独掩码」。
    """
    counts: dict[str, tuple[int, int]] = {}
    lexer = Lexer(src, verbatim_envs=frozenset(verbatim_envs))
    for tok in lexer:
        if tok.kind != "begin" or tok.name is None:
            continue
        seen = counts.get(tok.name)
        counts[tok.name] = (1, tok.start) if seen is None else (seen[0] + 1, seen[1])
    return counts


def _skip_spaces(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _skip_optionals(s: str, i: int) -> int:
    """跳过零个或多个可选参数 `[...]`（含其间空白）。"""
    while True:
        j = _skip_spaces(s, i)
        if j < len(s) and s[j] == "[":
            try:
                i = find_bracket_arg(s, j) + 1
            except TexLexError:
                return i
        else:
            return i


def _read_group(s: str, i: int) -> tuple[str, int] | None:
    """读一个必选参数 `{...}`，返回 (内容, 之后的位置)；不是 `{` 或不配平则 None。"""
    j = _skip_spaces(s, i)
    if j >= len(s) or s[j] != "{":
        return None
    try:
        close = find_balanced(s, j)
    except TexLexError:
        return None
    return s[j + 1 : close], close + 1


def parse_environment_declarations(
    src: str, table: EnvironmentTable
) -> dict[str, tuple[str, str | None, str]]:
    """解析文档自带的环境声明 → 名字 → (classification, category, decided_by)。

    * `\\newtheorem{X}{…}` / `\\newtheorem*{X}{…}` / `\\declaretheorem[…]{X}`
      → **散文**（定理类可翻，`\\begin{X}`/`\\end{X}` 包裹原样保留）。
    * `\\newenvironment{X}[n][d]{begin}{end}` 及 xparse 的
      `\\NewDocumentEnvironment{X}{spec}{begin}{end}`
      → 看 begin 代码**委托**给了谁：里面第一个已知的重环境（`\\begin{figure}`、
      `\\begin{lstlisting}` …）决定分类与 category；没有委托任何重环境则判散文。
      依据：这类宏绝大多数是「给散文加装饰」（自定义定理、强调块），真正包裹重环境的
      写法会在 begin 代码里显式 `\\begin{…}`，词法上可判。

    声明优先于分类表——它是这篇文档自己给出的知识，比全局表更具体。
    """
    decls: dict[str, tuple[str, str | None, str]] = {}
    lexer = Lexer(src, verbatim_envs=table.verbatim_envs)
    for tok in lexer:
        if tok.kind != "control":
            continue
        cs = src[tok.start : tok.end][1:]
        if cs not in _THEOREM_DECLS and cs not in _ENV_DECLS and cs not in _XPARSE_ENV_DECLS:
            continue
        i = tok.end
        if src[i : i + 1] == "*":
            i += 1
        i = _skip_optionals(src, i)
        head = _read_group(src, i)
        if head is None:
            continue
        name, i = head
        name = name.strip()
        if not name:
            continue
        if cs in _THEOREM_DECLS:
            decls[name] = ("prose", None, "newtheorem")
            lexer.pos = i
            continue
        if cs in _XPARSE_ENV_DECLS:
            spec = _read_group(src, i)  # 参数规格，跳过
            if spec is None:
                continue
            i = spec[1]
        else:
            i = _skip_optionals(src, i)
        body = _read_group(src, i)
        if body is None:
            continue
        begin_code, i = body
        decls[name] = (*_classify_definition(begin_code, table), "newenvironment")
        lexer.pos = i
    return decls


def _classify_definition(begin_code: str, table: EnvironmentTable) -> tuple[str, str | None]:
    """`\\newenvironment` 的 begin 代码委托给了哪个已知重环境。"""
    for m in BEGIN_RE.finditer(begin_code):
        rule = table.lookup(m.group(1))
        if rule is not None and rule.classification == "heavy":
            return "heavy", rule.category or "other"
    return "prose", None


def classify_environments(
    counts: Mapping[str, tuple[int, int]],
    *,
    src: str = "",
    table: EnvironmentTable | None = None,
    declarations: Mapping[str, tuple[str, str | None, str]] | None = None,
    arbiter: EnvArbiter | None = None,
    sample_chars: int = 200,
) -> dict[str, EnvironmentInfo]:
    """把枚举出来的环境名逐个定性（优先级见模块文档）。

    `arbiter` 是**关节③的 hook**：M3 会接上 agent 判断，本期默认 None——未知环境一律
    保守整块掩码、记 `category=unknown`，供 report 统计（架构 §3.1 第 2 条）。
    """
    table = table or load_environment_table()
    declarations = declarations or {}
    result: dict[str, EnvironmentInfo] = {}
    for name, (count, offset) in counts.items():
        decl = declarations.get(name)
        if decl is not None:
            classification, category, decided_by = decl
            result[name] = EnvironmentInfo(name, classification, decided_by, count, category)
            continue
        rule = table.lookup(name)
        if rule is not None:
            result[name] = EnvironmentInfo(
                name, rule.classification, "table", count, rule.category
            )
            continue
        verdict = None
        if arbiter is not None:
            sample = src[offset : offset + sample_chars]
            try:
                verdict = arbiter(EnvQuery(name=name, count=count, sample=sample))
            except Exception:  # noqa: BLE001 —— 关节不可用不得拖垮确定性骨架
                verdict = None
        if verdict == "prose":
            result[name] = EnvironmentInfo(name, "prose", "agent", count, None)
        elif verdict == "heavy":
            result[name] = EnvironmentInfo(name, "heavy", "agent", count, "other")
        else:
            result[name] = EnvironmentInfo(name, "heavy", "default", count, "unknown")
    return result


# ----------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class Caption:
    """块内抽出的可翻译文本槽位。"""

    id: str
    placeholder: str
    block_id: str
    kind: str  # caption / captionof / title / abstract / other
    text: str  # 逐字节原文（回填的兜底与恒等自检的依据）
    stream_text: str  # 掩码流中的单行展示文本

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "placeholder": self.placeholder,
            "block_id": self.block_id,
            "kind": self.kind,
            "text": self.text,
            "stream_text": self.stream_text,
        }

    @classmethod
    def from_json(cls, data: Mapping) -> "Caption":
        return cls(
            id=data["id"],
            placeholder=data["placeholder"],
            block_id=data.get("block_id", ""),
            kind=data.get("kind", "other"),
            text=data["text"],
            stream_text=data.get("stream_text", ""),
        )


@dataclass(frozen=True)
class Block:
    """一个掩码块：掩码流中的 `⟦BLK-n⟧` 与它背后的原始 TeX。"""

    id: str
    placeholder: str
    category: str
    tex: str
    span: tuple[int, int]
    line_span: tuple[int, int] | None = None
    environment: str | None = None
    label: str | None = None
    caption_ids: tuple[str, ...] = ()

    @property
    def survey_restore(self) -> bool:
        """survey 通读视图是否回填原文（数学类 True，表格/图/代码 False）。"""
        return self.category in SURVEY_RESTORE_CATEGORIES

    def to_json(self) -> dict:
        span = {"start": self.span[0], "end": self.span[1]}
        if self.line_span is not None:
            span["line_start"], span["line_end"] = self.line_span
        data = {
            "id": self.id,
            "placeholder": self.placeholder,
            "category": self.category,
            "tex": self.tex,
            "span": span,
            "survey_restore": self.survey_restore,
        }
        if self.environment is not None:
            data["environment"] = self.environment
        if self.label is not None or self.category != "preamble":
            data["label"] = self.label
        if self.caption_ids:
            data["caption_ids"] = list(self.caption_ids)
        return data

    @classmethod
    def from_json(cls, data: Mapping) -> "Block":
        span = data.get("span") or {}
        line_span = None
        if "line_start" in span and "line_end" in span:
            line_span = (span["line_start"], span["line_end"])
        return cls(
            id=data["id"],
            placeholder=data["placeholder"],
            category=data["category"],
            tex=data["tex"],
            span=(span.get("start", 0), span.get("end", 0)),
            line_span=line_span,
            environment=data.get("environment"),
            label=data.get("label"),
            caption_ids=tuple(data.get("caption_ids", ())),
        )


@dataclass(frozen=True)
class MaskResult:
    """mask 的全部产出。`masked` 进翻译流，其余进 blocks.json。"""

    masked: str
    blocks: tuple[Block, ...]
    captions: tuple[Caption, ...]
    environments: tuple[EnvironmentInfo, ...]
    warnings: tuple[str, ...] = ()
    source_chars: int = 0
    source_sha256: str = ""

    @property
    def block_map(self) -> dict[str, Block]:
        return {b.placeholder: b for b in self.blocks}

    @property
    def caption_map(self) -> dict[str, Caption]:
        return {c.placeholder: c for c in self.captions}

    def to_blocks_json(
        self,
        *,
        source_path: str = "build/flat.tex",
        roundtrip_ok: bool | None = None,
    ) -> dict:
        """按 docs/schemas/blocks.schema.json 组装 blocks.json 的内容。"""
        data: dict = {
            "contract_version": CONTRACT_VERSION,
            "source": {
                "path": source_path,
                "sha256": self.source_sha256,
                "chars": self.source_chars,
            },
            "blocks": [b.to_json() for b in self.blocks],
            "captions": [c.to_json() for c in self.captions],
            "environments": [e.to_json() for e in self.environments],
        }
        if roundtrip_ok is not None:
            data["roundtrip_ok"] = roundtrip_ok
        return data


# ------------------------------------------------------------------- 掩码器


class _Masker:
    def __init__(self, src: str, classes: Mapping[str, EnvironmentInfo], table: EnvironmentTable):
        self.src = src
        self.classes = classes
        self.table = table
        self.verbatim_envs = table.verbatim_envs
        self.blocks: list[Block] = []
        self.captions: list[Caption] = []
        self.warnings: list[str] = []
        self._starts = line_starts(src)
        self._body_pos = 0  # 掩码块吞掉的正文位置，由 _mask_environment 带出

    # ---- 记账

    def _line_span(self, start: int, end: int) -> tuple[int, int]:
        return (
            line_number(self._starts, start),
            line_number(self._starts, max(start, end - 1)),
        )

    def _next_block_id(self) -> str:
        return f"BLK-{len(self.blocks)}"

    def _add_block(
        self,
        *,
        block_id: str,
        category: str,
        tex: str,
        span: tuple[int, int],
        environment: str | None = None,
        label: str | None = None,
        caption_ids: Sequence[str] = (),
    ) -> Block:
        block = Block(
            id=block_id,
            placeholder=BLOCK_TOKEN.format(block_id.removeprefix("BLK-")),
            category=category,
            tex=tex,
            span=span,
            line_span=self._line_span(*span),
            environment=environment,
            label=label,
            caption_ids=tuple(caption_ids),
        )
        self.blocks.append(block)
        return block

    def _make_caption(
        self, *, block_id: str, kind: str, text: str, index: int
    ) -> tuple[Caption, str]:
        cap_id = f"CAP-{index}"
        placeholder = CAPTION_TOKEN.format(index)
        caption = Caption(
            id=cap_id,
            placeholder=placeholder,
            block_id=block_id,
            kind=kind,
            text=text,
            stream_text=_stream_text(kind, text, self.verbatim_envs),
        )
        return caption, f"{placeholder} {caption.stream_text}"

    def _warn(self, message: str, offset: int) -> None:
        line = line_number(self._starts, offset)
        self.warnings.append(f"第 {line} 行：{message}")

    # ---- 前导区

    def mask_preamble(self, preamble: str) -> tuple[Block, list[str]]:
        """前导区整体 → BLK-0，其中 `\\title` 与（前导区内的）abstract 抽成 CAP 槽位。

        有些文档类（v2 遇到过 deepseek.cls）要求 abstract 写在 `\\begin{document}` 之前，
        不抽出来摘要就永远留在英文原文里。
        """
        block_id = self._next_block_id()
        pending: list[Caption] = []
        lines: list[str] = []
        pieces: list[str] = []
        pos = 0

        for kind, start, end in self._preamble_slots(preamble):
            text = preamble[start:end]
            if not text.strip():
                continue
            caption, line = self._make_caption(
                block_id=block_id, kind=kind, text=text, index=len(self.captions) + len(pending)
            )
            pending.append(caption)
            lines.append(line)
            pieces.append(preamble[pos:start])
            pieces.append(caption.placeholder)
            pos = end
        pieces.append(preamble[pos:])

        self.captions.extend(pending)
        block = self._add_block(
            block_id=block_id,
            category="preamble",
            tex="".join(pieces),
            span=(0, len(preamble)),
            caption_ids=[c.id for c in pending],
        )
        return block, lines

    def _preamble_slots(self, preamble: str) -> list[tuple[str, int, int]]:
        """前导区里可翻译文本的位置：`\\title{…}` 与 `\\begin{abstract}…\\end{abstract}`。

        词法扫描而非正则：注释掉的 `% \\title{旧标题}` 与 verbatim 里的都不算数（v2 的
        `re.search` 会抓到它们）。
        """
        slots: list[tuple[str, int, int]] = []
        lexer = Lexer(preamble, verbatim_envs=self.verbatim_envs)
        seen_title = False
        for tok in lexer:
            if tok.kind == "control" and not seen_title:
                if preamble[tok.start : tok.end] != "\\title":
                    continue
                i = _skip_optionals(preamble, tok.end)
                group = _read_group(preamble, i)
                if group is None:
                    continue
                text, after = group
                slots.append(("title", after - 1 - len(text), after - 1))
                seen_title = True
                lexer.pos = after
                continue
            if tok.kind == "begin" and tok.name == "abstract":
                try:
                    end = find_env_end(preamble, tok.start, "abstract", self.verbatim_envs)
                except TexLexError as exc:
                    self._warn(f"前导区 abstract 未闭合（{exc}）", tok.start)
                    continue
                inner_end = preamble.rfind("\\end", tok.end, end)
                slots.append(("abstract", tok.end, inner_end))
                lexer.pos = end
        ordered: list[tuple[str, int, int]] = []
        for slot in sorted(slots, key=lambda s: s[1]):
            if ordered and slot[1] < ordered[-1][2]:
                continue  # 槽位不得重叠（`\title` 写在 abstract 里这种病态写法）
            ordered.append(slot)
        return ordered

    # ---- 正文

    def mask_body(self, body: str, base: int) -> str:
        """正文扫描：重环境 → 块，注释 → 块，散文（含行内公式与 `\\cite`）原样留流。"""
        out: list[str] = []
        pos = 0
        lexer = Lexer(body, verbatim_envs=self.verbatim_envs)
        while True:
            tok = lexer.next()
            if tok is None:
                break
            if tok.kind == "comment":
                end = _comment_run_end(body, tok.start)
                out.append(body[pos : tok.start])
                block = self._add_block(
                    block_id=self._next_block_id(),
                    category="comment",
                    tex=body[tok.start : end],
                    span=(base + tok.start, base + end),
                )
                out.append(block.placeholder)
                pos = lexer.pos = end
                continue
            if tok.kind != "begin" or tok.name is None:
                continue
            info = self.classes.get(tok.name)
            if info is None or info.classification != "heavy":
                continue
            try:
                end = tok.env_end if tok.env_end is not None else find_env_end(
                    body, tok.start, tok.name, self.verbatim_envs
                )
            except TexLexError as exc:
                self._warn(f"{exc}；该环境不掩码，原样留在掩码流里", base + tok.start)
                continue
            out.append(body[pos : tok.start])
            out.append(self._mask_environment(body, tok.start, end, tok.name, base))
            pos = lexer.pos = self._body_pos
            continue
        out.append(body[pos:])
        return "".join(out)

    def _mask_environment(self, body: str, start: int, end: int, name: str, base: int) -> str:
        """把 [start, end) 的重环境换成占位符（必要时后随 CAP 行），返回替换文本。

        `self._body_pos` 带出扫描应当继续的位置——有 CAP 行时块会**吸收**紧随其后的
        换行（见模块文档），故它可能是 end + 1。
        """
        raw = body[start:end]
        block_id = self._next_block_id()
        tex, pending, lines = self._extract_captions(raw, block_id, base + start)
        span_end = end
        if lines and end < len(body) and body[end] == "\n":
            tex += "\n"
            span_end += 1
        self.captions.extend(pending)
        block = self._add_block(
            block_id=block_id,
            category=self.classes[name].category or "other",
            tex=tex,
            span=(base + start, base + span_end),
            environment=name,
            label=_find_label(raw, self.verbatim_envs),
            caption_ids=[c.id for c in pending],
        )
        self._body_pos = span_end
        if not lines:
            return block.placeholder
        return block.placeholder + "\n" + "\n".join(lines) + "\n"

    def _extract_captions(
        self, raw: str, block_id: str, offset: int
    ) -> tuple[str, list[Caption], list[str]]:
        """把块内 `\\caption` / `\\captionof` 的文本参数换成 CAP 槽位。

        与 v2 的正则相比：控制序列按词法比对（`\\captionsetup` 不再可能误伤）、
        `\\caption*` 认得、可选短标题（进目录的那个）也抽出来翻译、可选参数按括号配平
        扫描（`\\caption[$a[i]$]{…}` 不再截断）、注释与 verbatim 子环境内的假 caption
        不抽取。任一步不配平就整块放弃抽取并记警告——块照样整块掩码，不损坏。
        """
        pieces: list[str] = []
        pending: list[Caption] = []
        lines: list[str] = []
        pos = 0
        lexer = Lexer(raw, verbatim_envs=self.verbatim_envs)
        for tok in lexer:
            if tok.kind != "control" or raw[tok.start : tok.end] not in _CAPTION_CS:
                continue
            kind = "captionof" if raw[tok.start : tok.end] == "\\captionof" else "caption"
            i = tok.end
            if raw[i : i + 1] == "*":
                i += 1
            if kind == "captionof":
                group = _read_group(raw, i)  # {figure} —— 浮动体类型，不翻译
                if group is None:
                    continue
                i = group[1]
            slots: list[tuple[int, int]] = []
            j = _skip_spaces(raw, i)
            if raw[j : j + 1] == "[":
                try:
                    close = find_bracket_arg(raw, j)
                except TexLexError as exc:
                    self._warn(f"caption 可选参数不配平（{exc}）", offset + j)
                    continue
                slots.append((j + 1, close))
                j = _skip_spaces(raw, close + 1)
            if raw[j : j + 1] != "{":
                continue
            try:
                close = find_balanced(raw, j)
            except TexLexError as exc:
                self._warn(f"caption 参数不配平（{exc}）", offset + j)
                continue
            slots.append((j + 1, close))
            for start, stop in slots:
                text = raw[start:stop]
                if not text.strip():
                    continue
                caption, line = self._make_caption(
                    block_id=block_id,
                    kind=kind,
                    text=text,
                    index=len(self.captions) + len(pending),
                )
                pending.append(caption)
                lines.append(line)
                pieces.append(raw[pos:start])
                pieces.append(caption.placeholder)
                pos = stop
            lexer.pos = close + 1
        pieces.append(raw[pos:])
        return "".join(pieces), pending, lines


def _stream_text(kind: str, text: str, verbatim_envs: frozenset[str]) -> str:
    """caption/title/abstract 在掩码流里的单行展示文本。"""
    if kind == "abstract":
        paragraphs = [
            strip_comments_inline(p, verbatim_envs)
            for p in re.split(r"\n[ \t]*\n", text)
            if p.strip()
        ]
        return " \\par ".join(paragraphs)
    return strip_comments_inline(text, verbatim_envs)


def _comment_run_end(s: str, i: int) -> int:
    """注释块的终点：从 `s[i]` 的 `%` 起，连续的**整行注释**合并成一块。

    合并的意义是别让 `%%%%%%` 分隔线和被注释掉的整段各自变成一个占位符，把掩码流搅碎。
    行尾注释（`word% 说明`）只吞它自己那一段，前面的正文照常进流。
    """
    end = skip_comment(s, i)
    while end < len(s):
        j = end + 1
        k = j
        while k < len(s) and s[k] in " \t":
            k += 1
        if k < len(s) and s[k] == "%":
            end = skip_comment(s, k)
        else:
            break
    return end


def _find_label(raw: str, verbatim_envs: frozenset[str]) -> str | None:
    """块内第一个 `\\label{…}` 的值（anchors 合成的交叉引用键）。"""
    lexer = Lexer(raw, verbatim_envs=verbatim_envs)
    for tok in lexer:
        if tok.kind != "control" or raw[tok.start : tok.end] != "\\label":
            continue
        group = _read_group(raw, tok.end)
        if group is not None:
            return group[0].strip()
    return None


def _find_document_start(src: str, verbatim_envs: frozenset[str]) -> int | None:
    """`\\begin{document}` 的结束位置（词法判定：注释掉的那个不算）。"""
    lexer = Lexer(src, verbatim_envs=verbatim_envs)
    for tok in lexer:
        if tok.kind == "begin" and tok.name == "document":
            return tok.end
    return None


# --------------------------------------------------------------------- 入口


def mask(
    src: str,
    *,
    table: EnvironmentTable | None = None,
    arbiter: EnvArbiter | None = None,
) -> MaskResult:
    """把 LaTeX 源码切成「可翻译流 + 块清单」。

    `arbiter` 是关节③的 hook（未知环境的外部裁决），默认 None = 保守整块掩码。
    源码本身的畸形（未闭合环境、不配平花括号）不抛异常：该段降级为不掩码并记
    `MaskResult.warnings`，掩码流仍与原文一一对应，恒等自检照样成立。
    """
    table = table or load_environment_table()
    counts = enumerate_environments(src, table.verbatim_envs)
    declarations = parse_environment_declarations(src, table)
    classes = classify_environments(
        counts, src=src, table=table, declarations=declarations, arbiter=arbiter
    )

    masker = _Masker(src, classes, table)
    document_start = _find_document_start(src, table.verbatim_envs)
    if document_start is None:
        masker.warnings.append("未找到 \\begin{document}：全文按正文处理，BLK-0 为空块")
        document_start = 0
    preamble, body = src[:document_start], src[document_start:]

    block0, cap_lines = masker.mask_preamble(preamble)
    masked_body = masker.mask_body(body, document_start)

    parts = [block0.placeholder]
    if cap_lines:
        parts.append("\n" + "\n".join(cap_lines) + "\n")
        if masked_body.startswith("\n"):
            # 与正文块同样的换行吸收规则：让 CAP 行的行尾换行顶替原文的那个换行，
            # 掩码流因此不多出空行；被吸收的换行进 BLK-0 的 tex，回填后逐字节复原。
            block0 = _absorb_newline(masker, block0)
            masked_body = masked_body[1:]
    parts.append(masked_body)

    return MaskResult(
        masked="".join(parts),
        blocks=tuple(masker.blocks),
        captions=tuple(masker.captions),
        environments=tuple(
            sorted(classes.values(), key=lambda e: (-e.count, e.name))
        ),
        warnings=tuple(masker.warnings),
        source_chars=len(src),
        source_sha256=hashlib.sha256(src.encode("utf-8")).hexdigest(),
    )


def _absorb_newline(masker: _Masker, block: Block) -> Block:
    """把紧随 BLK-0 的那个换行并入前导区块（见 mask() 中的调用点注释）。"""
    updated = Block(
        id=block.id,
        placeholder=block.placeholder,
        category=block.category,
        tex=block.tex + "\n",
        span=(block.span[0], block.span[1] + 1),
        line_span=masker._line_span(block.span[0], block.span[1] + 1),
        environment=block.environment,
        label=block.label,
        caption_ids=block.caption_ids,
    )
    masker.blocks[masker.blocks.index(block)] = updated
    return updated


def roundtrip_diff(
    src: str,
    *,
    result: MaskResult | None = None,
    table: EnvironmentTable | None = None,
    arbiter: EnvArbiter | None = None,
) -> str | None:
    """运行时往返自检（架构 §3.1 第 3 条）：不恒等时返回首处差异的人话描述。

    掩码完成后立刻 unmask 与原文逐字节比对——任何解析缺陷都在花第一分钱 LLM 调用之前
    当场暴露。「四海皆准」的保证不是解析器无缺陷，而是缺陷在每篇论文上都当场可测。

    阶段驱动器请把已经算好的 `result` 传进来：重算一次不但浪费，接上关节③之后还会**再问
    一次 agent**（arbiter 未必幂等，且逐次付费）。
    """
    from .unmask import UnmaskError, unmask  # 局部导入：unmask 依赖本模块的数据结构

    if result is None:
        result = mask(src, table=table, arbiter=arbiter)
    try:
        restored = unmask(result.masked, result)
    except UnmaskError as exc:
        return f"回填失败：{exc}"
    if restored == src:
        return None
    limit = min(len(src), len(restored))
    at = next((i for i in range(limit) if src[i] != restored[i]), limit)
    starts = line_starts(src)
    return (
        f"往返不恒等：首处差异在偏移 {at}（第 {line_number(starts, at)} 行）"
        f"，原文 {src[at : at + 40]!r} vs 回填 {restored[at : at + 40]!r}"
    )


def roundtrip_check(
    src: str,
    *,
    result: MaskResult | None = None,
    table: EnvironmentTable | None = None,
    arbiter: EnvArbiter | None = None,
) -> bool:
    """`unmask(mask(x)) == x` 是否成立。阶段驱动器据此放行（不过不放行）。"""
    return roundtrip_diff(src, result=result, table=table, arbiter=arbiter) is None
