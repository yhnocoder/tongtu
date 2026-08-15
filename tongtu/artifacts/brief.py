"""brief.json —— survey 阶段一次读完全文产出的 full-text outline（架构 §3 survey 节，决策 A.11）。

原文 abstract 照录（不生成，避免全部 chunk 对摘要译文形成级联依赖）+ 章节结构树与每节
摘要 + 记号与命名约定 + 本篇 register。首要用途是逐 chunk 翻译的全局上下文（chunk 间
一致性靠稳定共享上下文，不靠链式传递译文）；随包分发后 read path 的 AI 摘要与会话预热
可直接复用。brief 不阻塞 pipeline：模型 JSON 校验失败重试一次仍失败，则 degrade 为
确定性骨架并记 degraded 与 warnings。
"""

from __future__ import annotations

import hashlib
import json
import warnings
from datetime import datetime
from typing import Annotated

from pydantic import Field

from .base import CONTRACT_VERSION, ArtifactModel, ContractVersion, Sha256


class PaperInfo(ArtifactModel):
    """论文标识信息，全部照录原文。"""

    arxiv_id: str | None = None
    title: str | None = Field(default=None, description="原文标题（照录）。")
    authors: list[str] = Field(default_factory=list)
    primary_category: str | None = Field(default=None, description="如 cs.CL。")


class Section(ArtifactModel):
    """章节结构树节点（递归）。"""

    id: str | None = Field(default=None, description="章节 id 或 \\label 值。")
    number: str | None = Field(default=None, description="章节号，如 3.2。")
    title: str = Field(description="原文章节标题。")
    level: Annotated[int, Field(ge=1)] | None = Field(
        default=None, description="1 = section，2 = subsection，依此类推。"
    )
    summary: str = Field(description="该节中文摘要（逐 chunk 翻译时作为全局上下文注入）；degraded 骨架中为空串。")
    is_appendix: bool = Field(default=False, description="附录不进读全文输入，但仍正常翻译。")
    children: list[Section] = Field(default_factory=list, description="子节，递归。")


class NotationEntry(ArtifactModel):
    """记号约定：全文出现的数学符号及其含义（主要来自行间公式、表格与算法体——这正是 survey view backfill 这些 block 的理由）。"""

    symbol: str = Field(description="LaTeX 形式的符号，如 \\mathcal{L}。")
    meaning: str = Field(description="中文说明。")
    first_seen: str | None = Field(default=None, description="首次出现的章节号或 block id。")


class NamingConvention(ArtifactModel):
    """命名约定：模型名 / 方法名 / 数据集等专名在全文中的统一指称方式（词级硬约束在 glossary.json，此处记的是指称习惯）。"""

    name: str
    convention: str
    note: str | None = None


class Register(ArtifactModel):
    """本篇 register：语体判断，与 glossary 的全局 style rules 叠加。"""

    tone: str | None = Field(default=None, description="如「严谨学术、少量口语化比喻」。")
    audience: str | None = Field(default=None, description="预期读者。")
    notes: list[str] = Field(default_factory=list, description="逐条语体提示。")


class GeneratedBy(ArtifactModel):
    """产生此 outline 的 hook④ 调用信息。"""

    model_id: str | None = None
    prompt_version: str | None = None
    generated_at: datetime | None = None
    input_hash: Sha256 | None = Field(
        default=None, description="读全文输入（survey view，已剔除附录与参考文献）的 hash。"
    )


# 字段名 register 沿用架构 §5 的术语；它与 pydantic 基类继承链上 ABCMeta.register 同名，
# pydantic 会发一条无实际影响的 UserWarning，此处按名精确忽略。
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message='Field name "register"')

    class BriefArtifact(ArtifactModel):
        """brief.json 全文件。"""

        contract_version: ContractVersion = CONTRACT_VERSION
        paper: PaperInfo | None = None
        abstract: str = Field(description="原文 abstract 照录，不翻译不改写，由程序从源码里取。")
        sections: list[Section] = Field(description="章节结构树（递归），每节带摘要。")
        notation: list[NotationEntry] = Field(default_factory=list)
        naming_conventions: list[NamingConvention] = Field(default_factory=list)
        register: Register | None = None
        degraded: bool = Field(
            default=False,
            description="模型返回的 JSON 校验失败、重试仍失败后的确定性骨架：章节树从标题命令扫出、"
            "abstract 照录、其余留空，pipeline 继续往下走（架构 §3 survey 节）。",
        )
        warnings: list[str] = Field(default_factory=list, description="degrade 与解析中的异常记录。")
        generated_by: GeneratedBy | None = None

        def content_hash(self) -> str:
            """brief 内容 hash，chunks.json 的 brief_hash 即此值（参与每个 chunk 的 cache_key，架构 §4）。

            口径：排除 generated_by 与 contract_version 后的字段，键排序、紧凑分隔符序列化再取
            SHA-256。generated_by 记录的是调用信息（含时间戳），进 hash 会使同样内容的 brief 在
            重跑 survey 后产生不同 hash，全部 chunk 意外失效——而 §4 要求 brief 不意外漂移；
            contract_version 的变更走显式失效，不经内容 hash。口径只在此处定义，不得在别处重写。
            """
            data = self.model_dump(mode="json", exclude_none=True, exclude={"generated_by", "contract_version"})
            payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
