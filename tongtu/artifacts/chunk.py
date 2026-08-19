"""chunk 阶段产物的字段级权威定义：stage manifest。

`ChunkManifest` 是 `build/manifests/chunk.json`：`status` 是唯一分流依据；`masked_sha256` 是
输入 hash；`split_above` / `merge_below` / `chars_per_token` 是参与跳过判定的配置值（校准期
这几个数会改，不参与判定的话改完常量旧分块会静默留存）；`chunks_sha256` 是输出 hash，
translate 的 stage 级「输入未变不重算」判定以它为权威；`chunks` 是 chunk 记录列表。

chunk 文件本身是 `build/chunks/<id>.tex`，内容即掩码文本在 `start` / `end` 区间上的切片，
没有额外的结构，故不另设 artifact model。区、层级与 appendix 来路的取值词表由
`tongtu/chunking.py` 定义（分块层产出它们），本模块按那份词表给字段定类型。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from ..chunking import AppendixSource, Heading, Part


class ChunkStatus(StrEnum):
    """chunk 的结果状态，JSON 序列化为小写字符串值。

    `OK` 之外都不是异常，调用方按状态分流：`MASK_MISSING` 与 `MASK_NOT_OK` 是前置条件不满足
    （上游 mask 没跑、`build/masked.tex` 不在，或它的结论不是 ok）；`CHUNK_FAILED` 覆盖扫描的
    结构错误、定级的兜底硬判据不通过与出口自检不成立，各种情形由 message 区分。
    """

    OK = "ok"
    MASK_MISSING = "mask_missing"
    MASK_NOT_OK = "mask_not_ok"
    CHUNK_FAILED = "chunk_failed"


class ChunkRecord(BaseModel):
    """一个 chunk 的位置、内容摘要与定级结论。"""

    id: str = Field(description="chunk id，形如 c012：`c` 加三位零填充十进制序号，0 起，按文档序")
    start: int = Field(description="在 masked.tex 解码后字符序列中的起始偏移，文件内容即该区间切片")
    end: int = Field(description="在 masked.tex 解码后字符序列中的结束偏移（不含）")
    sha256: str = Field(description="chunk 文件内容的 sha256")
    token_estimate: int = Field(description="token 估算值，口径见 chunking.estimate_tokens；只用于分块决策")
    paragraphs: int = Field(description="段落计数，口径见 chunking.count_paragraphs")
    part: Part = Field(description="所属区，按 chunk 起始偏移对照区界判定")
    headings: list[Heading] = Field(
        default_factory=list,
        description="chunk 内有效深度 0 的标题，按文档序，每条含命令名与参数原文；排查、人工挑 chunk id 与拼章节标题树用",
    )
    internal_cuts: list[int] = Field(
        default_factory=list,
        description="聚合进本 chunk 的各单元起始偏移列表，首项等于 start；按更细边界拆分重试的确定性切点来源",
    )
    translatable_chars: int = Field(
        description="剥除 placeholder 后的非空白字符数；纯 placeholder chunk 要不要拉翻译会话，translate 据此判断"
    )


class ChunkManifest(BaseModel):
    """chunk 的结构化结果，同时是它的 stage manifest。"""

    stage: Literal["chunk"] = "chunk"
    status: ChunkStatus
    masked_sha256: str = Field(
        default="", description="build/masked.tex 的 sha256，本阶段的输入 hash，从 mask manifest 转录"
    )
    split_above: int = Field(default=0, description="下分线，参与跳过判定的配置值之一")
    merge_below: int = Field(default=0, description="合并线，参与跳过判定的配置值之二")
    chars_per_token: int = Field(default=0, description="token 估算系数，参与跳过判定的配置值之三")
    chunks_sha256: str = Field(
        default="",
        description="输出 hash：按文档序连接各 chunk 文件的 sha256 十六进制串再取 sha256；下游输入判定的权威",
    )
    chunks: list[ChunkRecord] = Field(default_factory=list, description="chunk 记录列表，按文档序")
    chunks_total: int = Field(default=0, description="chunk 计数")
    heading_level: str | None = Field(
        default=None, description="首选层级的命令名；null 即无标题退化路径（全文一个标题命令都没有）"
    )
    transparent_environments: list[str] = Field(
        default_factory=list, description="透明集里的环境名清单，按名排序；它们不计入有效深度"
    )
    appendix_source: AppendixSource = Field(default=AppendixSource.ABSENT, description="appendix 区起点的识别来路")
    mask_status: str = Field(default="", description="本次读到的 mask manifest 的状态，排查用")
    fetch_status: str = Field(default="", description="mask manifest 记录的 fetch 状态，退出码映射与排查用")
    warnings: list[str] = Field(
        default_factory=list, description="不阻断的情形，当前只记已下分到不可再分单元仍超过 split_above 的 chunk"
    )
    message: str = ""
