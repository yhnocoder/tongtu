"""子进程执行：以新会话启动、超时按进程组终止、回收子进程并计耗时。

消费方是 precompile 阶段的 latexmk 执行与 agent 适配层的会话拉起。两者都会派生子进程
（latexmk 派生 xelatex，agent 运行时派生编译与脚本），所以一律以新会话启动（
`start_new_session=True`）自成进程组：超时时终止整个进程组，再回收子进程，不留后台进程
继续写工作目录。

stderr 一律走管道捕获在内存里，由调用方决定摘录多少进 message 或 detail；stdout 的去向
是参数：默认同样走管道，agent 会话则直接重定向到已打开的 transcript 文件，让运行时逐行
写入，父进程只读 stderr 一个管道，不存在两个管道交替读取的阻塞问题。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

#: 子进程输出摘录进 manifest 的 message 或 WorkOutcome 的 detail 时的字符数上限。
OUTPUT_EXCERPT_CHARS = 500


@dataclass(frozen=True)
class ProcessOutcome:
    """一次子进程执行的结果：退出码、stderr、是否超时、耗时秒数。"""

    returncode: int
    stderr: bytes
    timed_out: bool
    duration_seconds: float

    @property
    def stderr_text(self) -> str:
        """stderr 解码成文本；子进程输出混杂多种编码，解码错误一律替换。"""
        return self.stderr.decode("utf-8", errors="replace")


def run_in_process_group(
    command: list[str],
    cwd: Path,
    timeout_seconds: float,
    *,
    stdout: int | IO[bytes] = subprocess.PIPE,
    stdin: int | None = None,
    input_bytes: bytes | None = None,
) -> ProcessOutcome:
    """在 `cwd` 里执行一次命令，返回退出码、stderr、是否超时与耗时。

    `stdout` 接收 `subprocess` 的常量或一个已打开的二进制文件对象；`stdin` 为 None 时
    子进程继承父进程的 stdin，传 `subprocess.DEVNULL` 则接 /dev/null，运行时不会停下来
    等输入。`input_bytes` 非 None 时把这些字节写进子进程的 stdin 后关闭（此时 `stdin`
    参数不生效，管道由本函数建立）。超时按进程组终止后仍要 `communicate` 一次回收子进程
    与剩余输出。

    拉不起进程（可执行文件不在 PATH、cwd 不存在）时抛 OSError，由调用方转对应状态。
    """
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if input_bytes is not None else stdin,
        stdout=stdout,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        _, stderr = process.communicate(input=input_bytes, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        _, stderr = process.communicate()
    return ProcessOutcome(
        returncode=process.returncode,
        stderr=stderr,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
    )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """终止该进程及它派生的子进程；取不到进程组时退回只终止该进程自身。"""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()
