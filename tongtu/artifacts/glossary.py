"""glossary.json —— 术语表（架构 §5 术语表节、§8 相关约定见 §5）。

同一 model 描述两种角色：用户可编辑的 input glossary（全局配置目录 → 论文目录内 →
--glossary，后者覆盖前者）与 artifact package 内的 resolved glossary（本篇实际生效
决策，含 agent 新决策；用户条目优先于 agent 决策）。结构三段：不译清单 / 术语唯一
译法 / style rules（style_version 所在）。术语条目按 chunk 内命中参与 cache_key，
style_version 单列——bump 即全量重翻的显式操作。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from .base import CONTRACT_VERSION, ArtifactModel, ContractVersion

#: 条目来源。用户三层（global/paper/cli）优先于 agent 决策。
EntrySource = Literal["global", "paper", "cli", "agent"]

#: 匹配方式，默认 word（词边界匹配）。
MatchMode = Literal["exact", "case-insensitive", "word"]


class DoNotTranslateEntry(ArtifactModel):
    """第一段：不译清单条目。命中即原样保留（模型名、库名、专有缩写等）。"""

    term: str = Field(description="原文词形。")
    match: MatchMode = "word"
    note: str | None = Field(default=None, description="备注，如「是数据集名，不是普通名词」。")
    source: EntrySource | None = None


class TermEntry(ArtifactModel):
    """第二段：术语唯一译法条目。一个原文词一个译法，全篇强制一致。"""

    term: str = Field(description="原文术语。")
    translation: str = Field(description="唯一中文译法。")
    aliases: list[str] = Field(default_factory=list, description="同义写法（复数、连字符变体、缩写），一并命中。")
    keep_original: bool | None = Field(default=None, description="首次出现时是否在译名后括注原文。")
    note: str | None = Field(default=None, description="决策依据或歧义提示。")
    source: EntrySource | None = None
    decided_at: datetime | None = Field(default=None, description="agent 决策时间（resolved glossary 用）。")


class StyleRules(ArtifactModel):
    """第三段：style rules。全局规则，改动即全量重翻，故单列版本号。"""

    style_version: str = Field(
        description="style rules 版本号。参与每个 chunk 的 cache_key；bump 是显式的全量重翻操作。"
    )
    tone: str | None = Field(default=None, description="整体语体，如「学术书面语，不用网络口语」。")
    translator_notes: bool | None = Field(default=None, description="译者注开关。")
    rules: list[str] = Field(
        default_factory=list, description="逐条规则（标点、数字与单位、被动句处理、专名首现处理等）。"
    )


class MergedFromEntry(ArtifactModel):
    """resolved glossary 专用：三层合并的来源清单，按优先级从低到高。"""

    layer: Literal["global", "paper", "cli"] = Field(description="全局配置目录 / 论文目录内 / --glossary。")
    path: str
    entries: Annotated[int, Field(ge=0)] | None = Field(default=None, description="该层贡献的条目数。")


class GlossaryArtifact(ArtifactModel):
    """glossary.json 全文件（input glossary 与 resolved glossary 共用）。"""

    contract_version: ContractVersion = CONTRACT_VERSION
    do_not_translate: list[DoNotTranslateEntry] = Field(default_factory=list)
    terms: list[TermEntry] = Field(default_factory=list)
    style: StyleRules
    merged_from: list[MergedFromEntry] = Field(default_factory=list)
