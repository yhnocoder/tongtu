"""report.json —— 运行报告（export 阶段产出，架构 §7）。

回退 chunk、校验统计、编译出口检查、agent 介入点干预记录与契约版本号。干预统计是
固化规则（架构 §2 原则 3）的数据来源——复发问题据此固化为确定性代码 / 分类表 /
适配表条目，一次性问题只记在此不进编排器。inspection page 的侧栏直接消费本文件。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from .base import CONTRACT_VERSION, ArtifactModel, ContractVersion, FallbackReason

#: 六个 agent 介入点：①主文件判定 ②修 toolchain ③环境分类 ④读全文与术语 ⑤翻译 ⑥编译修复。
AgentHook = Literal["main_file", "toolchain", "env_classify", "survey", "translate", "compile_fix"]

#: 适配层原语（架构 §3 agent 适配层节）。
AgentPrimitive = Literal["ask", "work"]

#: work 会话的终止原因（WorkOutcome.stop_reason）。只说明会话如何终止，不表示修好了。
StopReason = Literal["finished", "budget_exhausted", "timeout", "error"]

#: 阶段执行结果。cached = 按 manifest 判定输入未变、整段跳过。
StageStatus = Literal["ok", "cached", "skipped", "failed"]

#: 总体结果。ok_with_fallback = 出包但有回退 chunk（退出码仍为 0）；failed = 未能出包。
RunStatus = Literal["ok", "ok_with_fallback", "failed"]


class PaperInfo(ArtifactModel):
    """论文标识。"""

    arxiv_id: str | None = Field(
        default=None, description="arXiv id；本地目录来源时为空（与 --json 事件流同一口径），来源记在 source。"
    )
    title: str | None = Field(default=None, description="原文标题。")
    source: str | None = Field(default=None, description="源码来源：arxiv e-print 或本地目录路径。")


class StageRecord(ArtifactModel):
    """单阶段执行记录（增量构建：输入未变则 cached）。"""

    name: str = Field(description="阶段名，见 tongtu.stages.STAGES。")
    status: StageStatus
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    message: str | None = None


class ValidationFailures(ArtifactModel):
    """各校验层的失败计数，字段与 §3 translate 节 validate 四层一一对应。"""

    placeholders: Annotated[int, Field(ge=0)] = 0
    control_sequences: Annotated[int, Field(ge=0)] = 0
    braces_and_math: Annotated[int, Field(ge=0)] = 0
    paragraph_count: Annotated[int, Field(ge=0)] = 0


class ValidationStats(ArtifactModel):
    """机械校验统计。"""

    chunks_total: Annotated[int, Field(ge=0)]
    translated: Annotated[int, Field(ge=0)]
    cached: Annotated[int, Field(ge=0)] = Field(description="缓存命中、未重新调用模型的 chunk 数。")
    fallback: Annotated[int, Field(ge=0)]
    failures_by_check: ValidationFailures = Field(
        default_factory=ValidationFailures,
        description="出口终审 validate 的失败计数（会话内 agent 自查不计入——脚本看不见）。",
    )
    mask_roundtrip_ok: bool = Field(description="mask 往返自检结果（unmask(mask(x)) == x）；false 时流水线不放行。")


class FallbackRecord(ArtifactModel):
    """回退清单条目：保底用原文的 chunk 与段落（保证总能产出 PDF）。"""

    chunk_id: str
    paragraphs: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list, description="回退的段落序号；空表示整个 chunk 回退。"
    )
    reason: FallbackReason
    detail: str | None = None
    section: str | None = Field(default=None, description="所在章节，便于人工复核定位。")


class LogCountDelta(ArtifactModel):
    """某类日志条目在 baseline 与 zh 两次编译中的数量，出口取相对 baseline 的增量（zh - baseline）。"""

    baseline: Annotated[int, Field(ge=0)]
    zh: Annotated[int, Field(ge=0)]


class CompileLogChecks(ArtifactModel):
    """编译日志扫描结果（架构 §3 compile 节，决策 A.20）。

    cjk_missing_glyphs 是硬判据：大于 0 即 compile 失败——它只有 font fallback chain
    没接上一个原因，而页数判据看不见它。其余三项取相对 baseline 的增量进 warning，
    不设硬性阈值——「多难看才算坏」没有机械答案，这部分靠会话内 agent 看渲染页。
    """

    cjk_missing_glyphs: Annotated[int, Field(ge=0)] = Field(
        description="zh 编译日志中落在 CJK 区段（含全角标点）的 Missing character 行数。"
    )
    missing_glyphs: LogCountDelta | None = Field(default=None, description="非 CJK 的 missing glyph。")
    overfull_hboxes: LogCountDelta | None = Field(default=None, description="Overfull \\hbox 行数。")
    undefined_references: LogCountDelta | None = Field(default=None, description="未定义引用数。")


class CompileWarning(ArtifactModel):
    """编译警告（overfull hbox、缺字、未定义引用等），inspection page 侧栏逐条展示。"""

    kind: str = Field(description="如 overfull_hbox / missing_character / undefined_reference。")
    message: str
    page: Annotated[int, Field(ge=1)] | None = None
    count: Annotated[int, Field(ge=1)] = Field(default=1, description="同类警告合并计数。")


class InjectSummary(ArtifactModel):
    """xeCJK 注入与 documentclass 适配的结构化摘要（决策 A.13：assemble 已并入 compile）。

    branch 说明走了哪条注入分支，adaptations 是命中的适配表条目——固化规则据此判断某类
    documentclass 是否该沉淀进适配表。
    """

    branch: str | None = Field(default=None, description="注入分支：inject / already_cjk / no_documentclass 等。")
    changed: bool | None = Field(default=None, description="是否实际改动了源码。")
    documentclass: str | None = Field(default=None, description="识别出的 documentclass。")
    position: str | None = Field(
        default=None,
        description="注入位置名（after_documentclass / before_begin_document / after_package …，"
        "见 tongtu/data/documentclass.json 的 positions 段）。",
    )
    adaptations: list[str] = Field(default_factory=list, description="命中的 documentclass 适配表条目。")
    removed_packages: list[str] = Field(default_factory=list, description="为让 xeCJK 生效而删掉的宏包（如 CJKutf8）。")
    stripped_environments: list[str] = Field(
        default_factory=list, description="被剥掉的环境（如原文自带的 CJK 环境）。"
    )
    warnings: list[str] = Field(default_factory=list)


class CompileResult(ArtifactModel):
    """编译出口结果（裁决者，agent 自述无效力）。

    出口判据（架构 §3 compile 节）：zh.pdf 存在、非空、页数与 baseline 相当、日志无
    CJK missing glyph。
    """

    passed: bool
    compile_runs: Annotated[int, Field(ge=0)] | None = Field(
        default=None, description="修复会话内 tex compile 的执行次数（编译次数上限约束的对象）。"
    )
    baseline_passed: bool | None = Field(
        default=None, description="原文 baseline 编译是否通过（toolchain 问题的隔离判据）。"
    )
    pages: LogCountDelta | None = Field(default=None, description="baseline 与 zh 的页数，「页数相当」判据的记录。")
    log_checks: CompileLogChecks | None = None
    warnings: list[CompileWarning] = Field(default_factory=list)
    log_path: str | None = Field(default=None, description="编译日志相对路径（logs/ 内）。")
    inject: InjectSummary | None = None


class AgentIntervention(ArtifactModel):
    """一次 agent 介入点拉起的记录——固化规则的判据来源。trace 一律落 logs/。"""

    hook: AgentHook
    stage: str | None = Field(default=None, description="触发所在阶段。")
    primitive: AgentPrimitive
    trigger: str | None = Field(
        default=None, description="触发条件，如「未知环境 theorem*」「documentclass 未在适配表中」。"
    )
    outcome: Literal["resolved", "unresolved", "fallback"] = Field(
        description="由事后的校验脚本与编译裁决，不是 agent 自述。"
    )
    stop_reason: StopReason | None = Field(default=None, description="work 专有：会话的终止原因（WorkOutcome）。")
    detail: str | None = Field(
        default=None, description="stop_reason 为 error 时的运行时错误现场（WorkOutcome.detail）。"
    )
    action: str | None = Field(default=None, description="agent 实际做了什么（一句话）。")
    model_id: str | None = None
    prompt_version: str | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    trace_path: str | None = Field(
        default=None,
        description="logs/ 内的 trace 路径。work 的内容是 start-state hash + command sequence + "
        "end-state hash（架构 §3 compile 节）；ask 是提示词与返回文本。",
    )
    promotable: bool | None = Field(
        default=None, description="人工标记：该干预是否值得固化为确定性代码 / 分类表 / 适配表条目。"
    )


class HookUsage(ArtifactModel):
    """单个介入点的 agent 用量合计，由运行时对象按 hook 累计、export 组装时读一次（架构 §3 agent 适配层节）。

    逐 hook 分解是固化优先级的直接判据：哪个介入点花费最多，最值得固化为确定性代码。
    全局总计由逐条求和得出，不另存。
    """

    hook: AgentHook
    calls: Annotated[int, Field(ge=0)] = Field(description="该介入点的拉起次数。")
    turns: Annotated[int, Field(ge=0)] | None = Field(
        default=None, description="work 会话轮数合计；ask 无轮数概念，为空。"
    )
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None


class ArtifactEntry(ArtifactModel):
    """artifact package 文件清单条目及其自校验结果。"""

    path: str
    bytes: Annotated[int, Field(ge=0)] | None = None
    valid: bool | None = Field(
        default=None, description="JSON artifact 经 artifact model 校验的结果；非 JSON artifact 为空。"
    )


class ReportArtifact(ArtifactModel):
    """report.json 全文件。"""

    contract_version: ContractVersion = CONTRACT_VERSION
    tongtu_version: str | None = Field(default=None, description="产生本包的通途版本。")
    paper: PaperInfo
    status: RunStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stages: list[StageRecord] = Field(default_factory=list)
    validation: ValidationStats
    fallbacks: list[FallbackRecord] = Field(default_factory=list)
    compile: CompileResult
    agent_interventions: list[AgentIntervention] = Field(default_factory=list)
    agent_usage: list[HookUsage] = Field(
        default_factory=list, description="逐介入点的用量合计，只列实际拉起过的 hook。"
    )
    artifacts: list[ArtifactEntry] = Field(default_factory=list)
