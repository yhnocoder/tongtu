"""agent 适配层：流水线调用模型只经这层接口，两个原语各自可替换。

接口类型在 `base`（`AskOutcome`、`WorkOutcome`、`WorkBudget` 与各自的状态取值）。两个
原语分属两种传输：单次问答 `ask` 走 API 直调（适配器 `opencode`），多轮会话 `work`
走 agent CLI 运行时（适配器 `claude_code`）。调用方按适配器名字引用模块本身（如
`from ..agent import opencode` 后调 `opencode.ask(...)`），本模块转出 `ask` 与 `work`
只是给当前默认适配器一个包级入口。
"""

from __future__ import annotations

from .base import (
    ASK_STATUS_ERROR,
    ASK_STATUS_OK,
    STOP_REASON_BUDGET_EXHAUSTED,
    STOP_REASON_ERROR,
    STOP_REASON_FINISHED,
    STOP_REASON_TIMEOUT,
    AskOutcome,
    WorkBudget,
    WorkOutcome,
)
from .claude_code import work
from .opencode import ask

__all__ = [
    "ASK_STATUS_ERROR",
    "ASK_STATUS_OK",
    "STOP_REASON_BUDGET_EXHAUSTED",
    "STOP_REASON_ERROR",
    "STOP_REASON_FINISHED",
    "STOP_REASON_TIMEOUT",
    "AskOutcome",
    "WorkBudget",
    "WorkOutcome",
    "ask",
    "work",
]
