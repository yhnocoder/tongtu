from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO

OUTPUT_EXCERPT_CHARS = 500


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    stderr: bytes
    timed_out: bool
    duration_seconds: float

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


def run_in_process_group(
    command: list[str],
    cwd: Path,
    timeout_seconds: float,
    *,
    input_bytes: bytes | None = None,
    env: Mapping[str, str] | None = None,
    on_stdout_line: Callable[[bytes], None] | None = None,
) -> ProcessOutcome:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if input_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    if on_stdout_line is None:
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
    return _run_with_line_relay(process, input_bytes, timeout_seconds, on_stdout_line, started)


def _run_with_line_relay(
    process: subprocess.Popen[bytes],
    input_bytes: bytes | None,
    timeout_seconds: float,
    on_stdout_line: Callable[[bytes], None],
    started: float,
) -> ProcessOutcome:
    stderr_parts: list[bytes] = []
    stdout_thread = threading.Thread(target=_relay_lines, args=(process.stdout, on_stdout_line))
    stderr_thread = threading.Thread(target=_collect_stream, args=(process.stderr, stderr_parts))
    stdout_thread.start()
    stderr_thread.start()
    if input_bytes is not None:
        _feed_stdin(process, input_bytes)
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        process.wait()
    stdout_thread.join()
    stderr_thread.join()
    return ProcessOutcome(
        returncode=process.returncode,
        stderr=b"".join(stderr_parts),
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
    )


def _relay_lines(stream: IO[bytes] | None, on_line: Callable[[bytes], None]) -> None:
    if stream is None:
        return
    with stream:
        for line in stream:
            on_line(line)


def _collect_stream(stream: IO[bytes] | None, parts: list[bytes]) -> None:
    if stream is None:
        return
    with stream:
        parts.append(stream.read())


def _feed_stdin(process: subprocess.Popen[bytes], input_bytes: bytes) -> None:
    if process.stdin is None:
        return
    try:
        process.stdin.write(input_bytes)
        process.stdin.close()
    except OSError:
        return


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()
