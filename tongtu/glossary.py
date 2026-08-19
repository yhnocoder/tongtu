"""术语表的解析、合并与命中匹配：survey 的合并过滤与 translate 的逐 chunk 命中共用一份实现。

本模块只做文本层的工作，不读文件也不写文件：input glossary 的内容由调用方读进来交给
`parse`，合并顺序由调用方按层级排好交给 `merge`，命中判定由 `relevant_terms` 给出。survey
阶段用它把三层输入合并成 resolved glossary 并按全文命中过滤（阶段驱动器在
`tongtu/stages/survey.py`），translate 用同一个 `relevant_terms` 取每个 chunk 命中的词条——
两处走同一份判定，不会出现「survey 滤掉了、translate 本可命中」的分歧。

input glossary 文件形态三段，皆可缺省：`do_not_translate`（字符串列表）、`terms`（词到译法
的映射）、`style`（一段写给译者的额外要求，原样进提示词）。合并语义见 docs/stages/survey.md：
词条按词覆盖，跨区段同样覆盖，`do_not_translate` 视为「译法 = 保留原文」的词条，不做并集；
词的同一性按大小写不敏感比较，resolved 保留胜出层的原写法；`style` 是整段文本，高层给了就
整段取高层的，不做拼接。

合并单元是一份文件而不是一个层级：全局与论文目录各只有一份文件，两者等同；命令行的
`--glossary` 可以给多份，靠后的文件优先，同一个词在两份命令行文件里分处两个区段由这个
顺序判定，不算冲突。同一份文件内同一个词同时落在 `terms` 与 `do_not_translate` 才是冲突，
按配置错误处理，不猜测意图。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: input glossary 与 resolved glossary 共用的文件名：全局配置目录、论文工作目录与 build/ 下同名。
GLOSSARY_FILENAME = "glossary.json"

#: input glossary 认得的三个顶层字段，此外的字段一律按配置错误拒绝（挡拼错的字段名）。
DO_NOT_TRANSLATE_FIELD = "do_not_translate"
TERMS_FIELD = "terms"
STYLE_FIELD = "style"
KNOWN_FIELDS: tuple[str, ...] = (DO_NOT_TRANSLATE_FIELD, TERMS_FIELD, STYLE_FIELD)


class GlossaryError(ValueError):
    """input glossary 不可解析或不符合形状，由 survey 转 `glossary_invalid`。"""


class GlossaryLayer(StrEnum):
    """词条来源层，也是 resolved glossary 里 `decided_by` 的取值。

    优先级从低到高：全局配置目录的表、论文工作目录的表、命令行 `--glossary`。hook④ 接线
    后在最低处增一个 survey 层（模型的术语决策，见 docs/stages/survey.md）。
    """

    GLOBAL = "global"
    PAPER = "paper"
    CLI = "cli"


@dataclass(frozen=True)
class TermRecord:
    """一份文件里的一条词条。`translation` 为 None 表示保留原文，即 `do_not_translate` 段。"""

    word: str
    translation: str | None


@dataclass(frozen=True)
class InputGlossary:
    """一份 input glossary 文件解析后的内容。`style` 为 None 表示这份文件没写 style 段。"""

    terms: tuple[TermRecord, ...] = ()
    style: str | None = None


@dataclass(frozen=True)
class GlossarySource:
    """一个合并单元：它属于哪一层、读自哪个文件、内容是什么。

    `content` 为 None 表示该层缺席（文件不存在，或命令行未给出 `--glossary`）；缺席的层同样
    进输入 hash，以空占位。
    """

    layer: GlossaryLayer
    path: Path | None = None
    content: str | None = None


@dataclass(frozen=True)
class GlossaryEntry:
    """合并后的一条词条：胜出层的原写法、译法与来源层。`translation` 为 None 即保留原文。"""

    word: str
    translation: str | None
    decided_by: GlossaryLayer


@dataclass(frozen=True)
class MergedGlossary:
    """三层合并后的结果，尚未按全文命中过滤。`entries` 按词排序，使输出 hash 与输入顺序无关。

    `style` 是最高层给出的那段文本；三层都没写、或最高层写的是空白，都为 None——「没有额外
    要求」只有这一种表示。高层写空白因而也是一种用法：本篇不要低层配的那段要求。
    """

    entries: tuple[GlossaryEntry, ...] = ()
    style: str | None = None


# ------------------------------------------------------------------ 解析


def parse(content: str, origin: str) -> InputGlossary:
    """把一份 input glossary 的文本解析成 `InputGlossary`；不合形状抛 `GlossaryError`。

    `origin` 是出错时报给用户的文件路径。词与译法两侧的空白剥除后不得为空；同一份文件内
    同一个词（大小写不敏感）给出两条不一致的记录即冲突。
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise GlossaryError(f"{origin} 不是合法 JSON：{error}") from error
    if not isinstance(data, dict):
        raise GlossaryError(f"{origin} 的顶层必须是对象，实际是 {type(data).__name__}")
    unknown = [key for key in data if key not in KNOWN_FIELDS]
    if unknown:
        raise GlossaryError(f"{origin} 出现未知字段 {unknown[0]!r}，input glossary 只认 {'、'.join(KNOWN_FIELDS)} 三段")

    records: list[TermRecord] = []
    seen: dict[str, TermRecord] = {}
    for word in _parse_do_not_translate(data.get(DO_NOT_TRANSLATE_FIELD), origin):
        _record(records, seen, TermRecord(word=word, translation=None), origin)
    for word, translation in _parse_terms(data.get(TERMS_FIELD), origin):
        _record(records, seen, TermRecord(word=word, translation=translation), origin)
    return InputGlossary(terms=tuple(records), style=_parse_style(data.get(STYLE_FIELD), origin))


