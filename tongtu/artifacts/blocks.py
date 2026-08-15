"""blocks.json —— mask 阶段产出的 block 清单（架构 §3 mask 节，决策 A.10）。

每个 non-translatable environment 的类型、label、原始 TeX 与源码位置，外加从 block 内
抽出的可翻译 caption 槽位。unmask 按此文件 backfill；survey view 的参数化 backfill 按
category 判定（数学类、表格、算法 backfill 原文——架构 §3 survey 节），不设单独字段：
它是 category 的函数，物化进 artifact 只会成为第二份会漂移的定义。掩码无损是硬约束：掩码流是给 LLM 看的投影（block
成了 ⟦BLK-n⟧、caption 被单行化摘成 ⟦CAP-k⟧ 行），凡从流里消失的内容都在本文件里留有
逐字节原文，故 unmask(mask(x)) == x 逐字节成立。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .base import CONTRACT_VERSION, ArtifactModel, ContractVersion, Sha256

#: block 分类，驱动 unmask 的参数化 backfill 与 survey view 的选择性 backfill。
#: comment = 正文里被掩掉的注释段（掩码无损的前提）；unknown = 分类表与 hook③ 都没给出
#: 结论、按保守默认整体掩码的环境，供 report 统计覆盖率。
BlockCategory = Literal[
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
]

#: 环境分类结论：text（进翻译流）或 non-translatable（整块掩码）。
EnvironmentClassification = Literal["text", "non-translatable"]

#: 分类来源：分类表 / \newtheorem 声明自动归类 / \newenvironment 声明按其委托的环境归类 /
#: hook③ agent 判断 / 保守默认。
EnvironmentDecidedBy = Literal["table", "newtheorem", "newenvironment", "agent", "default"]

#: caption 槽位来源。preamble 抽出的可译槽位只有 abstract；\title 不抽成槽位，
#: 标题保留英文原题（架构 §3 mask 节）。
CaptionKind = Literal["caption", "captionof", "abstract", "other"]


class Span(ArtifactModel):
    """在掩码输入文件中的源码位置，字符偏移半开区间 [start, end)。"""

    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=0)]
    line_start: Annotated[int, Field(ge=1)] | None = Field(default=None, description="1-based 起始行号。")
    line_end: Annotated[int, Field(ge=1)] | None = Field(default=None, description="1-based 结束行号。")

    @model_validator(mode="after")
    def _check_order(self) -> Span:
        if self.end < self.start:
            raise ValueError(f"span 区间颠倒：end={self.end} < start={self.start}")
        if self.line_start is not None and self.line_end is not None and self.line_end < self.line_start:
            raise ValueError(f"span 行号颠倒：line_end={self.line_end} < line_start={self.line_start}")
        return self


class SourceFile(ArtifactModel):
    """掩码输入（flat.tex）的溯源信息。"""

    path: str = Field(description="相对工作目录的路径，通常为 build/flat.tex。")
    sha256: Sha256 = Field(description="输入文件内容 hash，往返自检与增量构建的依据。")
    chars: Annotated[int, Field(ge=0)] = Field(description="输入字符数，用于「多少内容进了 LLM」的统计。")


class Block(ArtifactModel):
    """一个被掩码的 block。"""

    id: str = Field(pattern=r"^BLK-[0-9]+$", description="block id，前导区恒为 BLK-0。")
    placeholder: str = Field(
        description="掩码流中的不透明 placeholder，形如 ⟦BLK-3⟧。validate 的 placeholder multiset 比对以此为单位。"
    )
    category: BlockCategory
    environment: str | None = Field(
        default=None, description="LaTeX 环境名（如 align*、tabularx）；前导区 block 无此字段。"
    )
    label: str | None = Field(default=None, description="block 内 \\label{...} 的值，anchors 合成时的交叉引用键。")
    tex: str = Field(
        description="原始 TeX 全文（含 \\begin/\\end），caption 已替换为 ⟦CAP-k⟧ 槽位，backfill 即用此串。"
        "带 CAP 行的 block 会把紧随其后的那个换行一并吸收进来（span 同步 +1），"
        "使掩码流里的 CAP 行不额外增加空行——故本串可能以换行结尾。"
    )
    span: Span
    caption_ids: list[str] = Field(default_factory=list, description="本 block 内抽出的 caption 槽位 id。")


class Caption(ArtifactModel):
    """从 block 内抽出的可翻译文本槽位。"""

    id: str = Field(pattern=r"^CAP-[0-9]+$", description="caption 槽位 id。")
    placeholder: str = Field(description="block 内的槽位 placeholder，形如 ⟦CAP-2⟧。")
    block_id: str = Field(description="所属 block id（abstract 槽位属于 preamble block）。")
    kind: CaptionKind
    text: str = Field(description="槽位逐字节原文（含换行与注释）。backfill 的权威来源，也是往返自检恒等的依据。")
    stream_text: str = Field(
        description="该槽位在掩码流 ⟦CAP-k⟧ 行里的单行化展示文本（剥注释、折叠空白；abstract 的段落以 \\par 相连）。"
        "unmask 据此判定「未改动 ⇒ backfill 原文」：流中该行文本与本字段相同或为空即视为未翻译，"
        "backfill text 原文，否则当作译文。"
    )


class EnvironmentUsage(ArtifactModel):
    """全文 \\begin{X} 环境名的完备枚举及其分类结论（架构 §3 mask 节第 2 层）。"""

    name: str = Field(description="环境名，如 theorem、tabularx。")
    classification: EnvironmentClassification
    decided_by: EnvironmentDecidedBy
    category: BlockCategory | None = Field(
        default=None, description="non-translatable 环境对应的 block 分类；text 环境无此字段。"
    )
    count: Annotated[int, Field(ge=1)] = Field(description="该环境在全文中出现次数（枚举来自实际出现，至少 1）。")


class BlocksArtifact(ArtifactModel):
    """blocks.json 全文件。"""

    contract_version: ContractVersion = CONTRACT_VERSION
    source: SourceFile
    blocks: list[Block] = Field(description="被掩码的 block，按在源码中出现的顺序排列。")
    captions: list[Caption] = Field(default_factory=list, description="从 block 内抽出的可翻译文本槽位。")
    environments: list[EnvironmentUsage] = Field(default_factory=list)
    roundtrip_ok: bool = Field(description="运行时往返自检结果：unmask(mask(x)) == x。为 false 时流水线不得放行。")
