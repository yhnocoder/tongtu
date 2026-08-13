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

六个关节：①主文件 ②构建环境 ③环境分类 ④通读与术语 ⑤翻译 ⑥适配与修复。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


__all__ = ["JOINTS", "Complete", "Session", "SessionOutcome"]
