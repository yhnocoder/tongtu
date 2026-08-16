"""fetch 阶段的 stage manifest（`build/manifests/fetch.json`）的字段级权威定义。

`status` 是唯一分流依据；`files` 记录 `src/` 全部文件的 sha256，是下游阶段判定
「输入未变不重算」时引用的权威记录。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class FetchStatus(StrEnum):
    """fetch 的结果状态，JSON 序列化为小写字符串值。

    `OK` 之外都不是异常，调用方按状态分流；`PDF_ONLY` 是分支而非错误——源是 PDF
    而非 LaTeX 源码，走 degraded path。
    """

    OK = "ok"
    PDF_ONLY = "pdf_only"
    EMPTY = "empty"
    DOWNLOAD_FAILED = "download_failed"
    UNPACK_FAILED = "unpack_failed"
    SOURCE_MISSING = "source_missing"


#: 下载体的容器形态；`local` 表示本地目录拷贝，无容器。失败得早（下载失败）时为 None。
FetchKind = Literal["tar.gz", "tar", "gz", "tex", "pdf", "local"]


class FetchManifest(BaseModel):
    """fetch 的结构化结果，同时是它的 stage manifest。"""

    stage: Literal["fetch"] = "fetch"
    status: FetchStatus
    source: str = Field(description="arXiv 编号，或本地源码目录的绝对路径")
    kind: FetchKind | None = None
    url: str = ""
    payload_sha256: str = ""
    payload_bytes: int = 0
    files: dict[str, str] = Field(
        default_factory=dict, description="src/ 相对路径 → sha256，排序全量清单；下游输入 hash 的权威记录"
    )
    tex_files: list[str] = Field(default_factory=list)
    tex_chars: int = 0
    rejected: list[str] = Field(default_factory=list, description="解包时被安全策略拒绝的 tar 成员名")
    warnings: list[str] = Field(default_factory=list)
    message: str = ""
