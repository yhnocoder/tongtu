from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from pydantic import ValidationError

from .. import fonts
from ..artifacts.common import Session, StopReason, Usage
from ..assets import asset_path
from ..processes import OUTPUT_EXCERPT_CHARS, run_in_process_group
from ..workdir import raw_log_path
from .config import Target, load_config, mode_target

SKILL_ROOT = asset_path("skill")

SKILL_DIR = ".skills/{skill}"

PROMPT = "读 {skill_path}/SKILL.md，按它做；现场是当前目录这棵树，只在其中读写。"

AGENTRUN_EXECUTABLE = "agentrun"

AGENTRUN_GRACE_SECONDS = 120

PYTHON_EXECUTABLE = "python3"

COST_RUNTIME = "claude-code"

COST_RESULT_TYPE = "result"

COST_FIELD = "total_cost_usd"


@dataclass(frozen=True)
class WorkOutcome:
    session: Session
    detail: str = ""


@dataclass
class _Events:
    ends: list[dict]
    turns: int = 0


def work(
    skill: str,
    session_dir: Path,
    *,
    mode: str,
    log_path: Path,
    report: Callable[[str], None] | None = None,
    extra_prompt: str = "",
) -> WorkOutcome:
    started = time.monotonic()
    config, detail = load_config()
    if config is None:
        return _outcome(started, "", None, StopReason.ERROR, None, detail)
    target, detail = mode_target(config, mode)
    if target is None:
        return _outcome(started, "", None, StopReason.ERROR, None, detail)
    stop_reason, usage, detail = _launch(target, skill, session_dir, log_path, report, extra_prompt)
    return _outcome(started, str(target), target.effort, stop_reason, usage, detail)


def _outcome(
    started: float, model: str, effort: str | None, stop_reason: StopReason, usage: Usage | None, detail: str
) -> WorkOutcome:
    session = Session(
        stop_reason=stop_reason,
        model=model,
        effort=effort,
        duration_seconds=time.monotonic() - started,
        usage=usage,
    )
    return WorkOutcome(session=session, detail=detail)


def _launch(
    target: Target,
    skill: str,
    session_dir: Path,
    log_path: Path,
    report: Callable[[str], None] | None,
    extra_prompt: str,
) -> tuple[StopReason, Usage | None, str]:
    executable = shutil.which(AGENTRUN_EXECUTABLE)
    if executable is None:
        return StopReason.ERROR, None, f"{AGENTRUN_EXECUTABLE} is not in PATH; every session runs through it."

    skill_path = skill_dir(skill)
    source = SKILL_ROOT / skill
    if not source.is_dir():
        return StopReason.ERROR, None, f"skill directory {source} does not exist; skill {skill} has nothing to copy."
    shutil.copytree(source, session_dir / skill_path, dirs_exist_ok=True)

    raw_path = raw_log_path(log_path)
    events = _Events(ends=[])
    with tempfile.TemporaryDirectory(prefix="tongtu-work-") as tmp_dir:
        python_bin = Path(tmp_dir) / "bin"
        interpreter = Path(sys.base_prefix) / "bin" / PYTHON_EXECUTABLE
        try:
            python_bin.mkdir()
            (python_bin / PYTHON_EXECUTABLE).symlink_to(interpreter)
        except OSError as error:
            return (
                StopReason.ERROR,
                None,
                f"failed to prepare the {PYTHON_EXECUTABLE} link to {interpreter} for the session"
                f" ({type(error).__name__}: {error}).",
            )
        command = [
            executable,
            target.runtime,
            "--cwd",
            str(session_dir),
            "--format",
            "jsonl",
            "--timeout",
            str(math.ceil(target.timeout_seconds)),
            "--path",
            str(python_bin),
            "--raw",
            str(raw_path),
            "--no-subagents",
        ]
        if target.model:
            command += ["--model", target.model]
        if target.effort:
            command += ["--effort", target.effort]
        environment = fonts.environment(fonts.configured(), os.environ) | {"TONGTU_DISABLE": "1"}
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("wb") as events_file:
                outcome = run_in_process_group(
                    command,
                    session_dir,
                    target.timeout_seconds + AGENTRUN_GRACE_SECONDS,
                    input_bytes=(PROMPT.format(skill_path=skill_path) + extra_prompt).encode("utf-8"),
                    env=environment,
                    on_stdout_line=_event_line(events_file, report, events),
                )
        except OSError as error:
            return (
                StopReason.ERROR,
                None,
                f"failed to open {log_path} or launch {executable} ({type(error).__name__}: {error}).",
            )
    if outcome.timed_out:
        return (
            StopReason.ERROR,
            None,
            f"{executable} did not stop within {AGENTRUN_GRACE_SECONDS} seconds after its own timeout; tongtu killed it.",
        )
    if not events.ends:
        stderr = outcome.stderr_text.strip()[-OUTPUT_EXCERPT_CHARS:]
        return (
            StopReason.ERROR,
            None,
            f"{executable} exited with code {outcome.returncode} without an end event; stderr: {stderr or '(empty)'}",
        )
    end = events.ends[-1]
    usage = session_usage(end, events.turns, _cost(target.runtime, raw_path))
    stop_reason = _stop_reason(end)
    if stop_reason is not StopReason.ERROR:
        return stop_reason, usage, ""
    return StopReason.ERROR, usage, f"{AGENTRUN_EXECUTABLE} {end.get('status')}: {end.get('detail') or '(no detail)'}"


def session_usage(end: dict, turns: int, cost_usd: float | None) -> Usage | None:
    reported = end.get("usage")
    if not isinstance(reported, dict):
        return None
    try:
        return Usage.model_validate({**reported, "turns": turns, "cost_usd": cost_usd})
    except ValidationError:
        return None


def result_cost(raw_lines: Sequence[bytes]) -> float | None:
    results = [record for record in map(_parse_event, raw_lines) if record and record.get("type") == COST_RESULT_TYPE]
    if not results:
        return None
    cost = results[-1].get(COST_FIELD)
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        return None
    return float(cost)


def _cost(runtime: str, raw_path: Path) -> float | None:
    if runtime != COST_RUNTIME:
        return None
    try:
        return result_cost(raw_path.read_bytes().splitlines())
    except OSError:
        return None


def _stop_reason(end: dict) -> StopReason:
    status = end.get("status")
    if status in (StopReason.FINISHED, StopReason.TIMEOUT):
        return StopReason(status)
    return StopReason.ERROR


def skill_dir(skill: str) -> str:
    return SKILL_DIR.format(skill=skill)


def _event_line(
    events_file: IO[bytes], report: Callable[[str], None] | None, events: _Events
) -> Callable[[bytes], None]:
    def handle(line: bytes) -> None:
        events_file.write(line)
        events_file.flush()
        event = _parse_event(line)
        if event is None:
            return
        kind = event.get("type")
        if kind == "tool" and report is not None and isinstance(event.get("summary"), str):
            report(event["summary"])
        if kind == "usage" and event.get("parent") is None:
            events.turns += 1
        if kind == "end":
            events.ends.append(event)

    return handle


def _parse_event(line: bytes) -> dict | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None
