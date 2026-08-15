"""``--json`` 机器可读事件流的事件类型（架构 §6，开放问题 3：一期容器调度前冻结）。

传输形式为 JSON Lines：stdout 每行一个独立事件对象，本模块的 model 描述单个事件。
三类事件：阶段起止（stage_start / stage_end）、chunk 进度（chunk_progress）、最终结果
（result，每次运行恰好一条，流的最后一行）。每行自带 contract_version，消费方无需缓存
首行即可解析。

hook⑤ 是 work 会话（架构 §3 translate 节），翻译的重试循环在会话内部、脚本不可见，
故 chunk 进度没有重试状态——脚本能观测到的只有缓存命中、会话结束后的终审结果与回退。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from .base import CONTRACT_VERSION, ArtifactModel, ContractVersion

#: chunk 级状态：started = 会话拉起；cached = 缓存命中未调用模型；ok = 终审通过
#: （translate 的 validate 全绿、figures 的图产出齐全）；fallback = 回退原文；failed = 未产出。
ChunkProgressStatus = Literal["started", "cached", "ok", "fallback", "failed"]


class EventBase(ArtifactModel):
    """全部事件的公共字段。"""

    contract_version: ContractVersion = CONTRACT_VERSION
    ts: datetime
    run_id: str = Field(description="本次运行的标识，与 logs/ 下的 trace 关联。")
    arxiv_id: str | None = Field(default=None, description="本地目录来源时为空。")


class StageStartEvent(EventBase):
    """阶段开始。"""

    event: Literal["stage_start"] = "stage_start"
    stage: str = Field(description="阶段名，见 tongtu.stages.STAGES。")
    total: Annotated[int, Field(ge=0)] | None = Field(
        default=None, description="该阶段的工作单元总数（translate 为 chunk 数），未知则空。"
    )


class StageEndEvent(EventBase):
    """阶段结束。status = cached 表示按 manifest 判定输入未变、整段跳过。"""

    event: Literal["stage_end"] = "stage_end"
    stage: str
    status: Literal["ok", "cached", "skipped", "failed"]
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    error: str | None = Field(default=None, description="status 为 failed 时的一句话原因。")


class ChunkProgressEvent(EventBase):
    """chunk 进度（translate 逐 chunk 发出，也用于 figures 的逐图进度）。"""

    event: Literal["chunk_progress"] = "chunk_progress"
    stage: str = Field(description="translate 或 figures。")
    id: str = Field(description="chunk id（如 c012）或图 id。")
    index: Annotated[int, Field(ge=0)] | None = Field(default=None, description="0-based 序号。")
    total: Annotated[int, Field(ge=0)] | None = None
    status: ChunkProgressStatus
    reason: str | None = Field(default=None, description="fallback / failed 的原因。")


class ResultEvent(EventBase):
    """最终结果，每次运行恰好一条，流的最后一行。"""

    event: Literal["result"] = "result"
    status: Literal["ok", "ok_with_fallback", "failed"] = Field(description="与 report.json 的 status 一致。")
    exit_code: int = Field(
        description="进程退出码：0 = artifact package 完整产出（含有回退 chunk 的情形）；非 0 = 未能出包。"
    )
    out_dir: str | None = Field(default=None, description="artifact package 目录绝对路径。")
    pdf: str | None = Field(default=None, description="zh.pdf 路径，未产出则空。")
    report: str | None = Field(default=None, description="report.json 路径。")
    chunks_total: Annotated[int, Field(ge=0)] | None = None
    fallback_chunks: Annotated[int, Field(ge=0)] | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    error: str | None = None


#: 单个事件的联合类型，按 event 字段判别。
Event = Annotated[
    StageStartEvent | StageEndEvent | ChunkProgressEvent | ResultEvent,
    Field(discriminator="event"),
]

#: 解析一行事件用：EVENT_ADAPTER.validate_json(line)。
EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)
