"""Codex CLI 适配器（架构 §9 两原语、§13 选型「首发 Codex CLI 试水」）。

    complete(prompt, text, model) -> text                  一次性无状态调用（read-only 沙箱）
    session(prompt, workdir, model, budget) -> SessionOutcome   有状态修复（workspace-write）

首发**两个原语同走运行时**（架构 §13：纯 API 直调为后续优化），故 `complete` 也是拉一次
`codex exec`——只是沙箱收紧成只读、工作目录换成临时目录、结果取回纯文本。

## argv 模板：CLI 细节变了只改模板

Codex CLI 的 flag 会变（它自己还在演进），而适配器的**逻辑**不会变：拉起、圈目录、限沙箱、
指定模型、限时、取回最后一条消息、转录落盘。故把 argv 拆成**段模板**
（:data:`SESSION_ARGV` / :data:`COMPLETE_ARGV`），逻辑只负责填字段：

* 每个段是一小串 argv 片段，片段里的 `{field}` 由 :func:`render_argv` 替换；
* **段内任一字段为空 → 整段丢弃**——`--model` 没给就连 flag 一起消失，不会留下裸 flag；
* 未知字段 → 结构化错误，而不是拼出一条诡异的命令行。

于是「codex 把 `-C` 改名成 `--cd`」这类变更只改 :data:`SESSION_ARGV` 一行，或者调用方
`CodexAgent(session_argv=...)` 就地覆盖，不动任何逻辑。提示词默认经 **stdin** 递进去
（模板里的 `-` 位置参数），免得长提示词撞上 `ARG_MAX`。

## 不抛栈，只给结构化错误

CLI 不存在 / 超时 / 非零退出，对流水线都是「这个关节这次不可用」，不是崩溃：

* `session` 返回 `SessionOutcome(done=False, message=...)`，并把 :class:`CodexError` 记进
  `self.errors`。注意 `done` **仍不是裁决**（架构 §9）：`done=False` 只说明会话没能正常结束，
  修没修好由驱动器随后的重新编译说了算；
* `complete` 必须返回 `str`（协议如此），故失败时抛 :class:`CodexError`——
  `tongtu.stages.translate` 的块循环本来就把关节异常当作「一次失败的尝试」，重试用尽即回退
  原文（`fallback_reason="agent_unavailable"`），不会有栈冒到编排器。

真 subprocess 只住在 :func:`subprocess_runner` 里；注入 `runner=` 即可在没有 codex CLI 的
机器上全量单测（同 `tongtu.compiler` 把 latexmk 封在 `Compiler` 之后的做法）。

## 转录

每次调用把 `--json` 事件流与一份元信息落 `logs/`（架构 §9：审计 + 促升规则的数据来源）。
`complete` 默认只在**失败时**落盘（一篇论文几十上百块，全量落盘只是噪声），
`log_completions=True` 可全落。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from . import SessionOutcome

__all__ = [
    "CODEX",
    "COMPLETE_ARGV",
    "COMPLETE_TIMEOUT",
    "CallRecord",
    "CodexAgent",
    "CodexError",
    "ERROR",
    "FAILED",
    "MISSING_TOOL",
    "OK",
    "RUN_STATUSES",
    "READ_ONLY",
    "RunResult",
    "Runner",
    "SESSION_ARGV",
    "SESSION_TIMEOUT",
    "TIMEOUT",
    "WORKSPACE_WRITE",
    "clean_output",
    "render_argv",
    "subprocess_runner",
]

# --------------------------------------------------------------------- 常量

#: 可执行文件名（`CodexAgent(cli=...)` 覆盖）。
CODEX = "codex"

#: 沙箱档位。修复会话要写工作目录；`complete` 只需要说话，收到只读。
WORKSPACE_WRITE = "workspace-write"
READ_ONLY = "read-only"

#: 超时（秒）。会话要跑命令、装包、多轮编译，给得宽；单块翻译给得紧。
SESSION_TIMEOUT = 1800.0
COMPLETE_TIMEOUT = 600.0

# 一次调用的状态。
OK = "ok"
FAILED = "failed"  # 跑了，退出码非零
MISSING_TOOL = "missing_tool"  # PATH 里没有 codex
TIMEOUT = "timeout"
ERROR = "error"  # 调用本身炸了（OSError 等）

RUN_STATUSES: tuple[str, ...] = (OK, FAILED, MISSING_TOOL, TIMEOUT, ERROR)

#: argv 段模板：`tuple[片段, ...]`，片段里 `{field}` 待填；段内任一字段为空则整段丢弃。
Segment = tuple[str, ...]

#: 有状态修复会话的 argv（关节 ②/⑥）。
SESSION_ARGV: tuple[Segment, ...] = (
    ("{cli}",),
    ("exec",),
    ("--sandbox", "{sandbox}"),
    ("-C", "{workdir}"),
    ("--model", "{model}"),
    ("--json",),
    ("--output-last-message", "{output_file}"),
    ("--skip-git-repo-check",),
    ("{prompt_arg}",),  # 默认 `-`：提示词走 stdin，避开 ARG_MAX
)

#: 一次性无状态调用的 argv（关节 ③/⑤ 等 `complete` 场景）。
#: 不要 `--json`——这里要的是最后一条消息的纯文本，事件流只会变成待剥离的噪声。
COMPLETE_ARGV: tuple[Segment, ...] = (
    ("{cli}",),
    ("exec",),
    ("--sandbox", "{sandbox}"),
    ("-C", "{workdir}"),
    ("--model", "{model}"),
    ("--output-last-message", "{output_file}"),
    ("--skip-git-repo-check",),
    ("{prompt_arg}",),
)

#: `--output-last-message` 的落点文件名（在临时目录里）。
LAST_MESSAGE_NAME = "last-message.txt"

#: 模板字段占位符。
_FIELD_RE = re.compile(r"\{(\w+)\}")

#: 整段被 ``` 围栏包起来的输出（模型爱这么干）。
_FENCE_RE = re.compile(r"\A```[^\n]*\n(?P<body>.*?)\n?```\Z", re.DOTALL)

#: 开场白行：「以下是翻译：」「Here is the translation:」之类。
#: 判据取交集（词表 + 以冒号结尾 + 短 + 不含 LaTeX / 占位符），宁可漏剥也不错杀正文——
#: 剥错了会掉一段译文，剥漏了会被 validate 的段落数比对当场抓住。
_LEADIN_RE = re.compile(
    r"\A(?:好的|好[,，]|明白|收到|以下是|下面是|这是|翻译如下|译文如下|"
    r"here(?:'s| is)|sure|okay|ok)[^\n]{0,40}[:：]\s*\Z",
    re.IGNORECASE,
)

#: 开场白最多剥几行（防止把正文一行行剥光）。
_MAX_LEADIN_LINES = 3


class CodexError(RuntimeError):
    """Codex CLI 不可用（结构化，不带栈）。

    `kind` ∈ :data:`RUN_STATUSES` ∪ {"bad_template", "empty_output"}；`detail` 放现场
    （命令行、stderr 尾巴）。同 :class:`tongtu.compiler.AssetError` 的形状。
    """

    def __init__(self, message: str, *, kind: str = ERROR, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail

    def to_json(self) -> dict:
        return {"kind": self.kind, "message": str(self), "detail": self.detail}


# ----------------------------------------------------------------- 执行层

#: stderr 进结构化错误时的截断长度。
_DETAIL_MAX = 2000


@dataclass(frozen=True)
class RunResult:
    """一次子进程调用的结果（`runner` 的返回形状）。"""

    status: str = OK
    """:data:`OK`（跑完了，退出码另说）/ :data:`MISSING_TOOL` / :data:`TIMEOUT` / :data:`ERROR`。"""

    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK and self.returncode == 0


class Runner(Protocol):
    """执行 argv 的薄接口——真 subprocess 只在 :func:`subprocess_runner` 里。

        runner(argv, cwd=..., stdin=..., timeout=..., env=...) -> RunResult
    """

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        stdin: str = "",
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult: ...


def subprocess_runner(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    stdin: str = "",
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> RunResult:
    """默认 runner：`subprocess.run`，异常一律折成 :class:`RunResult`（不抛栈）。"""
    argv = list(argv)
    if not argv:
        return RunResult(status=ERROR, message="空命令行")
    executable = shutil.which(argv[0])
    if executable is None:
        return RunResult(
            status=MISSING_TOOL,
            message=f"PATH 中没有 {argv[0]}——装 Codex CLI，或换 --agent mock",
        )
    argv[0] = executable

    full_env = None if env is None else {**os.environ, **env}
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(
            status=TIMEOUT,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            message=f"codex 超时（{timeout:.0f}s）",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return RunResult(
            status=ERROR,
            message=f"codex 调用失败（{type(exc).__name__}）：{exc}",
        )
    return RunResult(
        status=OK,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _as_text(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


# ----------------------------------------------------------------- 纯函数


def render_argv(template: Sequence[Segment], fields: Mapping[str, object]) -> tuple[str, ...]:
    """把段模板渲染成 argv：段内任一字段为空 → 整段丢弃；未知字段 → :class:`CodexError`。

    >>> render_argv((("codex",), ("--model", "{model}")), {"model": ""})
    ('codex',)
    """
    argv: list[str] = []
    for segment in template:
        rendered: list[str] = []
        drop = False
        for piece in segment:
            def sub(match: re.Match) -> str:
                nonlocal drop
                name = match.group(1)
                if name not in fields:
                    raise CodexError(
                        f"argv 模板里有未知字段 {{{name}}}（可用：{sorted(fields)}）",
                        kind="bad_template",
                        detail=str(segment),
                    )
                value = fields[name]
                text = "" if value is None else str(value)
                if not text:
                    drop = True
                return text

            rendered.append(_FIELD_RE.sub(sub, piece))
        if not drop:
            argv.extend(rendered)
    return tuple(argv)


def clean_output(text: str) -> str:
    """清洗运行时输出：剥围栏、剥开场白、掐首尾空白。

    运行时会给纯文本任务加装饰（``` 代码块、「以下是翻译：」一类的告白）。剥得**保守**：
    只动整段围栏与词表命中的短开场白行——validate 才是裁决者，剥漏了它会打回重译，
    剥错了却会安静地掉一段正文。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    # 整段围栏（```latex … ```）。最多剥两层：偶见「围栏套围栏」。
    for _ in range(2):
        match = _FENCE_RE.match(cleaned)
        if match is None:
            break
        cleaned = match.group("body").strip()

    for _ in range(_MAX_LEADIN_LINES):
        head, sep, rest = cleaned.partition("\n")
        if not sep or "⟦" in head or "\\" in head or not _LEADIN_RE.match(head.strip()):
            break
        cleaned = rest.lstrip("\n")

    return cleaned.strip()


def join_prompt(prompt: str, text: str) -> str:
    """`complete` 的输入：规则/上下文在前，待处理正文在后，中间空一行。"""
    prompt = (prompt or "").rstrip()
    text = text or ""
    if not text:
        return prompt
    return f"{prompt}\n\n{text}" if prompt else text


# ----------------------------------------------------------------- 记录


@dataclass(frozen=True)
class CallRecord:
    """一次调用的审计记录（测试断言、report 的干预统计、促升数据源）。"""

    kind: str  # "session" / "complete"
    argv: tuple[str, ...]
    status: str
    cwd: str = ""
    model: str = ""
    joint: str = ""
    returncode: int | None = None
    duration: float = 0.0
    transcript_path: str | None = None

    def to_json(self) -> dict:
        data: dict = {
            "kind": self.kind,
            "status": self.status,
            "argv": list(self.argv),
            "duration": round(self.duration, 3),
        }
        for name in ("cwd", "model", "joint"):
            value = getattr(self, name)
            if value:
                data[name] = value
        if self.returncode is not None:
            data["returncode"] = self.returncode
        if self.transcript_path:
            data["transcript_path"] = self.transcript_path
        return data


# ----------------------------------------------------------------- 适配器


@dataclass
class CodexAgent:
    """Codex CLI 上的两原语实现。

    :param cli: 可执行文件名。
    :param model: 模型标识。空 = 用 CLI 自己的默认模型；**建议显式指定**——它进块级翻译
        缓存的 key（架构 §4），留空则换模型不会失效缓存。
    :param log_dir: 转录落点。给 `session` 传 `Workdir` 时优先用它的 `logs/`。
    :param runner: 执行 argv 的函数，默认 :func:`subprocess_runner`（唯一碰 subprocess 的地方）。
    :param session_argv / complete_argv: argv 段模板，见模块文档。
    :param budget_args: `budget` 的下发方式。Codex CLI 目前没有稳定的「最大轮数」开关，
        故默认**不下发**——`budget` 只进转录元信息；需要时给一组段模板（可用 `{budget}`）。
    :param extra_args: 无条件追加在 argv 末尾之前的片段（调试用）。
    """

    cli: str = CODEX
    model: str = ""
    log_dir: Path | None = None
    session_timeout: float = SESSION_TIMEOUT
    complete_timeout: float = COMPLETE_TIMEOUT
    session_sandbox: str = WORKSPACE_WRITE
    complete_sandbox: str = READ_ONLY
    session_argv: tuple[Segment, ...] = SESSION_ARGV
    complete_argv: tuple[Segment, ...] = COMPLETE_ARGV
    budget_args: tuple[Segment, ...] = ()
    extra_args: tuple[str, ...] = ()
    prompt_arg: str = "-"
    runner: Runner | None = None
    env: Mapping[str, str] | None = None
    log_completions: bool = False
    calls: list[CallRecord] = field(default_factory=list)
    errors: list[CodexError] = field(default_factory=list)

    # -- 原语 ① complete ----------------------------------------------------

    def complete(self, prompt: str, text: str, model: str | None = None) -> str:
        """一次性只读会话：递进 `prompt + text`，取回清洗后的纯文本。

        失败（CLI 缺失 / 超时 / 非零退出 / 空输出）抛 :class:`CodexError`——调用方
        （translate 的块循环）把它当作一次失败的尝试，重试用尽则回退原文。
        """
        with tempfile.TemporaryDirectory(prefix="tongtu-codex-") as tmp:
            tmpdir = Path(tmp)
            output_file = tmpdir / LAST_MESSAGE_NAME
            argv = self._argv(
                self.complete_argv,
                sandbox=self.complete_sandbox,
                workdir=tmpdir,
                model=model or self.model,
                output_file=output_file,
                budget=None,
            )
            full = join_prompt(prompt, text)
            result, duration = self._run(argv, cwd=tmpdir, stdin=full, timeout=self.complete_timeout)
            answer = clean_output(self._last_message(output_file, result))
            error = self._error(result, argv)
            if error is None and not answer:
                error = CodexError(
                    "codex 返回了空输出（`--output-last-message` 与 stdout 都是空的）",
                    kind="empty_output",
                    detail=" ".join(argv),
                )

            transcript = None
            if error is not None or self.log_completions:
                transcript = self._write_transcript(
                    kind="complete",
                    joint="",
                    argv=argv,
                    prompt=full,
                    result=result,
                    duration=duration,
                    answer=answer,
                    log_dir=self.log_dir,
                    model=model or self.model,
                    budget=None,
                )
            self.calls.append(
                CallRecord(
                    kind="complete",
                    argv=argv,
                    status=error.kind if error is not None else OK,
                    cwd=str(tmpdir),
                    model=model or self.model,
                    returncode=result.returncode,
                    duration=duration,
                    transcript_path=None if transcript is None else str(transcript),
                )
            )
            if error is not None:
                self.errors.append(error)
                raise error
            return answer

    # -- 原语 ② session -----------------------------------------------------

    def session(
        self,
        prompt: str,
        workdir: str | os.PathLike[str] | None = None,
        model: str | None = None,
        budget: int | None = None,
        *,
        joint: str = "",
    ) -> SessionOutcome:
        """有状态修复会话：`workspace-write` 沙箱 + `-C <workdir>` 圈定可写范围。

        `workdir` 可以是路径，也可以是 :class:`tongtu.workdir.Workdir`（此时转录落它的
        `logs/`）。返回的 `done` **不是裁决**：`True` 只表示会话正常结束，修没修好由驱动器
        随后的重新编译说了算；`False` 表示会话没能正常结束（CLI 缺失 / 超时 / 非零退出）。
        """
        root, logs = _workdir_paths(workdir, self.log_dir)
        with tempfile.TemporaryDirectory(prefix="tongtu-codex-") as tmp:
            output_file = Path(tmp) / LAST_MESSAGE_NAME
            argv = self._argv(
                self.session_argv,
                sandbox=self.session_sandbox,
                workdir=root,
                model=model or self.model,
                output_file=output_file,
                budget=budget,
            )
            result, duration = self._run(
                argv, cwd=root, stdin=prompt, timeout=self.session_timeout
            )
            answer = clean_output(self._last_message(output_file, result))
            error = self._error(result, argv)
            transcript = self._write_transcript(
                kind="session",
                joint=joint,
                argv=argv,
                prompt=prompt,
                result=result,
                duration=duration,
                answer=answer,
                log_dir=logs,
                model=model or self.model,
                budget=budget,
            )

        self.calls.append(
            CallRecord(
                kind="session",
                argv=argv,
                status=error.kind if error is not None else OK,
                cwd=str(root),
                model=model or self.model,
                joint=joint,
                returncode=result.returncode,
                duration=duration,
                transcript_path=None if transcript is None else str(transcript),
            )
        )
        if error is not None:
            self.errors.append(error)
            return SessionOutcome(done=False, transcript_path=transcript, message=str(error))
        return SessionOutcome(
            done=True,
            transcript_path=transcript,
            message=answer[:400] or "codex 会话结束（裁决在随后的重新编译）",
        )

    # -- 适配到编译回环的 SessionFn 形状 -------------------------------------

    def as_session_fn(self) -> Callable[[object], SessionOutcome]:
        """适配成 :data:`tongtu.compiler.SessionFn`（关节 ②/⑥ 的 `FixupRequest` 形状）。

        除了拆参数，这里还把关节的 prompt 资产（`skill/repair.md`）拼在现场信息之前——
        阶段驱动器只给现场（主文件、引擎、第一条错误、日志尾巴），规则住在 `skill/`。
        资产读不到时退化成只用现场信息：修复路径不该因为一份 markdown 缺失而瘫掉。
        """

        def run(request: object) -> SessionOutcome:
            workdir = getattr(request, "workdir", None)
            joint = str(getattr(request, "joint", "") or "")
            return self.session(
                _with_skill(joint, str(getattr(request, "prompt", ""))),
                workdir=workdir,
                model=self.model,
                joint=joint,
            )

        return run

    # -- 内部 ---------------------------------------------------------------

    def _argv(
        self,
        template: Sequence[Segment],
        *,
        sandbox: str,
        workdir: Path,
        model: str,
        output_file: Path,
        budget: int | None,
    ) -> tuple[str, ...]:
        fields = {
            "cli": self.cli,
            "sandbox": sandbox,
            "workdir": str(workdir),
            "model": model,
            "output_file": str(output_file),
            "prompt_arg": self.prompt_arg,
            "budget": "" if budget is None else str(budget),
        }
        argv = list(render_argv(template, fields))
        extra = list(render_argv(self.budget_args, fields)) + list(self.extra_args)
        if extra:
            # 追加片段插在提示词位置参数之前——位置参数必须留在末尾。
            at = len(argv) - 1 if argv and argv[-1] == self.prompt_arg else len(argv)
            argv[at:at] = extra
        return tuple(argv)

    def _run(
        self, argv: Sequence[str], *, cwd: Path, stdin: str, timeout: float
    ) -> tuple[RunResult, float]:
        runner = self.runner or subprocess_runner
        started = time.monotonic()
        try:
            result = runner(argv, cwd=str(cwd), stdin=stdin, timeout=timeout, env=self.env)
        except Exception as exc:  # noqa: BLE001 —— 注入的 runner 也不许把栈冒出去
            result = RunResult(
                status=ERROR, message=f"runner 抛了异常（{type(exc).__name__}）：{exc}"
            )
        return result, time.monotonic() - started

    def _error(self, result: RunResult, argv: Sequence[str]) -> CodexError | None:
        """把一次调用的结果折成结构化错误；成功则 None。"""
        if result.status == OK and result.returncode == 0:
            return None
        detail = (result.stderr or result.message or "")[-_DETAIL_MAX:]
        if result.status != OK:
            return CodexError(
                result.message or f"codex 调用未完成（{result.status}）",
                kind=result.status,
                detail=detail or " ".join(argv),
            )
        return CodexError(
            f"codex 退出码 {result.returncode}"
            + (f"：{detail.strip().splitlines()[-1]}" if detail.strip() else ""),
            kind=FAILED,
            detail=detail or " ".join(argv),
        )

    @staticmethod
    def _last_message(output_file: Path, result: RunResult) -> str:
        """优先取 `--output-last-message` 的文件，取不到退回 stdout。"""
        try:
            if output_file.is_file():
                text = output_file.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    return text
        except OSError:
            pass
        return result.stdout

    def _write_transcript(
        self,
        *,
        kind: str,
        joint: str,
        argv: Sequence[str],
        prompt: str,
        result: RunResult,
        duration: float,
        answer: str,
        log_dir: Path | None,
        model: str,
        budget: int | None,
    ) -> Path | None:
        """转录落 `logs/`：`<base>.json` 元信息 + `<base>.log` 原始 stdout（事件流）。

        写不下去（只读盘、权限）不该拖垮流水线——记 `errors` 并返回 None。
        """
        if log_dir is None:
            return None
        stamp = time.strftime("%Y%m%dT%H%M%S")
        parts = ["codex", kind, joint, stamp, f"{len(self.calls) + 1:03d}"]
        base = "-".join(part for part in parts if part)
        meta = {
            "kind": kind,
            "joint": joint,
            "argv": list(argv),
            "model": model,
            "budget": budget,
            "status": result.status,
            "returncode": result.returncode,
            "duration": round(duration, 3),
            "message": result.message,
            "prompt": prompt,
            "answer": answer,
            "stderr": result.stderr[-_DETAIL_MAX:],
        }
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            meta_path = log_dir / f"{base}.json"
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            if result.stdout:
                stream_path = log_dir / f"{base}.log"
                stream_path.write_text(result.stdout, encoding="utf-8")
                return stream_path
            return meta_path
        except OSError as exc:
            self.errors.append(
                CodexError(
                    f"转录写不进 {log_dir}（{type(exc).__name__}：{exc}）",
                    kind="transcript",
                    detail=str(log_dir),
                )
            )
            return None


# ----------------------------------------------------------------- 辅助


def _workdir_paths(
    workdir: object, fallback_logs: Path | None
) -> tuple[Path, Path | None]:
    """`(圈定的根目录, 转录目录)`。`Workdir` 走它的 `path` / `logs`，路径走它自己。"""
    if workdir is None:
        return Path.cwd(), fallback_logs
    path = getattr(workdir, "path", None)
    logs = getattr(workdir, "logs", None)
    if path is not None:
        return Path(path), (fallback_logs or (Path(logs) if logs is not None else None))
    root = Path(os.fspath(workdir))  # type: ignore[arg-type]
    return root, (fallback_logs or root / "logs")


def _with_skill(joint: str, prompt: str) -> str:
    """在现场信息之前拼上关节的 prompt 资产（`skill/repair.md`）。资产缺失即原样返回。"""
    try:
        from ..prompts import joint_prompt

        rules = joint_prompt(joint)
    except Exception:  # noqa: BLE001 —— 缺一份 markdown 不该让修复路径瘫掉
        return prompt
    return f"{rules}\n\n---\n\n{prompt}" if prompt else rules
