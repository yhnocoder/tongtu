"""agent 适配层的接口类型：会话原语 `work` 的返回值与预算。

`work(prompt, workdir, model, budget, trace_path) -> WorkOutcome` 是流水线拉起多轮会话的
唯一入口，各运行时的适配器（如 `claude_code`）实现它，本模块只定义两端的数据类型，不含
任何运行时细节。

`WorkOutcome.stop_reason` 取四个值之一：会话正常结束 `finished`、轮数预算耗尽
`budget_exhausted`、墙钟超时 `timeout`、运行时报错或无法拉起 `error`。运行时自己的终止
原因由各适配器映射到这四个值，映射不上的归 `error`，错误现场写进 `detail`。
"""

from __future__ import annotations

from dataclasses import dataclass

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
