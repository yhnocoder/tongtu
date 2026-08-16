"""precompile 阶段的 stage manifest（`build/manifests/precompile.json`）的字段级权威定义。

`status` 是唯一分流依据；`flat_sha256` 与 `fetch_files_sha256` 是本阶段的两个输入 hash，
都从 flatten manifest 转录；`precompile_sha256` 是输出 hash，下游阶段判定「输入未变不重算」
时引用它；`pages` 与四类日志计数是原文在本机 toolchain 下的基线数据，compile 阶段对
`zh.pdf` 做增量比对时引用它们；`fix_session` 与三个 `session_` 字段记录修复会话是否拉起
以及它的结局，`changed_files` 是会话对 flat.tex 之外文件的改动清单。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class PrecompileStatus(StrEnum):
    """precompile 的结果状态，JSON 序列化为小写字符串值。

    `OK` 之外都不是异常，调用方按状态分流：`FLATTEN_MISSING` 与 `FLATTEN_NOT_OK` 是前置
    条件不满足（上游 flatten 没跑、产物不在，或它的结论不是 ok）；`COMPILE_FAILED` 覆盖
    latexmk 执行失败、编译超时、prompt 资产缺失、agent 运行时不可用，以及经修复会话后复验
    仍不过出口判据，各种情形由 message 区分。
    """

    OK = "ok"
    FLATTEN_MISSING = "flatten_missing"
    FLATTEN_NOT_OK = "flatten_not_ok"
    COMPILE_FAILED = "compile_failed"


class PrecompileManifest(BaseModel):
    """precompile 的结构化结果，同时是它的 stage manifest。"""

    stage: Literal["precompile"] = "precompile"
    status: PrecompileStatus
    flat_sha256: str = Field(
        default="", description="build/flat.tex 的 sha256，本阶段的输入 hash 之一，从 flatten manifest 转录"
    )
    fetch_files_sha256: str = Field(
        default="",
        description="fetch manifest 的 files 清单的规范化 hash，本阶段的输入 hash 之二，从 flatten manifest 转录",
    )
    precompile_sha256: str = Field(
        default="", description="build/precompile.tex 的 sha256，本阶段的输出 hash，下游输入判定的权威记录"
    )
    precompile_bytes: int = Field(default=0, description="build/precompile.tex 的字节数")
    flatten_status: str = Field(default="", description="本次读到的 flatten manifest 的状态，排查用")
    fetch_status: str = Field(default="", description="flatten manifest 记录的 fetch 状态，退出码映射与排查用")
    command: list[str] = Field(default_factory=list, description="终审那次实际执行的 latexmk 命令行")
    pages: int = Field(default=0, description="flat.log 解析出的页数，compile 出口判据的页数基准")
    pdf_bytes: int = Field(default=0, description="build/precompile/flat.pdf 的字节数")
    overfull_hboxes: int = Field(default=0, description=r"flat.log 里 Overfull \hbox 的行数")
    undefined_references: int = Field(default=0, description="flat.log 里未定义 reference 警告的行数")
    undefined_citations: int = Field(default=0, description="flat.log 里未定义 citation 警告的行数")
    missing_characters: int = Field(default=0, description="flat.log 里 Missing character 的行数")
    duration_seconds: float = Field(default=0.0, description="终审那次 latexmk 的执行耗时，排查与超时校准用")
    fix_session: bool = Field(default=False, description="本次执行是否拉起过 agent 修复会话")
    session_stop_reason: str = Field(
        default="", description="修复会话的终止原因（WorkOutcome.stop_reason 转录），未拉起时为空"
    )
    session_model: str = Field(default="", description="修复会话实际使用的模型标识，未拉起时为空")
    session_duration_seconds: float = Field(default=0.0, description="修复会话从拉起到返回的耗时")
    changed_files: list[str] = Field(
        default_factory=list,
        description="修复会话改动的 flat.tex 之外的文件，src/ 相对路径；这些改动不传播到下游阶段",
    )
    warnings: list[str] = Field(default_factory=list)
    message: str = ""
