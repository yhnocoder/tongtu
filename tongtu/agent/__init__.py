"""agent 运行时适配层（架构 §9）——零期 M3 填充，此处仅占位。

两个原语，运行时可插拔（首发 Codex CLI，见架构 §13）：

    complete(prompt, text, model) -> text
        # 无状态判断：逐块翻译、术语决策。可由纯 API 调用或 agent 运行时实现。

    session(prompt, workdir, model, budget) -> {done, transcript_path}
        # 有状态修复：修构建环境、修编译错、documentclass 适配。
        # 要求：headless 拉起、读写 workdir、执行命令、联网、可指定模型。

纪律：
- `session` 的 `done` 只表示会话结束，**裁决权在事后的校验脚本与编译**，永不信 agent 自述。
- 所有会话转录落 `logs/`——既是审计，也是促升规则的数据来源（report.json 的干预统计）。
- MockAgent（M2）：`complete` 恒等返回、`session` no-op——编译层 CI（恒等翻译 e2e）的钥匙。
- CodexAgent（M3，`codex.py`）：Codex CLI headless 拉起；`complete` 首发同走运行时。

运行时用 :func:`get_agent` 选：`get_agent("codex")` / `$TONGTU_AGENT` / 默认 `mock`。
默认是 mock 而不是真运行时——**花钱与拉起外部进程都必须是显式选择**。

六个关节：①主文件 ②构建环境 ③环境分类 ④通读与术语 ⑤翻译 ⑥适配与修复。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

#: 六个 agent 关节的稳定标识符，report.json 的干预记录与事件流共用这套命名。
JOINTS: tuple[str, ...] = (
    "main_file",      # ① flatten：主文件歧义 → 判定主文件
    "build_env",      # ② baseline：原文编译失败 → workdir 内修构建环境
    "env_classify",   # ③ mask：未知环境 → 散文/重环境分类
    "survey",         # ④ survey：全文通读 → brief + 术语预扫决策
    "translate",      # ⑤ translate：单块翻译
    "fixup",          # ⑥ compile：documentclass 适配与编译修复
)


@dataclass(frozen=True)
class SessionOutcome:
    """`session` 原语的返回值。

    `done` **只表示会话结束**（架构 §9），不是「修好了」——裁决权在事后的校验脚本与
    编译。驱动器拿到它之后一定会重新编译一次，故本对象里没有、也不该有任何成败字段。
    """

    done: bool = True
    transcript_path: Path | None = None
    message: str = ""

    def to_json(self) -> dict:
        data: dict = {"done": self.done}
        if self.transcript_path is not None:
            data["transcript_path"] = str(self.transcript_path)
        if self.message:
            data["message"] = self.message
        return data


class Complete(Protocol):
    """原语一：无状态判断（逐块翻译、术语决策）。

        complete(prompt, text, model) -> text
    """

    def __call__(self, prompt: str, text: str, model: str | None = None) -> str: ...


class Session(Protocol):
    """原语二：有状态修复（修构建环境、修编译错、documentclass 适配）。

        session(prompt, workdir, model, budget) -> {done, transcript_path}

    实现须能 headless 拉起、读写 `workdir`、执行命令、联网、指定模型。
    `tongtu.compiler.SessionFn`（关节 ②/⑥ 的 `FixupRequest` 形状）是它在编译回环里的
    包装形态，适配层用 `as_session_fn()` 一类的适配器把两者接起来。
    """

    def __call__(
        self,
        prompt: str,
        workdir: str | os.PathLike[str] | None = None,
        model: str | None = None,
        budget: int | None = None,
    ) -> SessionOutcome: ...


# ------------------------------------------------------------------ 运行时选择

#: 运行时名 → 构造器（惰性 import：选了 mock 就不该把 codex 那套 subprocess 逻辑拖进来）。
AGENTS: dict[str, Callable[..., object]] = {}

#: 默认运行时。**真运行时是显式选择**——默认 mock 意味着「没人明说就不花钱、不动外部进程」。
DEFAULT_AGENT = "mock"

#: 覆盖默认值的环境变量（优先级低于显式参数）。
AGENT_ENV = "TONGTU_AGENT"


def _mock_agent(**kwargs) -> object:
    from .mock import MockAgent

    return MockAgent(**kwargs)


def _codex_agent(**kwargs) -> object:
    from .codex import CodexAgent

    return CodexAgent(**kwargs)


AGENTS["mock"] = _mock_agent
AGENTS["codex"] = _codex_agent


def agent_names() -> tuple[str, ...]:
    """可选的运行时名（CLI 的 `--agent` choices 用它，不另抄一份）。"""
    return tuple(sorted(AGENTS))


def resolve_agent_name(
    name: str | None = None, env: os._Environ[str] | dict[str, str] | None = None
) -> str:
    """名字解析：显式参数 → `$TONGTU_AGENT` → :data:`DEFAULT_AGENT`。"""
    chosen = (name or "").strip()
    if not chosen:
        environ = os.environ if env is None else env
        chosen = (environ.get(AGENT_ENV) or "").strip()
    return chosen or DEFAULT_AGENT


def get_agent(
    name: str | None = None,
    *,
    env: os._Environ[str] | dict[str, str] | None = None,
    **kwargs,
) -> object:
    """按名字造一个 agent 运行时（两原语的实现），关键字参数直通构造器。

        get_agent()          -> MockAgent（默认，零成本）
        get_agent("codex")   -> CodexAgent（架构 §13 选型：首发 Codex CLI）

    名字未知时抛 `ValueError`（用法错误，不是运行期故障——CLI 会把它变成退出码 2）。
    """
    chosen = resolve_agent_name(name, env)
    factory = AGENTS.get(chosen)
    if factory is None:
        raise ValueError(
            f"未知的 agent 运行时：{chosen!r}（可选 {'/'.join(agent_names())}；"
            f"也可用 ${AGENT_ENV} 指定）"
        )
    return factory(**kwargs)


__all__ = [
    "AGENTS",
    "AGENT_ENV",
    "DEFAULT_AGENT",
    "JOINTS",
    "Complete",
    "Session",
    "SessionOutcome",
    "agent_names",
    "get_agent",
    "resolve_agent_name",
]
