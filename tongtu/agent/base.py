"""agent 适配层的接口类型：两个原语（`ask` 与 `work`）的返回值与预算。

`ask(prompt, text, model, schema, log_path, effort) -> AskOutcome` 是流水线做单次问答的唯一入口，
`work(prompt, workdir, model, budget, trace_path) -> WorkOutcome` 是拉起多轮会话的唯一
入口。各适配器（如 `opencode`、`claude_code`）实现它们，本模块只定义两端的数据类型，
不含任何传输细节。

`AskOutcome.status` 取两个值之一：拿到返回正文 `ok`、失败 `error`（密钥缺失、请求失败、
响应里解析不出正文、日志写不出）。失败以值返回而不抛异常，错误现场写进 `detail`。

`WorkOutcome.stop_reason` 取四个值之一：会话正常结束 `finished`、轮数预算耗尽
`budget_exhausted`、墙钟超时 `timeout`、运行时报错或无法拉起 `error`。运行时自己的终止
原因由各适配器映射到这四个值，映射不上的归 `error`，错误现场写进 `detail`。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 单次问答拿到了返回正文。
ASK_STATUS_OK = "ok"

#: 单次问答失败：密钥缺失、请求失败、响应里解析不出正文，或日志写不出。
ASK_STATUS_ERROR = "error"


@dataclass(frozen=True)
class AskOutcome:
    """一次单次问答的结局。

    `text` 在 `status` 是 `ok` 时为模型返回的正文（请求给出 schema 时是符合该 schema 的
    JSON 字符串），失败时为空。`detail` 仅在 `status` 是 `error` 时非空，记录失败现场供
    调用方写进 manifest。正文是否可用的终审在调用方：解析与校验不过怎么处置由各阶段自己定。
    """

    status: str
    text: str = ""
    detail: str = ""


#: 会话正常结束（运行时自己判定任务做完并退出）。
STOP_REASON_FINISHED = "finished"

#: 会话用满了 `WorkBudget.max_turns` 的轮数上限，被运行时终止。
STOP_REASON_BUDGET_EXHAUSTED = "budget_exhausted"

#: 会话超过 `WorkBudget.timeout_seconds` 的墙钟上限，被适配器终止。
STOP_REASON_TIMEOUT = "timeout"

#: 运行时报错、拉不起来，或终止原因映射不到上面三个值。
STOP_REASON_ERROR = "error"


@dataclass(frozen=True)
class WorkOutcome:
    """一次会话的结局。

    `stop_reason` 只说明会话如何终止，不表示 agent 把事情做对了：正确性由调用方在会话结束
    之后自己校验（precompile 的做法是清理编译产物再编译一遍）。`detail` 仅在
    `stop_reason` 是 `error` 时非空，记录运行时的错误现场供排查。
    """

    stop_reason: str
    detail: str = ""


@dataclass(frozen=True)
class WorkBudget:
    """一次会话的上限：轮数与墙钟秒数。

    两个值由调用方按介入点给出，超限即终止会话；终止后的处置（重试、判失败、照常复验）
    同样由调用方决定，适配层只负责终止并如实报告 `stop_reason`。
    """

    max_turns: int
    timeout_seconds: float
