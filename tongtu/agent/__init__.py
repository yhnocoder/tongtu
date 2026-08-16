"""agent 适配层：流水线拉起 agent 会话只经这层接口，运行时可替换。

接口类型在 `base`（`WorkOutcome`、`WorkBudget` 与四个 `stop_reason` 取值），运行时适配器
一个模块一个，首发运行时是 Claude Code CLI（`claude_code`）。调用方按运行时名字引用适配器
模块本身（`from ..agent import claude_code` 后调 `claude_code.work(...)`），本模块转出
`work` 只是给当前唯一运行时一个包级入口。
"""

from __future__ import annotations

from .base import (
    STOP_REASON_BUDGET_EXHAUSTED,
    STOP_REASON_ERROR,
    STOP_REASON_FINISHED,
    STOP_REASON_TIMEOUT,
    WorkBudget,
    WorkOutcome,
)
from .claude_code import work

__all__ = [
    "STOP_REASON_BUDGET_EXHAUSTED",
    "STOP_REASON_ERROR",
    "STOP_REASON_FINISHED",
    "STOP_REASON_TIMEOUT",
    "WorkBudget",
    "WorkOutcome",
    "work",
]
