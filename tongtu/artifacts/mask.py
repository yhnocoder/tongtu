"""mask 阶段两份产物的字段级权威定义：stage manifest 与 `build/blocks.json`。

`MaskManifest` 是 `build/manifests/mask.json`：`status` 是唯一分流依据；`precompile_sha256`
与 `environments_table_sha256` 是本阶段的两个输入 hash；`masked_sha256` / `masked_bytes` 与
`blocks_sha256` 是输出 hash，下游判定「输入未变不重算」时引用它们；`environments` 是环境
分类结论一览，report 的 agent 干预统计与固化判据取自这里。

`BlocksFile` 是 `build/blocks.json`：被摘出去的 block 与抽出的 caption 槽位，从 mask 一直用
到 compile 与 figures，也是 artifact contract 的一员。类别、分类结论与槽位种类的取值词表由
`tongtu/masking.py` 定义（掩码文本层产出它们），本模块按那份词表给字段定类型。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from ..masking import BlockCategory, CaptionKind, DecidedBy, EnvironmentClass


class MaskStatus(StrEnum):
    """mask 的结果状态，JSON 序列化为小写字符串值。

    `OK` 之外都不是异常，调用方按状态分流：`PRECOMPILE_MISSING` 与 `PRECOMPILE_NOT_OK` 是
    前置条件不满足（上游 precompile 没跑、产物不在，或它的结论不是 ok）；`MASK_FAILED` 覆盖
    解码失败、哨兵冲突、词法遍历的结构错误、往返自检不恒等与环境分类表读不到，各种情形由
    message 区分。
    """

    OK = "ok"
    PRECOMPILE_MISSING = "precompile_missing"
    PRECOMPILE_NOT_OK = "precompile_not_ok"
    MASK_FAILED = "mask_failed"


class EnvironmentDecisionRecord(BaseModel):
    """一个环境名的分类结论与两个计数，manifest 的 `environments` 逐名记一条。"""

    classification: EnvironmentClass = Field(
        description="分类结论：text 留在掩码文本里，non_translatable 整块掩码。字段名不用 class，那是 Python 关键字"
    )
    category: str = Field(default="", description="block 类别，取值见 masking.BlockCategory；分类为 text 时为空")
    decided_by: DecidedBy = Field(description="分类结论的来源，四级下沉各占一个取值")
    occurrences: int = Field(default=0, description=r"第一遍词法扫描枚举到的 \begin{X} 次数")
    blocks: int = Field(
        default=0, description="第二遍实际成块的次数；嵌在已掩 block 内部的环境为 0，将来只对大于 0 的未知环境提问"
    )


class MaskManifest(BaseModel):
    """mask 的结构化结果，同时是它的 stage manifest。"""

    stage: Literal["mask"] = "mask"
    status: MaskStatus
    precompile_sha256: str = Field(
        default="", description="build/precompile.tex 的 sha256，本阶段的输入 hash 之一，从 precompile manifest 转录"
    )
    environments_table_sha256: str = Field(
        default="", description="环境分类表文件内容的 sha256，本阶段的输入 hash 之二；改表即失效重算"
    )
    masked_sha256: str = Field(default="", description="build/masked.tex 的 sha256，本阶段的输出 hash 之一")
    masked_bytes: int = Field(default=0, description="build/masked.tex 的字节数")
    precompile_chars: int = Field(default=0, description="build/precompile.tex 解码后的字符数，观察值")
    masked_chars: int = Field(default=0, description="build/masked.tex 解码后的字符数，观察值")
    blocks_sha256: str = Field(
        default="",
        description="build/blocks.json 的 sha256，本阶段的输出 hash 之二；改分类表可能只改它而不动掩码文本",
    )
    environments: dict[str, EnvironmentDecisionRecord] = Field(
        default_factory=dict, description="环境分类结论一览，按环境名排序"
    )
    blocks_total: int = Field(default=0, description="blocks.json 里 block 记录的条数")
    captions_total: int = Field(default=0, description="blocks.json 里 caption 槽位记录的条数")
    masked_chars_ratio: float = Field(default=0.0, description="掩码文本占原文的字符比，观察值")
    precompile_status: str = Field(default="", description="本次读到的 precompile manifest 的状态，排查用")
    fetch_status: str = Field(default="", description="precompile manifest 记录的 fetch 状态，退出码映射与排查用")
    warnings: list[str] = Field(default_factory=list)
    message: str = ""


class BlockRecord(BaseModel):
    """一个被摘出去的 block。"""

    id: str = Field(description="block id，形如 BLK-7；掩码文本里的 token 由它拼出")
    category: BlockCategory = Field(description="block 类别，survey view 与 figures 按它分流")
    environment: str = Field(default="", description="环境名；结构性 block（前导区、注释等）为空")
    decided_by: str = Field(default="", description="环境分类结论的来源，取值见 masking.DecidedBy；结构性 block 为空")
    labels: list[str] = Field(default_factory=list, description=r"block 内 \label 的参数清单")
    tex: str = Field(description="block 的原始 TeX，带 CAP 槽位的形式：caption 必选参数处是 ⟦CAP-n⟧")
    start: int = Field(description="在 precompile.tex 解码后字符序列中的起始偏移")
    end: int = Field(description="在 precompile.tex 解码后字符序列中的结束偏移（不含）")
    line: int = Field(description="起始行号，1 起，排查用")


class CaptionRecord(BaseModel):
    """一个 caption 槽位。"""

    id: str = Field(description="槽位 id，形如 CAP-2；掩码文本里的 token 由它拼出")
    block_id: str = Field(description="所属 block 的 id")
    kind: CaptionKind = Field(description="槽位来源：前导区的 abstract 环境体，或 block 内的 caption 命令")
    tex: str = Field(description="槽位的原始文本")
    masked_text: str = Field(description="掩码文本里的单行形态，unmask 回填判定的比较基准")


class BlocksFile(BaseModel):
    """`build/blocks.json` 的内容：被摘出去的 block 与抽出的 caption 槽位。"""

    blocks: list[BlockRecord] = Field(default_factory=list)
    captions: list[CaptionRecord] = Field(default_factory=list)
