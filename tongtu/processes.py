from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping
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
    stdout: int | IO[bytes] = subprocess.PIPE,
    stdin: int | None = None,
    input_bytes: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> ProcessOutcome:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if input_bytes is not None else stdin,
        stdout=stdout,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
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
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()