def _parse_do_not_translate(value: object, origin: str) -> list[str]:
    """解析 `do_not_translate` 段：字符串列表，逐项剥除两侧空白后不得为空。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise GlossaryError(f"{origin} 的 {DO_NOT_TRANSLATE_FIELD} 必须是字符串列表，实际是 {type(value).__name__}")
    words: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise GlossaryError(
                f"{origin} 的 {DO_NOT_TRANSLATE_FIELD}[{index}] 必须是字符串，实际是 {type(item).__name__}"
            )
        word = item.strip()
        if not word:
            raise GlossaryError(f"{origin} 的 {DO_NOT_TRANSLATE_FIELD}[{index}] 是空词")
        words.append(word)
    return words


def _parse_terms(value: object, origin: str) -> list[tuple[str, str]]:
    """解析 `terms` 段：词到译法的映射，词与译法剥除两侧空白后都不得为空。"""
    if value is None:
        return []
    if not isinstance(value, dict):
        raise GlossaryError(f"{origin} 的 {TERMS_FIELD} 必须是对象（词到译法的映射），实际是 {type(value).__name__}")
    pairs: list[tuple[str, str]] = []
    for raw_word, raw_translation in value.items():
        word = raw_word.strip()
        if not word:
            raise GlossaryError(f"{origin} 的 {TERMS_FIELD} 里有空词")
        if not isinstance(raw_translation, str):
            raise GlossaryError(
                f"{origin} 的 {TERMS_FIELD}[{word!r}] 的译法必须是字符串，实际是 {type(raw_translation).__name__}"
            )
        translation = raw_translation.strip()
        if not translation:
            raise GlossaryError(
                f"{origin} 的 {TERMS_FIELD}[{word!r}] 译法为空；要保留原文请写进 {DO_NOT_TRANSLATE_FIELD}"
            )
        pairs.append((word, translation))
    return pairs


def _parse_style(value: object, origin: str) -> str | None:
    """解析 `style` 段：一段写给译者的额外要求，原样进 translate 的提示词。

    这一段是可选输入，文件里没写就返回 None，survey 不替用户造内容；写了就返回剥除两侧空白
    后的原文，可以是空串——那是「本篇不要低层配的那段要求」，与没写不同，含义由合并层给出。
    引擎自带的翻译标准（反翻译腔规则、专名保留、代码与公式原样等）不在这里，它们随 prompt
    资产分发，进 `prompt_version`。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise GlossaryError(
            f"{origin} 的 {STYLE_FIELD} 必须是字符串（一段写给译者的额外要求），实际是 {type(value).__name__}"
        )
    return value.strip()


