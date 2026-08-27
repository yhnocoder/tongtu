from __future__ import annotations

import sys
from pathlib import Path

from tongtu.processes import ProcessOutcome, run_in_process_group

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


def test_without_a_line_handler_the_outcome_is_unchanged(tmp_path: Path) -> None:
    outcome = run_python(EMIT_SCRIPT, tmp_path)
    assert outcome.returncode == 3
    assert outcome.stderr == b"boom\n"
    assert not outcome.timed_out
    assert outcome.duration_seconds > 0
