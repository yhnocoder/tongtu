"""chunks.json —— 翻译记忆（架构 §4/§5，决策 A.3）。

每个 chunk「源码 hash → 译文 + 模型 / prompt 版本 / 术语快照 / 状态」。这是 incremental
retranslation 所需的全部状态，随 artifact package 分发——build/ 整体删除不丢失任何昂贵
成果，在任何新环境续跑只需此文件。cache_key 的构成见架构 §4；每条记录都存着 key 的
组成要素快照，配合 key_version，将来改 key 构成算法时可对旧记忆重算新 key 平滑迁移。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .base import CONTRACT_VERSION, ArtifactModel, ContractVersion, FallbackReason, Sha256

#: chunk 状态：translated = 译文通过 validate 与编译；fallback = 回退原文保底（详情记
#: report.json）；edited = 编译修复会话内经标注 --chunk 的 patch 改过（架构 §3 compile 节）。
ChunkStatus = Literal["translated", "fallback", "edited"]


class TermSnapshot(ArtifactModel):
    """术语快照条目：本 chunk 命中的 glossary 条目（排序后参与 cache_key）。"""

    term: str
    translation: str


class Chunk(ArtifactModel):
    """一个翻译 chunk 的记忆条目。

    每条记录自足：key 的全部组成要素（src、terms、model_id、prompt_version，加上文件级的
    brief_hash 与 style_version）都在包内，key_version 迁移与脱离 build/ 的重算不依赖其他文件。
    neighbor_src 不单独存——它是相邻 chunk 的段落，可从各条 src 重新推导，存 hash 即可。
    """

    id: str = Field(
        pattern=r"^c[0-9]{3,}$",
        description="chunk id，形如 c012；retranslate --chunks 按此指定。文档顺序即 chunks 列表顺序。",
    )
    section_path: list[str] = Field(
        default_factory=list, description='所属章节路径（章节树优先分块的结果），如 ["3", "3.2"]；first chunk 为空。'
    )
    src: str = Field(description="chunk 源码（掩码流片段）。key 重算与脱离 build/ 的人工比对都依赖它，必存。")
    src_hash: Sha256 = Field(description="空白规范化后 chunk 源码的 hash。")
    cache_key: Sha256 = Field(
        description="缓存键：hash(norm(src) + neighbor_src + 命中术语 + brief_hash + style_version + "
        "prompt_version + model_id)，构成见架构 §4。命中即跳过重翻。"
    )
    translation: str = Field(description="译文；status 为 fallback 时此处为回退所用的原文。")
    status: ChunkStatus
    fallback_reason: FallbackReason | None = Field(
        default=None, description="status 为 fallback 时必填，其余状态必须为空。"
    )
    fallback_paragraphs: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list, description="chunk 内回退到原文的段落序号（回退粒度是段落而非整个 chunk）。"
    )
    model_id: str = Field(description="实际使用的模型标识（retranslate 可逐 chunk 不同）。")
    prompt_version: str = Field(description="实际使用的 prompt 资产版本（retranslate 可逐 chunk 不同）。")
    terms: list[TermSnapshot] = Field(default_factory=list)
    neighbor_hash: Sha256 = Field(
        description="提示词携带的 neighboring context 原文（前节末段 + 后节首段）的 hash。"
        "不含前 chunk 译文——避免缓存失效沿 chunk 链级联（架构 §3 translate 节）。",
    )
    paragraph_count: Annotated[int, Field(ge=0)] = Field(
        description="原文段落数；validate 强制原译一一对应，也是回退原文的最小单位。"
    )
    translated_at: datetime | None = Field(
        default=None, description="译文产生时间；整个 chunk 回退（从未译出）时为空。"
    )

    @model_validator(mode="after")
    def _check_fallback_reason(self) -> Chunk:
        if self.status == "fallback" and self.fallback_reason is None:
            raise ValueError("status 为 fallback 时必须给出 fallback_reason")
        if self.status != "fallback" and self.fallback_reason is not None:
            raise ValueError(f"status 为 {self.status} 时不得携带 fallback_reason")
        return self


class ChunksArtifact(ArtifactModel):
    """chunks.json 全文件。"""

    contract_version: ContractVersion = CONTRACT_VERSION
    key_version: str = Field(
        default="1",
        description="cache_key 构成算法自身的版本号。将来改 key 逻辑（brief 分字段参与、按要素降级匹配、"
        "非 arXiv 来源）时，从各条记录存好的要素快照对旧记忆重算新 key，翻译记忆平滑迁移（架构 §4）。",
    )
    brief_hash: Sha256 = Field(
        description="本次翻译所依据的 brief 内容 hash（参与每个 chunk 的 cache_key）。"
        "口径 = BriefArtifact.content_hash()，排除 generated_by；brief 变即全量失效，故全包一致、只存文件级一份。"
    )
    style_version: str = Field(
        description="全局 style rules 版本号；bump 即全量失效（显式有意行为），故全包一致、只存文件级一份。"
    )
    chunks: list[Chunk] = Field(description="翻译 chunk，按文档顺序排列。")
