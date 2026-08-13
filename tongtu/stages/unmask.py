"""unmask：把掩码流按块清单回填成完整 LaTeX（架构 §3、决策 10/11）。

三个调用方，同一套机制、不同参数（这正是「参数化回填」的意思）：

| 调用方 | 输入流 | `restore` | `caption_mode` |
|---|---|---|---|
| mask 的往返自检 | 原始掩码流 | 默认（全部回填） | `restore` |
| compile 阶段 | 译文流 | 默认（全部回填） | `restore` |
| survey 通读视图 | 原始掩码流 | `"survey"` | `keep` |

survey 的选择性回填视图（架构 §3 与决策 11）：数学类块回填原文——记号约定住在行间公式
里，brief 的「记号与命名约定」正需要它们；表格 / 图 / 代码 / tikz 保持占位符——对通读是纯
噪音且是 token 大头；注释块与前导区直接删掉。

caption 的回填规则是「**未改动 ⇒ 回填原文**」：流中 `⟦CAP-k⟧` 行的文本若与掩码时写进流
的展示文本相同（或为空），说明没人翻译过它，回填 blocks.json 里的逐字节原文；不同才当译
文。这条规则让默认参数下的 `unmask(mask(x)) == x` 逐字节成立，无需给自检开后门，也让
「LLM 漏译某个 caption」自动退化为保留原文而不是留下半成品。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping

from .mask import (
    BLOCK_TOKEN_RE,
    CAPTION_TOKEN_RE,
    Block,
    Caption,
    MaskResult,
)

__all__ = [
    "UnmaskError",
    "Restore",
    "RestoreSpec",
    "UnmaskResult",
    "SURVEY",
    "unmask",
    "unmask_detail",
    "survey_view",
]


class UnmaskError(ValueError):
    """回填失败：占位符残缺、重复、未知，或块清单与流对不上。"""


class Restore(str, Enum):
    """单个块的回填方式。"""

    ORIGINAL = "original"  # 回填原始 TeX
    PLACEHOLDER = "placeholder"  # 保留占位符（survey 视图里的表格/图/代码）
    DROP = "drop"  # 删除（survey 视图里的注释与前导区）


#: `restore="survey"` 的预设名。
SURVEY = "survey"

#: 掩码流里的 CAP 行（含首尾换行），与 mask 的插入规则严格互逆。
_CAPTION_LINE_RE = re.compile(r"\n?[ \t]*⟦CAP-(\d+)⟧([^\n]*)\n?")

#: survey 通读视图里直接删掉的块分类。
_SURVEY_DROP = frozenset({"comment", "preamble"})

RestoreSpec = (
    str
    | Mapping[str, "Restore | str"]
    | Callable[[Block], "Restore | str"]
    | None
)


@dataclass
class UnmaskResult:
    """回填结果与统计（compile / report 需要知道哪些 caption 回退了原文）。"""

    text: str
    used: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    duplicated: tuple[str, ...] = ()
    caption_fallbacks: tuple[str, ...] = ()
    caption_translated: tuple[str, ...] = ()


def _normalize(blocks) -> tuple[list[Block], dict[str, Caption]]:
    """接受 `MaskResult`、blocks.json 的 dict，或 (blocks, captions) 序列对。"""
    if isinstance(blocks, MaskResult):
        return list(blocks.blocks), blocks.caption_map
    if isinstance(blocks, Mapping):
        block_list = [
            b if isinstance(b, Block) else Block.from_json(b) for b in blocks.get("blocks", ())
        ]
        captions = [
            c if isinstance(c, Caption) else Caption.from_json(c)
            for c in blocks.get("captions", ())
        ]
        return block_list, {c.placeholder: c for c in captions}
    if isinstance(blocks, (tuple, list)) and len(blocks) == 2:
        block_list, captions = blocks
        return list(block_list), {c.placeholder: c for c in captions}
    raise UnmaskError(f"无法识别的块清单类型：{type(blocks).__name__}")


def _policy(restore: RestoreSpec) -> Callable[[Block], Restore]:
    if restore is None or restore == "all":
        return lambda block: Restore.ORIGINAL
    if restore == SURVEY:

        def survey(block: Block) -> Restore:
            if block.category in _SURVEY_DROP:
                return Restore.DROP
            return Restore.ORIGINAL if block.survey_restore else Restore.PLACEHOLDER

        return survey
    if isinstance(restore, str):
        raise UnmaskError(f"未知的 restore 预设：{restore!r}")
    if callable(restore):
        return lambda block: Restore(restore(block))
    if isinstance(restore, Mapping):
        table = {key: Restore(value) for key, value in restore.items()}
        return lambda block: table.get(block.category, Restore.ORIGINAL)
    raise UnmaskError(f"无法识别的 restore 参数：{restore!r}")


def unmask_detail(
    masked: str,
    blocks,
    *,
    restore: RestoreSpec = None,
    caption_mode: str = "restore",
    strict: bool = True,
) -> UnmaskResult:
    """回填并带出统计。参数含义见 `unmask`。"""
    if caption_mode not in ("restore", "keep"):
        raise UnmaskError(f"未知的 caption_mode：{caption_mode!r}")
    block_list, caption_map = _normalize(blocks)
    policy = _policy(restore)
    by_placeholder = {b.placeholder: b for b in block_list}

    stream = masked
    translations: dict[str, str] = {}
    stray: list[str] = []
    if caption_mode == "restore":

        def take(match: re.Match) -> str:
            token = f"⟦CAP-{match.group(1)}⟧"
            if token not in caption_map:
                # 流里冒出块清单里没有的 CAP 行：多半是译文把占位符编号改了，
                # 照单删除会连带删掉一整行正文——必须当错误报出来。
                stray.append(token)
                return match.group(0)
            translations[token] = match.group(2).strip()
            return ""

        stream = _CAPTION_LINE_RE.sub(take, stream)
    if strict and stray:
        raise UnmaskError(f"流中出现未知 caption 占位符：{sorted(set(stray))[:5]}")

    fallbacks: list[str] = []
    translated: list[str] = []

    def fill_captions(tex: str) -> str:
        def resolve(match: re.Match) -> str:
            token = match.group(0)
            caption = caption_map.get(token)
            if caption is None:
                if strict:
                    raise UnmaskError(f"块内出现未知 caption 占位符 {token}")
                return token
            candidate = translations.get(token)
            if candidate and candidate != caption.stream_text.strip():
                translated.append(caption.id)
                return candidate
            fallbacks.append(caption.id)
            return caption.text

        return CAPTION_TOKEN_RE.sub(resolve, tex)

    used: list[str] = []
    kept: list[str] = []
    dropped: list[str] = []

    def put(match: re.Match) -> str:
        token = match.group(0)
        block = by_placeholder.get(token)
        if block is None:
            if strict:
                raise UnmaskError(f"流中出现未知块占位符 {token}")
            return token
        used.append(token)
        mode = policy(block)
        if mode is Restore.DROP:
            dropped.append(block.id)
            return ""
        if mode is Restore.PLACEHOLDER:
            kept.append(block.id)
            return token
        return fill_captions(block.tex)

    text = BLOCK_TOKEN_RE.sub(put, stream)

    counted = Counter(used)
    duplicated = tuple(sorted(t for t, n in counted.items() if n > 1))
    missing = tuple(b.placeholder for b in block_list if b.placeholder not in counted)
    if strict:
        if duplicated:
            raise UnmaskError(f"块占位符被重复使用：{list(duplicated[:5])}")
        if missing:
            raise UnmaskError(f"译文流丢失了块占位符：{list(missing[:5])}")
        leftover = set(CAPTION_TOKEN_RE.findall(text))
        if leftover and caption_mode == "restore":
            raise UnmaskError(f"输出残留 caption 占位符：{sorted(leftover)[:5]}")

    return UnmaskResult(
        text=text,
        used=tuple(used),
        kept=tuple(kept),
        dropped=tuple(dropped),
        missing=missing,
        duplicated=duplicated,
        caption_fallbacks=tuple(fallbacks),
        caption_translated=tuple(translated),
    )


def unmask(
    masked: str,
    blocks,
    *,
    restore: RestoreSpec = None,
    caption_mode: str = "restore",
    strict: bool = True,
) -> str:
    """把掩码流（或译文流）回填成完整 LaTeX。

    * `blocks`：`MaskResult`，或 blocks.json 读出来的 dict；
    * `restore`：按块 `category` 选择回填方式——`None` 全部回填原文；`"survey"` 用通读
      视图预设；也可传 `{category: Restore}` 映射或 `Callable[[Block], Restore]`；
    * `caption_mode`：`"restore"` 取流中 `⟦CAP-k⟧` 行的译文并删除该行（未改动则回填原
      文）；`"keep"` 原样留着 CAP 行（survey 通读要读 caption）；
    * `strict`：占位符残缺 / 重复 / 未知时报错。分块校验等场景可关掉。
    """
    return unmask_detail(
        masked, blocks, restore=restore, caption_mode=caption_mode, strict=strict
    ).text


def survey_view(masked: str, blocks) -> str:
    """survey 通读输入：数学回填、表格/图/代码保持占位符、注释与前导区删掉。"""
    return unmask(masked, blocks, restore=SURVEY, caption_mode="keep", strict=False)