def _record(records: list[TermRecord], seen: dict[str, TermRecord], record: TermRecord, origin: str) -> None:
    """把一条词条记进本文件的清单；同一个词已有不一致的记录即冲突，完全相同的重复则忽略。"""
    key = record.word.casefold()
    previous = seen.get(key)
    if previous is None:
        seen[key] = record
        records.append(record)
        return
    if previous == record:
        return
    raise GlossaryError(
        f"{origin} 里 {record.word!r} 给出了两条不一致的记录"
        f"（{_describe_record(previous)}、{_describe_record(record)}），同一份文件内不判定优先级"
    )


def _describe_record(record: TermRecord) -> str:
    """一条词条的人读说明，用于冲突报错。"""
    if record.translation is None:
        return f"{DO_NOT_TRANSLATE_FIELD} 中的 {record.word!r}"
    return f"{TERMS_FIELD} 中的 {record.word!r} → {record.translation!r}"


# ------------------------------------------------------------------ 合并与命中


def merge(units: Sequence[tuple[GlossaryLayer, InputGlossary]]) -> MergedGlossary:
    """按给定顺序合并各份 input glossary，靠后的覆盖靠前的。

    词条按词覆盖且跨区段覆盖：同一个词（大小写不敏感）在后一份文件里出现，就整条换成后者
    的记录，原写法也随之换成后者的。`style` 是整段文本，写了 style 段的最高层整段胜出，不与
    低层拼接；最高层写的是空白则合并结果为 None，即本篇不要低层配的那段要求。
    """
    entries: dict[str, GlossaryEntry] = {}
    style: str | None = None
    for layer, unit in units:
        for record in unit.terms:
            entries[record.word.casefold()] = GlossaryEntry(
                word=record.word, translation=record.translation, decided_by=layer
            )
        if unit.style is not None:
            style = unit.style or None
    return MergedGlossary(entries=tuple(sorted(entries.values(), key=_entry_order)), style=style)


def relevant_terms(entries: Iterable[GlossaryEntry], text: str) -> tuple[GlossaryEntry, ...]:
    """取 `entries` 中在 `text` 里命中的词条，保持传入顺序。

    survey 用它对整份 `masked.tex` 过滤出 resolved glossary，translate 用它取每个 chunk 命中
    的词条。零期的命中判定是大小写不敏感的子串查找；将来精细化（词边界、词形变化）只改这
    一处，两个调用方同步变化。
    """
    haystack = text.casefold()
    return tuple(entry for entry in entries if entry.word.casefold() in haystack)


def _entry_order(entry: GlossaryEntry) -> tuple[str, str]:
    """词条的排序键：先按大小写不敏感的词，再按原写法，使同词不同写法的顺序也确定。"""
    return entry.word.casefold(), entry.word


# ------------------------------------------------------------------ 输入 hash


def input_sha256(sources: Sequence[GlossarySource]) -> str:
    """三层输入按层序规范化序列后的 sha256，即 survey 的输入 hash 之一。

    参与的是各份文件的原始内容与它们的层序，缺席的层以空占位；文件路径不参与——路径变了
    而内容相同不必重算。全局配置目录的表在工作目录之外，它的内容同样参与，改动它即失效重算。
    """
    payload = json.dumps([[str(source.layer), source.content] for source in sources], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
