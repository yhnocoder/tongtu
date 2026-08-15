"""术语表三层合并与块内命中（架构 §8、§4，决策 5/11）。

三层，后者覆盖前者：

| 层 | 位置 | 谁写 |
|---|---|---|
| `global` | `$XDG_CONFIG_HOME/tongtu/glossary.json`，未设即 `~/.config/tongtu/glossary.json` | 用户，跨论文 |
| `paper`  | **`<workdir>/glossary.json`**（与 `src/` `build/` `out/` `logs/` 同级） | 用户，本篇 |
| `cli`    | `--glossary FILE`（可多次，靠后的覆盖靠前的） | 用户 / wenshu 从 R2 落的文件 |

论文层刻意**不放在 `src/` 里**：`src/` 是 e-print 原始解包、只读不改（架构 §5），把用户
可编辑的文件混进去会污染 fetch 的树 hash（每编辑一次术语表就重跑 flatten 与 baseline）。
放在工作目录根则与四区平级、`build/` 被整体删掉也不受影响。

**输入表与决策表分离**（架构 §8）：本模块合并出来的是三层**输入表**；survey 阶段把
agent 的新词决策叠上去（:func:`with_agent_decisions`），产出 `glossary.json` **决策表**
（`source="agent"` 标出哪些是 agent 决定的）。合并语义：

* 条目级覆盖——`terms` 按 `term` 归并，同名后者胜；
* `do_not_translate` 取并集（同名后者胜，保留后者的 `match` / `note`）；
* `style` 逐字段覆盖，`style_version` 取最后一个提供它的层；
* **用户条目优先于 agent 决策**——agent 只能新增，不能改写任何一层用户已经写死的词。

## 与缓存 key 的关系（架构 §4）

术语表拆两半参与块级缓存 key：**术语条目**按块内命中计入（:func:`relevant_terms`），
改一个词只失效含它的块；**全局文风规则**单列 `style_version`，bump 即全量重翻。
:func:`hit_terms` 是命中判定的**唯一**实现，`tongtu.stages.translate` 直接用它——两处
各写一遍迟早会漂，而漂了就意味着缓存 key 与提示词不是一回事。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from . import CONTRACT_VERSION, prompts
from .schema_check import SchemaError
from .schema_check import check as schema_check

__all__ = [
    "CONFIG_DIRNAME",
    "DEFAULT_STYLE_VERSION",
    "GLOSSARY_NAME",
    "Glossary",
    "GlossaryError",
    "Layer",
    "NoTranslate",
    "SOURCES",
    "Style",
    "Term",
    "XDG_ENV",
    "content_hash",
    "empty",
    "global_path",
    "hit_terms",
    "load_file",
    "load_glossary",
    "load_layers",
    "merge",
    "paper_path",
    "relevant_terms",
    "term_map",
    "with_agent_decisions",
]


class GlossaryError(ValueError):
    """术语表文件读不出来或不合 schema。调用方决定是终止还是降级为空表。"""


#: 术语表文件名（论文层与全局层共用同一个名字）。
GLOSSARY_NAME = "glossary.json"

#: 全局层所在的配置目录名（XDG 之下）。
CONFIG_DIRNAME = "tongtu"

#: XDG 配置根的环境变量。
XDG_ENV = "XDG_CONFIG_HOME"

#: 层名，按优先级从低到高（= schema 的 merged_from.layer 枚举顺序）。
SOURCES: tuple[str, ...] = ("global", "paper", "cli", "agent")

#: 没有任何一层给出 style_version 时的默认值（进块级缓存 key）：prompt 资产自带的那份
#: 文风规则的版本号。用户表写了 style_version 就以用户的为准——架构 §8 把它定义为术语表
#: 第三段的字段，用户 bump 它即显式要求全量重翻。
DEFAULT_STYLE_VERSION = prompts.STYLE_VERSION


# --------------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class Term:
    """一条术语唯一译法（`glossary.schema.json` 的 `terms[]`）。"""

    term: str
    translation: str
    aliases: tuple[str, ...] = ()
    keep_original: bool | None = None
    note: str = ""
    source: str = ""
    decided_at: str = ""

    @property
    def key(self) -> str:
        """归并键：原文词形（大小写不敏感——同一个词只该有一个译法）。"""
        return self.term.strip().lower()

    @property
    def forms(self) -> tuple[str, ...]:
        """参与命中的全部写法：词形本身 + 同义写法。"""
        return (self.term, *self.aliases)

    def to_json(self) -> dict:
        data: dict = {"term": self.term, "translation": self.translation}
        if self.aliases:
            data["aliases"] = list(self.aliases)
        if self.keep_original is not None:
            data["keep_original"] = self.keep_original
        if self.note:
            data["note"] = self.note
        if self.source:
            data["source"] = self.source
        if self.decided_at:
            data["decided_at"] = self.decided_at
        return data

    @classmethod
    def from_json(cls, data: Mapping, *, source: str = "") -> Term:
        return cls(
            term=str(data["term"]),
            translation=str(data["translation"]),
            aliases=tuple(str(a) for a in data.get("aliases", ())),
            keep_original=data.get("keep_original"),
            note=str(data.get("note", "")),
            source=str(data.get("source") or source),
            decided_at=str(data.get("decided_at", "")),
        )


@dataclass(frozen=True)
class NoTranslate:
    """一条不译清单条目（`glossary.schema.json` 的 `do_not_translate[]`）。"""

    term: str
    match: str = ""
    note: str = ""
    source: str = ""

    @property
    def key(self) -> str:
        return self.term.strip().lower()

    def to_json(self) -> dict:
        data: dict = {"term": self.term}
        if self.match:
            data["match"] = self.match
        if self.note:
            data["note"] = self.note
        if self.source:
            data["source"] = self.source
        return data

    @classmethod
    def from_json(cls, data: Mapping, *, source: str = "") -> NoTranslate:
        return cls(
            term=str(data["term"]),
            match=str(data.get("match", "")),
            note=str(data.get("note", "")),
            source=str(data.get("source") or source),
        )


@dataclass(frozen=True)
class Style:
    """文风约定（`glossary.schema.json` 的 `style`）。改动即全量重翻，故单列版本号。"""

    style_version: str = DEFAULT_STYLE_VERSION
    tone: str = ""
    translator_notes: bool | None = None
    rules: tuple[str, ...] = ()

    def to_json(self) -> dict:
        data: dict = {"style_version": self.style_version}
        if self.tone:
            data["tone"] = self.tone
        if self.translator_notes is not None:
            data["translator_notes"] = self.translator_notes
        if self.rules:
            data["rules"] = list(self.rules)
        return data

    @classmethod
    def from_json(cls, data: Mapping) -> Style:
        return cls(
            style_version=str(data.get("style_version", DEFAULT_STYLE_VERSION)),
            tone=str(data.get("tone", "")),
            translator_notes=data.get("translator_notes"),
            rules=tuple(str(r) for r in data.get("rules", ())),
        )

    def overlay(self, other: Style, *, other_had_version: bool) -> Style:
        """后者覆盖前者，**逐字段**：后者没写的字段保留前者的值。"""
        return Style(
            style_version=other.style_version if other_had_version else self.style_version,
            tone=other.tone or self.tone,
            translator_notes=(other.translator_notes if other.translator_notes is not None else self.translator_notes),
            rules=other.rules or self.rules,
        )


@dataclass(frozen=True)
class Layer:
    """一层输入表：来源 + 路径 + 内容（路径为空表示不是从文件来的）。"""

    layer: str
    path: str
    glossary: Glossary

    def to_json(self) -> dict:
        return {
            "layer": self.layer,
            "path": self.path,
            "entries": self.glossary.entry_count,
        }


@dataclass(frozen=True)
class Glossary:
    """一份术语表（输入表或决策表，同一 schema 的两种角色）。"""

    do_not_translate: tuple[NoTranslate, ...] = ()
    terms: tuple[Term, ...] = ()
    style: Style = field(default_factory=Style)
    merged_from: tuple[Layer, ...] = ()
    style_version_set: bool = False
    """本表是否**显式**写了 style_version（合并时用来判断该不该覆盖前一层）。"""

    @property
    def entry_count(self) -> int:
        return len(self.terms) + len(self.do_not_translate)

    @property
    def style_version(self) -> str:
        return self.style.style_version

    def term(self, name: str) -> Term | None:
        key = name.strip().lower()
        return next((t for t in self.terms if t.key == key), None)

    def to_json(self, *, merged_from: bool = True) -> dict:
        """按 `docs/schemas/glossary.schema.json` 组装（决策表落盘用这份）。"""
        data: dict = {"contract_version": CONTRACT_VERSION}
        if self.do_not_translate:
            data["do_not_translate"] = [d.to_json() for d in self.do_not_translate]
        if self.terms:
            data["terms"] = [t.to_json() for t in self.terms]
        data["style"] = self.style.to_json()
        if merged_from and self.merged_from:
            data["merged_from"] = [layer.to_json() for layer in self.merged_from]
        return data

    @classmethod
    def from_json(cls, data: Mapping, *, source: str = "") -> Glossary:
        style_raw = data.get("style") or {}
        return cls(
            do_not_translate=tuple(NoTranslate.from_json(d, source=source) for d in data.get("do_not_translate", ())),
            terms=tuple(Term.from_json(t, source=source) for t in data.get("terms", ())),
            style=Style.from_json(style_raw),
            style_version_set="style_version" in style_raw,
        )


def empty() -> Glossary:
    """空表（哪一层都没有时的中性元）。"""
    return Glossary()


# ------------------------------------------------------------------- 层的定位


def global_path(env: Mapping[str, str] | None = None) -> Path:
    """全局层路径：`$XDG_CONFIG_HOME/tongtu/glossary.json`，未设则 `~/.config/…`。"""
    environ = os.environ if env is None else env
    root = (environ.get(XDG_ENV) or "").strip()
    base = Path(root).expanduser() if root else Path("~/.config").expanduser()
    return base / CONFIG_DIRNAME / GLOSSARY_NAME


def paper_path(workdir) -> Path:
    """论文层路径：`<workdir>/glossary.json`（`workdir` 可为 `Workdir` 或路径）。"""
    path = getattr(workdir, "path", workdir)
    return Path(path) / GLOSSARY_NAME


# ------------------------------------------------------------------- 读与合并


def load_file(path: str | os.PathLike[str], *, source: str = "") -> Glossary:
    """读一份术语表文件并过 schema 校验。文件不存在 → 空表（不是错误）。"""
    path = Path(path)
    if not path.is_file():
        return empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GlossaryError(f"术语表读不出来：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise GlossaryError(f"术语表不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise GlossaryError(f"术语表顶层应为对象：{path}")
    # 用户手写的表不必写全 schema 的必填项（contract_version / style.style_version）：
    # 校验的是**补了默认值的副本**，读进内存的仍是原文——否则「文件里没写 style_version」
    # 与「写了默认值」就分不开了，而合并时正靠这个区分决定该不该覆盖上一层。
    style = data.get("style")
    checked = {
        **data,
        "contract_version": data.get("contract_version", CONTRACT_VERSION),
        "style": (
            {"style_version": DEFAULT_STYLE_VERSION, **style}
            if isinstance(style, dict)
            else ({"style_version": DEFAULT_STYLE_VERSION} if style is None else style)
        ),
    }
    try:
        errors = schema_check(checked, "glossary")
    except SchemaError:
        errors = []  # schema 目录不可用不该让用户的表读不进来（survey 会记警告）
    if errors:
        raise GlossaryError(f"术语表不合 schema：{path}\n  " + "\n  ".join(errors[:5]))
    return Glossary.from_json(data, source=source)


def merge(layers: Sequence[Layer]) -> Glossary:
    """把若干层合并成一张输入表（后者覆盖前者，语义见模块文档）。"""
    terms: dict[str, Term] = {}
    dnt: dict[str, NoTranslate] = {}
    style = Style()
    style_set = False
    for layer in layers:
        for entry in layer.glossary.do_not_translate:
            dnt[entry.key] = replace(entry, source=entry.source or layer.layer)
        for entry in layer.glossary.terms:
            terms[entry.key] = replace(entry, source=entry.source or layer.layer)
        style = style.overlay(layer.glossary.style, other_had_version=layer.glossary.style_version_set)
        style_set = style_set or layer.glossary.style_version_set
    return Glossary(
        do_not_translate=tuple(sorted(dnt.values(), key=lambda d: d.key)),
        terms=tuple(sorted(terms.values(), key=lambda t: t.key)),
        style=style,
        merged_from=tuple(layers),
        style_version_set=style_set,
    )


def load_layers(
    *,
    workdir=None,
    cli: Sequence[str | os.PathLike[str]] = (),
    env: Mapping[str, str] | None = None,
    include_global: bool = True,
) -> tuple[Layer, ...]:
    """定位并读入三层（缺哪层就没有哪层；`--glossary` 按给出顺序，靠后的优先）。"""
    found: list[Layer] = []
    candidates: list[tuple[str, Path]] = []
    if include_global:
        candidates.append(("global", global_path(env)))
    if workdir is not None:
        candidates.append(("paper", paper_path(workdir)))
    candidates.extend(("cli", Path(p).expanduser()) for p in cli)

    for layer, path in candidates:
        if layer == "cli" and not path.is_file():
            raise GlossaryError(f"--glossary 指定的文件不存在：{path}")
        if not path.is_file():
            continue
        found.append(Layer(layer=layer, path=str(path), glossary=load_file(path, source=layer)))
    return tuple(found)


def load_glossary(
    *,
    workdir=None,
    cli: Sequence[str | os.PathLike[str]] = (),
    env: Mapping[str, str] | None = None,
    include_global: bool = True,
) -> Glossary:
    """三层合并的输入表（`load_layers` + `merge` 的便捷入口）。"""
    return merge(load_layers(workdir=workdir, cli=cli, env=env, include_global=include_global))


def with_agent_decisions(
    base: Glossary,
    *,
    terms: Iterable[Mapping] = (),
    do_not_translate: Iterable[Mapping] = (),
    decided_at: str | None = None,
) -> Glossary:
    """把 agent 的新词决策叠到输入表上，产出**决策表**。

    **用户条目优先于 agent 决策**（架构 §8）：任何一层用户表已经写过的词，agent 的版本
    直接丢弃——这一条不是策略而是纪律，故在此处硬编码，不给调用方开关。
    """
    stamp = decided_at or _now()
    known_terms = {t.key for t in base.terms}
    known_dnt = {d.key for d in base.do_not_translate}
    added_terms: list[Term] = []
    added_dnt: list[NoTranslate] = []

    for raw in terms:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("term") or "").strip()
        translation = str(raw.get("translation") or "").strip()
        if not name or not translation or name.lower() in known_terms:
            continue
        known_terms.add(name.lower())
        added_terms.append(
            Term(
                term=name,
                translation=translation,
                aliases=tuple(str(a) for a in raw.get("aliases", ()) if str(a).strip()),
                keep_original=raw.get("keep_original") if isinstance(raw.get("keep_original"), bool) else None,
                note=str(raw.get("note", "")),
                source="agent",
                decided_at=stamp,
            )
        )
    for raw in do_not_translate:
        name = str(raw.get("term") or "").strip() if isinstance(raw, Mapping) else str(raw or "").strip()
        if not name or name.lower() in known_dnt:
            continue
        known_dnt.add(name.lower())
        note = str(raw.get("note", "")) if isinstance(raw, Mapping) else ""
        match = str(raw.get("match", "")) if isinstance(raw, Mapping) else ""
        added_dnt.append(
            NoTranslate(
                term=name,
                match=match if match in ("exact", "case-insensitive", "word") else "",
                note=note,
                source="agent",
            )
        )

    return Glossary(
        do_not_translate=tuple(sorted([*base.do_not_translate, *added_dnt], key=lambda d: d.key)),
        terms=tuple(sorted([*base.terms, *added_terms], key=lambda t: t.key)),
        style=base.style,
        merged_from=base.merged_from,
        style_version_set=base.style_version_set,
    )


# ------------------------------------------------------------------ 命中与 hash


def term_map(glossary: Glossary) -> dict[str, str]:
    """`{可命中写法: 译法}`——喂给 translate 的上下文组装与缓存 key。

    * `terms` 的词形与 `aliases` 都进表（同义写法命中同一译法）；
    * `do_not_translate` 的词映射到**它自己**——「不译」在提示词里的表达就是「译法即原
      词」，这样不译清单同样按块内命中参与缓存 key（改不译清单只失效含它的块）。

    用户条目与 agent 条目在这一层不再区分：合并时用户已经赢过了。
    """
    mapping: dict[str, str] = {}
    for entry in glossary.do_not_translate:
        if entry.term.strip():
            mapping[entry.term] = entry.term
    for entry in glossary.terms:
        for form in entry.forms:
            if form.strip():
                mapping[form] = entry.translation
    return mapping


def hit_terms(text: str, mapping: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """块内命中的术语子集（架构 §4 的 `relevant_terms(chunk)`），按 (词, 译法) 排序。

    大小写不敏感的子串命中：论文里的术语会带复数、连字符、所有格，词边界匹配反而漏；
    命中多算一个词的代价只是提示词长一行与缓存粒度粗一点，漏掉则是术语不一致。
    排序保证同一块的命中集合**逐次相同**——它直接进缓存 key，不稳定即缓存失效。
    """
    if not mapping:
        return ()
    lowered = text.lower()
    return tuple(sorted((term, str(value)) for term, value in mapping.items() if term and term.lower() in lowered))


def relevant_terms(chunk_text: str, glossary: Glossary | None) -> tuple[tuple[str, str], ...]:
    """块内命中的术语子集（架构 §4 缓存 key 的那一项）。

    与 :func:`hit_terms` 同一实现，只是入参是 :class:`Glossary` 而非裸映射——
    `tongtu.stages.translate` 用的是后者，两者结果逐字节一致。
    """
    if glossary is None:
        return ()
    return hit_terms(chunk_text, term_map(glossary))


def content_hash(glossary: Glossary) -> str:
    """术语表的**内容** hash：只含影响译文的部分，不含 `merged_from` 与决策时间戳。

    进 translate 的阶段 manifest（架构 §4）。刻意排除时间戳一类的易变字段——survey 重跑
    出同样的决策时不该把全部块的翻译一起失效掉。
    """
    payload = {
        "do_not_translate": [{"term": d.term, "match": d.match} for d in glossary.do_not_translate],
        "terms": [
            {
                "term": t.term,
                "translation": t.translation,
                "aliases": list(t.aliases),
                "keep_original": t.keep_original,
            }
            for t in glossary.terms
        ],
        "style": glossary.style.to_json(),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
