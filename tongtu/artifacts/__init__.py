"""artifact model —— artifact contract 的字段级权威定义（架构 §5，决策 A.22）。

字段、类型与默认值只在本包的 model 一处定义，artifact 的读写都经过 model 校验，
``--json`` 事件流的事件类型同样以 model 定义。契约变更 = 改 model 并 bump
``CONTRACT_VERSION``，契约 diff 在 model 代码的 diff 上审阅。语言中立的 JSON Schema
由 model 生成（``model_json_schema()``），不提交进仓库。

包结构：每个 JSON artifact 一个模块，顶层 model 以 ``*Artifact`` 命名并在此导出；
子结构 model 从各模块取（如 ``tongtu.artifacts.blocks.Block``）。
"""

from __future__ import annotations

from .anchors import AnchorsArtifact
from .base import CONTRACT_VERSION, ArtifactModel, ContractVersion, FallbackReason, Sha256
from .blocks import BlocksArtifact
from .brief import BriefArtifact
from .chunks import ChunksArtifact
from .events import EVENT_ADAPTER, ChunkProgressEvent, Event, ResultEvent, StageEndEvent, StageStartEvent
from .figures import FiguresArtifact
from .glossary import GlossaryArtifact
from .report import ReportArtifact

__all__ = [
    "CONTRACT_VERSION",
    "EVENT_ADAPTER",
    "AnchorsArtifact",
    "ArtifactModel",
    "BlocksArtifact",
    "BriefArtifact",
    "ChunkProgressEvent",
    "ChunksArtifact",
    "ContractVersion",
    "Event",
    "FallbackReason",
    "FiguresArtifact",
    "GlossaryArtifact",
    "ReportArtifact",
    "ResultEvent",
    "Sha256",
    "StageEndEvent",
    "StageStartEvent",
]
