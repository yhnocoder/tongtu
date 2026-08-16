"""Claude Code CLI 的 `work` 适配器：headless 拉起一次多轮会话。

会话以 `claude -p …` 的形式拉起，prompt 经 stdin 送入——prompt 可能以 `-` 开头（如
SKILL.md 的 frontmatter 分隔线），作为命令行参数会被当成选项解析，长度也受 ARG_MAX 限制。
cwd 设为调用方给出的 workdir——agent 能读写的就是这棵目录树，别的什么都不给。stdout 是
stream-json 事件流，原样落 `trace_path`，一行一个 JSON 事件（JSON Lines）；stderr 捕获在
内存里，只在报错时摘进 `WorkOutcome.detail`。

会话沿用本机 Claude Code 的登录态与全局配置（hook、插件、CLAUDE.md 自动发现都照常生效）：
本模块不做配置隔离，因为隔离会连登录凭证一起隔掉——维护者用订阅登录，隔离后运行时取不到
凭证。会话现场的约束由三样承担：cwd 圈定可读写的目录树、`--allowedTools` 限定工具集、
prompt 资产给出规则。

终止原因的映射：进程退出码 0 → `finished`；墙钟超过 `budget.timeout_seconds` → 杀进程组
并归 `timeout`；退出码非 0 时读 transcript 最后一个 `result` 事件，subtype 是
`error_max_turns` → `budget_exhausted`，其余（含 transcript 解析不出结果）→ `error`。
`claude` 不在 PATH 或拉不起来同样归 `error`，detail 说明运行时不可用。

**`stop_reason` 不表示 agent 把事情做对了**，只说明会话如何终止。正确性的终审在调用方：
precompile 的做法是会话结束后自己清理编译产物再编译一遍，agent 在会话内的自述不作数。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..processes import OUTPUT_EXCERPT_CHARS, run_in_process_group
from .base import (
    STOP_REASON_BUDGET_EXHAUSTED,
    STOP_REASON_ERROR,
    STOP_REASON_FINISHED,
    STOP_REASON_TIMEOUT,
    WorkBudget,
    WorkOutcome,
)

#: 运行时的可执行文件名（Claude Code CLI，需自行安装并在 PATH 里）。
CLAUDE_EXECUTABLE = "claude"

#: `model` 参数为 None 时使用的模型标识。
DEFAULT_MODEL = "claude-sonnet-5"

#: 会话的 reasoning effort 档位，固定值，不随介入点变化。
EFFORT_LEVEL = "xhigh"

#: 输出格式：事件流一行一个 JSON 对象，便于原样落盘成 transcript 并事后解析。
#: stream-json 在 headless 模式下要求同时给 --verbose。
OUTPUT_FORMAT = "stream-json"

#: 会话可用的工具：读写树内文件加执行命令，够修一棵编译树。
ALLOWED_TOOLS = "Read,Edit,Write,Glob,Grep,Bash"

#: 权限模式：文件编辑自动放行，headless 会话不停在权限提示上。
PERMISSION_MODE = "acceptEdits"

#: transcript 里表示会话结束的事件类型，与轮数耗尽对应的 subtype。
RESULT_EVENT_TYPE = "result"
MAX_TURNS_SUBTYPE = "error_max_turns"


def build_command(model: str, budget: WorkBudget) -> list[str]:
    """拼出 headless 会话的命令行；prompt 不在其中，由 `work` 经 stdin 送入。
    模型标识由调用方定好（None 的替换在 `work` 里做）。"""
    return [
        CLAUDE_EXECUTABLE,
        "-p",
        "--model",
        model,
        "--effort",
        EFFORT_LEVEL,
        "--max-turns",
        str(budget.max_turns),
        "--output-format",
        OUTPUT_FORMAT,
        "--verbose",
        "--allowedTools",
        ALLOWED_TOOLS,
        "--permission-mode",
        PERMISSION_MODE,
    ]


def work(
    prompt: str,
    workdir: Path,
    model: str | None,
    budget: WorkBudget,
    trace_path: Path,
) -> WorkOutcome:
    """在 `workdir` 里拉起一次 Claude Code 会话，transcript 写 `trace_path`，返回终止原因。

    `model` 为 None 时用 `DEFAULT_MODEL`。会话经 `processes` 执行：agent 会派生子进程
    （编译、脚本），超时要按进程组终止，不留后台进程继续写 workdir。prompt 写进 stdin 后
    管道即关闭，运行时不会停下来等输入。

    返回的 `stop_reason` 只说明会话如何终止，不表示 agent 把事情做对了；结果的终审由调用方
    自己跑校验或编译做出。
    """
    command = build_command(model or DEFAULT_MODEL, budget)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # stdout 直接重定向到 transcript 文件：事件流由运行时逐行写入，父进程只需读 stderr
        # 一个管道，不存在两个管道交替读取的阻塞问题。
        with trace_path.open("wb") as trace_file:
            outcome = run_in_process_group(
                command,
                workdir,
                budget.timeout_seconds,
                stdout=trace_file,
                input_bytes=prompt.encode("utf-8"),
            )
    except OSError as error:
        return WorkOutcome(
            stop_reason=STOP_REASON_ERROR,
            detail=(
                f"拉起 {CLAUDE_EXECUTABLE} 失败（{type(error).__name__}：{error}）。"
                f"确认已安装 Claude Code CLI、{CLAUDE_EXECUTABLE} 在 PATH 里，且 {trace_path.parent} 可写。"
            ),
        )

    if outcome.timed_out:
        return WorkOutcome(stop_reason=STOP_REASON_TIMEOUT)
    if outcome.returncode == 0:
        return WorkOutcome(stop_reason=STOP_REASON_FINISHED)
    return _classify_failure(outcome.returncode, outcome.stderr_text, trace_path)


def _classify_failure(returncode: int, stderr: str, trace_path: Path) -> WorkOutcome:
    """退出码非 0 时按 transcript 的 result 事件分流：轮数耗尽与其他错误。"""
    result_event = _last_result_event(trace_path)
    if result_event is not None and result_event.get("subtype") == MAX_TURNS_SUBTYPE:
        return WorkOutcome(stop_reason=STOP_REASON_BUDGET_EXHAUSTED)
    parts = [f"{CLAUDE_EXECUTABLE} 退出码 {returncode}"]
    if result_event is not None:
        subtype = str(result_event.get("subtype", ""))
        text = str(result_event.get("result", "")).strip()[:OUTPUT_EXCERPT_CHARS]
        parts.append(f"transcript 的 result 事件 subtype={subtype or '（无）'}，result={text or '（空）'}")
    else:
        parts.append(f"transcript 里没有可解析的 result 事件（{trace_path}）")
    if stderr.strip():
        parts.append(f"stderr：{stderr.strip()[:OUTPUT_EXCERPT_CHARS]}")
    return WorkOutcome(stop_reason=STOP_REASON_ERROR, detail="；".join(parts))


def _last_result_event(trace_path: Path) -> dict[str, object] | None:
    """从 transcript 末尾往前找第一个 result 事件；读不到或没有返回 None。

    事件流一行一个 JSON 对象，超时被杀时末行可能是半行，故逐行解析失败即跳过。
    """
    try:
        lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == RESULT_EVENT_TYPE:
            return event
    return None
