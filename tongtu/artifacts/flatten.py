"""flatten 阶段的 stage manifest（`build/manifests/flatten.json`）的字段级权威定义。

`status` 是唯一分流依据；`flat_sha256` 与 `flat_bytes` 记录 `build/flat.tex` 的内容，
是下游阶段判定「输入未变不重算」时引用的权威记录；`fetch_files_sha256` 是本阶段的
输入 hash，由 fetch manifest 的 `files` 清单算出。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class FlattenStatus(StrEnum):
    """flatten 的结果状态，JSON 序列化为小写字符串值。

    `OK` 之外都不是异常，调用方按状态分流：`FETCH_MISSING` 与 `FETCH_NOT_OK` 是前置
    条件不满足（上游 fetch 没跑或结论不是 ok）；`MAIN_NOT_FOUND` 与 `MAIN_AMBIGUOUS`
    是主文件判定不出唯一结果；`EXPAND_FAILED` 是 latexpand 执行失败或输出不过出口检查。
    """

    OK = "ok"
    FETCH_MISSING = "fetch_missing"
    FETCH_NOT_OK = "fetch_not_ok"
    MAIN_NOT_FOUND = "main_not_found"
    MAIN_AMBIGUOUS = "main_ambiguous"
    EXPAND_FAILED = "expand_failed"


class FlattenManifest(BaseModel):
    """flatten 的结构化结果，同时是它的 stage manifest。"""

    stage: Literal["flatten"] = "flatten"
    status: FlattenStatus
    main_file: str = Field(default="", description="主文件的 src/ 相对路径；未判定出主文件时为空")
    candidates: list[str] = Field(
        default_factory=list,
        description=r"主文件候选：内容在注释外含 \documentclass 或 \documentstyle 的文件，src/ 相对路径",
    )
    fetch_files_sha256: str = Field(
        default="", description="fetch manifest 的 files 清单的规范化 hash，本阶段的输入 hash"
    )
    fetch_status: str = Field(default="", description="本次读到的 fetch manifest 的状态，退出码映射与排查用")
    bbl_file: str = Field(default="", description="内联进 flat.tex 的 .bbl 的 src/ 相对路径；未内联时为空")
    flat_sha256: str = Field(default="", description="build/flat.tex 的 sha256")
    flat_bytes: int = Field(default=0, description="build/flat.tex 的字节数")
    command: list[str] = Field(default_factory=list, description="实际执行的 latexpand 命令行")
    warnings: list[str] = Field(default_factory=list)
    message: str = ""
