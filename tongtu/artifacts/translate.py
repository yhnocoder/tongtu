"""translate 阶段产物的字段级权威定义：stage manifest。

`TranslateManifest` 是 `build/manifests/translate.json`：`status` 是唯一分流依据；
`chunks_sha256`、`glossary_sha256` 与 `brief_sha256` 是本阶段的三个输入 hash（分别从 chunk
manifest 与 survey manifest 转录）；`translated_sha256` 是输出 hash；`model_id` 与
`prompt_version` 参与跳过判定，换模型或升级 prompt 资产即作废已有译文。

复用的粒度是整个阶段：六个判定值全都不变才跳过，任一变了就整篇重翻。零期不做 chunk 级
翻译记忆，理由见 docs/stages/translate.md 复用粒度节。

译文文件本身是 `build/translated/<id>.tex`，内容即该 chunk 的译文（首尾空白与原文一致），
没有额外结构，故不另设 artifact model。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class TranslateStatus(StrEnum):
    """translate 的结果状态，JSON 序列化为小写字符串值。

    `OK` 之外都不是异常，调用方按状态分流：`CHUNK_MISSING` / `CHUNK_NOT_OK` 与
    `SURVEY_MISSING` / `SURVEY_NOT_OK` 是两个上游的前置条件不满足；`TRANSLATE_FAILED` 是
    回退比例超过阈值，或一个 chunk 的译文都没产出，具体情形由 message 区分。存在回退 chunk
    但比例未超阈值时状态仍是 `OK`，回退清单记在 `fallback_chunks`。
    """

    OK = "ok"
    CHUNK_MISSING = "chunk_missing"
    CHUNK_NOT_OK = "chunk_not_ok"
    SURVEY_MISSING = "survey_missing"
    SURVEY_NOT_OK = "survey_not_ok"
    TRANSLATE_FAILED = "translate_failed"


class ChunkTranslateStatus(StrEnum):
    """单个 chunk 的翻译结论。

    `TRANSLATED` 是译文通过四层 validate；`FALLBACK` 是重试耗尽、`ask` 失败或 worker 异常，
    该 chunk 回退原文；`SKIPPED` 是 `translatable_chars` 为 0 的纯 placeholder chunk，不调
    `ask`，原文即译文。
    """

    TRANSLATED = "translated"
    FALLBACK = "fallback"
    SKIPPED = "skipped"


class ChunkTranslateRecord(BaseModel):
    """manifest 里一个 chunk 的翻译结论。"""

    id: str = Field(description="chunk id，与 chunk manifest 的记录一一对应")
    status: ChunkTranslateStatus = Field(description="该 chunk 的翻译结论")
    sha256: str = Field(description="译文文件内容的 sha256")
    attempts: int = Field(
        default=0, description="本次执行对该 chunk 的 ask 调用次数，1 即一次通过；跳过翻译的 chunk 记 0"
    )
    failures: list[str] = Field(
        default_factory=list, description="回退时最后一次 validate 未通过的层名，或 ask 的失败现场"
    )


class TranslateManifest(BaseModel):
    """translate 的结构化结果，同时是它的 stage manifest。"""

    stage: Literal["translate"] = "translate"
    status: TranslateStatus
    chunks_sha256: str = Field(
        default="", description="chunk manifest 的输出 hash，本阶段的输入 hash 之一，从 chunk manifest 转录"
    )
    glossary_sha256: str = Field(
        default="", description="build/glossary.json 的 sha256，本阶段的输入 hash 之二，从 survey manifest 转录"
    )
    brief_sha256: str = Field(
        default="", description="build/brief.json 的 sha256，本阶段的输入 hash 之三，从 survey manifest 转录"
    )
    translated_sha256: str = Field(
        default="", description="输出 hash：按文档序连接各译文文件的 sha256 十六进制串再取 sha256"
    )
    model_id: str = Field(default="", description="本次翻译使用的模型标识，参与跳过判定")
    prompt_version: str = Field(default="", description="translate 的 prompt 资产版本，参与跳过判定")
    jobs: int = Field(default=0, description="本次使用的并发度")
    chunks: list[ChunkTranslateRecord] = Field(default_factory=list, description="逐 chunk 的翻译结论，按文档序")
    chunks_total: int = Field(default=0, description="chunk 计数")
    fallback_chunks: list[str] = Field(default_factory=list, description="回退原文的 chunk id 清单")
    fallback_ratio: float = Field(
        default=0.0, description="回退的 chunk 数除以参与翻译的 chunk 数；跳过判定命中的 chunk 不计入分母"
    )
    max_fallback_ratio: float = Field(default=0.0, description="本次使用的回退比例阈值，超过它整体判失败")
    skipped_chunks: list[str] = Field(
        default_factory=list, description="translatable_chars 为 0、不调 ask 的 chunk id 清单"
    )
    chunk_status: str = Field(default="", description="本次读到的 chunk manifest 的状态，排查用")
    survey_status: str = Field(default="", description="本次读到的 survey manifest 的状态，排查用")
    fetch_status: str = Field(default="", description="上游转录的 fetch 状态，退出码映射与排查用")
    warnings: list[str] = Field(default_factory=list)
    message: str = ""
