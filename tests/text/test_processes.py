from __future__ import annotations

import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

from tongtu import processes
from tongtu.processes import ProcessOutcome, run_in_process_group, terminate_active

EMIT_SCRIPT = """\
import sys
print("first line")
print("second line")
print("boom", file=sys.stderr)
sys.exit(3)
"""

ECHO_SCRIPT = """\
import sys
for line in sys.stdin:
    print(f"got {line.strip()}")
"""

HANG_SCRIPT = """\
import sys, time
print("before the hang", flush=True)
time.sleep(60)
"""


def run_python(script: str, tmp_path: Path, **kwargs: object) -> ProcessOutcome:
    return run_in_process_group([sys.executable, "-c", script], tmp_path, 30.0, **kwargs)


def test_lines_are_relayed_in_order_and_stderr_is_kept(tmp_path: Path) -> None:
    lines: list[bytes] = []
    outcome = run_python(EMIT_SCRIPT, tmp_path, on_stdout_line=lines.append)
    assert lines == [b"first line\n", b"second line\n"]
    assert outcome.returncode == 3
    assert outcome.stdout == b""
    assert outcome.stderr == b"boom\n"
    assert not outcome.timed_out


def test_input_bytes_reach_stdin_with_a_line_handler(tmp_path: Path) -> None:
    lines: list[bytes] = []
    outcome = run_python(ECHO_SCRIPT, tmp_path, input_bytes=b"ping\n", on_stdout_line=lines.append)
    assert outcome.returncode == 0
    assert lines == [b"got ping\n"]


def test_timeout_kills_the_group_and_keeps_lines_already_relayed(tmp_path: Path) -> None:
    lines: list[bytes] = []
    outcome = run_in_process_group([sys.executable, "-c", HANG_SCRIPT], tmp_path, 1.0, on_stdout_line=lines.append)
    assert outcome.timed_out
    assert lines == [b"before the hang\n"]


def test_without_a_line_handler_stdout_is_collected(tmp_path: Path) -> None:
    outcome = run_python(EMIT_SCRIPT, tmp_path)
    assert outcome.returncode == 3
    assert outcome.stdout == b"first line\nsecond line\n"
    assert outcome.stderr == b"boom\n"
    assert not outcome.timed_out
    assert outcome.duration_seconds > 0


GROUP_SCRIPT = """\
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_terminate_active_ends_the_running_process_group(tmp_path: Path) -> None:
    lines: queue.Queue[bytes] = queue.Queue()
    outcomes: list[ProcessOutcome] = []
    command = [sys.executable, "-c", GROUP_SCRIPT]
    runner = threading.Thread(
        target=lambda: outcomes.append(run_in_process_group(command, tmp_path, 60.0, on_stdout_line=lines.put))
    )
    runner.start()
    grandchild = int(lines.get(timeout=30))
    assert len(processes._active) == 1
    terminate_active()
    runner.join(timeout=30)
    assert outcomes[0].returncode == -signal.SIGTERM
    assert not outcomes[0].timed_out
    deadline = time.monotonic() + 30
    while not gone(grandchild):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert processes._active == set()


def test_finished_processes_leave_no_registration(tmp_path: Path) -> None:
    run_python(EMIT_SCRIPT, tmp_path)
    assert processes._active == set()
    terminate_active()
