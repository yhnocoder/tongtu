"""MockAgent / PseudoAgent：`complete` 恒等（或中文注入）返回、`session` no-op（架构 §9 末、§12 层 2）。

**编译层 CI 靠它成立**：恒等翻译 e2e 让 MockAgent 原样返回源文，三篇 fixture 论文全
流水线跑到底，零 LLM 成本覆盖掩码 / 注入 / 编译回环 / 导出全链路（架构 §12 层 2）。

两个原语的签名与 `tongtu.agent` 的 :class:`~tongtu.agent.Complete` /
:class:`~tongtu.agent.Session` 协议一致：

    complete(prompt, text, model) -> text        # 恒等：返回 `text` 本身
    session(prompt, workdir, model, budget)      # no-op：done=True，什么也不改

`session` 返回 `done=True` 不构成任何「修好了」的断言——架构 §9 明说 `done` 只表示会话
结束，裁决权在事后的编译。对 MockAgent 而言这条纪律恰好是**可测的**：它什么也没改，
驱动器重新编译必然还是同一个结果。

## 为什么恒等翻译能当校验

恒等译文对 :mod:`tongtu.validate` 的四层校验天然全绿（占位符、控制序列、括号、段落数
逐项相等），于是 translate 的内环重试路径不会被触发，e2e 里跑出来的 validate 失败一定
是流水线自己的 bug 而不是「模型没翻好」。

## 中文路径

恒等译文不含中文，xeCJK 断行、字体与断行 locale 等中文路径盖不到（架构 §12 层 2 的注、
附录 B 开放问题 2）。:class:`MockAgent` 因此留了 `transform` 钩子：给一个 `text -> text`
的纯函数即可得到**伪翻译（pseudo-translation）变体**（该词出自架构附录 B 开放问题 2），
仍旧零 LLM、零随机。开放问题 2 就此落定为这个变体而非另造一篇中文 fixture：
:func:`pseudo_translate` 是那个纯函数，
:class:`PseudoAgent` 是它的成品封装（`get_agent("pseudo")`），e2e 里与恒等变体同参数化跑
（`tests/test_e2e_pseudo.py`），真 TeX 形态即中文路径的覆盖点。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import SessionOutcome

__all__ = [
    "CompleteCall",
    "MockAgent",
    "PSEUDO_PREFIX",
    "PseudoAgent",
    "SessionCall",
    "identity",
    "pseudo_translate",
]

#: MockAgent 的默认模型标识——进翻译缓存 key（架构 §4），使 mock 与真模型的缓存互不混用。
MODEL = "mock"

#: PseudoAgent 的模型标识：与 `mock` 不同，两个变体的翻译记忆互不命中（架构 §4）。
PSEUDO_MODEL = "pseudo"


def identity(text: str) -> str:
    """恒等变换：`complete` 的默认 `transform`。"""
    return text


# --------------------------------------------------------------------------- #
# 中文注入变体：每段前缀一句固定中文
# --------------------------------------------------------------------------- #

#: 注入的固定中文句。只含汉字与全角句号——不含 `\` `{` `}` `$` `⟦` `⟧`，故
#: :mod:`tongtu.validate` 的四层（占位符 / 控制序列 / 括号与行内数学 / 段落数）一层也不动。
PSEUDO_PREFIX = "以下是本段中文译文。"

#: 段落分隔，与 `validate.PARAGRAPH_SEP_RE` 同规则；捕获组使 `split` 保留分隔本身，
#: 于是变换**只往段落里塞字符**，段落数与分隔符逐字节原样。
_PARAGRAPH_SPLIT_RE = re.compile(r"(\n\s*\n)")

#: 结构行的首字符：命令（`\`）、掩码占位符（`⟦`，含 `⟦CAP-k⟧` 行）、注释（`%`）、
#: 对齐符（`&`）。**以它们开头的段落一律跳过**——中文塞在 `\documentclass` 之前
#: （前导区就是 `⟦BLK-0⟧` 这一段）或 `\begin{itemize}` 与首个 `\item` 之间是真编译错，
#: 而本变体的用途是让编译回环跑中文，不是给它造错。代价是 `\begin{abstract}` 这类整段
#: 被环境包住的散文也一并跳过：中文覆盖略窄（三篇 fixture 各 9–15 段），换的是**绝不造错**。
_STRUCTURAL_STARTS = "\\⟦%&"


def pseudo_translate(text: str) -> str:
    """中文注入：给每个**散文段**前缀一句固定中文，其余逐字节原样。

    确定性、零随机、零 LLM 的纯函数；删掉全部 :data:`PSEUDO_PREFIX` 即得回原文（e2e 拿
    这条当字节级断言）。**不幂等**——连跑两次就是两句前缀，故只在 agent 边界上跑一次。

        >>> pseudo_translate("Hello ⟦BLK-1⟧\\n\\n\\\\section{Hi}")
        '以下是本段中文译文。Hello ⟦BLK-1⟧\\n\\n\\\\section{Hi}'
    """
    parts = _PARAGRAPH_SPLIT_RE.split(text)
    return "".join(part if index % 2 else _prefix_paragraph(part) for index, part in enumerate(parts))


def _prefix_paragraph(paragraph: str) -> str:
    """给一段加前缀；空段与结构行开头的段原样返回。缩进保留在前缀之前。"""
    body = paragraph.lstrip()
    if not body or body[0] in _STRUCTURAL_STARTS:
        return paragraph
    indent = paragraph[: len(paragraph) - len(body)]
    return indent + PSEUDO_PREFIX + body


@dataclass(frozen=True)
class CompleteCall:
    """一次 `complete` 调用的记录（测试断言与转录归档用）。"""

    prompt: str
    text: str
    model: str | None = None


@dataclass(frozen=True)
class SessionCall:
    """一次 `session` 调用的记录。"""

    prompt: str
    workdir: str | None = None
    model: str | None = None
    budget: int | None = None


@dataclass
class MockAgent:
    """恒等 / no-op 的 agent 运行时实现。

    :param model: 自报的模型标识，进缓存 key。
    :param transform: `complete` 的文本变换，默认恒等；换成 :func:`pseudo_translate`
        即得中文注入变体（成品见 :class:`PseudoAgent`）。
    :param transcript_dir: 给出时把 `session` 的提示词落成转录文件（架构 §9：转录一律
        落 `logs/`）。默认不写盘——e2e 里没有真会话可转录。
    """

    model: str = MODEL
    transform: Callable[[str], str] = identity
    transcript_dir: Path | None = None
    completions: list[CompleteCall] = field(default_factory=list)
    sessions: list[SessionCall] = field(default_factory=list)

    # -- 原语 ① complete ----------------------------------------------------

    def complete(self, prompt: str, text: str, model: str | None = None) -> str:
        """恒等返回 `text`（`transform` 非默认时按它变换）。`prompt` 只被记录。"""
        self.completions.append(CompleteCall(prompt=prompt, text=text, model=model))
        return self.transform(text)

    # -- 原语 ② session -----------------------------------------------------

    def session(
        self,
        prompt: str,
        workdir: str | os.PathLike[str] | None = None,
        model: str | None = None,
        budget: int | None = None,
    ) -> SessionOutcome:
        """no-op：不读不写不执行，直接 `done=True`。裁决仍在调用方的重新编译。"""
        self.sessions.append(
            SessionCall(
                prompt=prompt,
                workdir=None if workdir is None else str(workdir),
                model=model,
                budget=budget,
            )
        )
        return SessionOutcome(
            done=True,
            transcript_path=self._write_transcript(prompt),
            message="MockAgent：no-op 会话，什么也没改",
        )

    def _write_transcript(self, prompt: str) -> Path | None:
        if self.transcript_dir is None:
            return None
        try:
            self.transcript_dir.mkdir(parents=True, exist_ok=True)
            path = self.transcript_dir / f"mock-session-{len(self.sessions):03d}.txt"
            path.write_text(prompt, encoding="utf-8")
            return path
        except OSError:  # 转录写不下去不该拖垮流水线
            return None

    # -- 适配到编译回环的 SessionFn 形状 -------------------------------------

    def as_session_fn(self) -> Callable[[object], SessionOutcome]:
        """适配成 :data:`tongtu.compiler.SessionFn`（关节 ②/⑥ 的 `FixupRequest` 形状）。

        编译回环递进来的是一个 `FixupRequest`（自带现成 prompt 与 workdir），这里把它
        拆成两原语的参数。返回值不作裁决依据，驱动器只认调用之后的重新编译。
        """

        def run(request: object) -> SessionOutcome:
            workdir = getattr(request, "workdir", None)
            return self.session(
                getattr(request, "prompt", ""),
                workdir=getattr(workdir, "path", workdir),
                model=self.model,
            )

        return run


@dataclass
class PseudoAgent(MockAgent):
    """pseudo-translation 变体：`complete` 走 :func:`pseudo_translate`，`session` 仍是 no-op。

    与 :class:`MockAgent` 的唯一差别是那个 `transform`（以及随之不同的模型标识）。关节 ④
    （survey）拿到的同样不是 JSON，故 survey 照旧走确定性降级骨架——变体只改译文里有没有
    中文，不改任何流水线分支。
    """

    model: str = PSEUDO_MODEL
    transform: Callable[[str], str] = pseudo_translate
