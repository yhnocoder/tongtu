from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import IO

from .. import fonts
from ..assets import asset_path
from ..config import config_path
from ..processes import OUTPUT_EXCERPT_CHARS, run_in_process_group
from .config import ModelsConfig, Target, load_config, role_target

SKILL_ROOT = asset_path("skill")

SKILL_DIR = ".skills/{role}"

PROMPT = "读 {skill_path}/SKILL.md，按它做；现场是当前目录这棵树，只在其中读写。"

AGENTRUN_EXECUTABLE = "agentrun"

MAX_TURNS_RUNTIME = "claude-code"

AGENTRUN_GRACE_SECONDS = 120

EVENTS_SUFFIX = ".events.jsonl"

PYTHON_EXECUTABLE = "python3"


class StopReason(StrEnum):
    FINISHED = "finished"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class WorkOutcome:
    stop_reason: StopReason
    detail: str = ""
    model: str = ""


def work(
    role: str,
    workdir: Path,
    *,
    trace_path: Path,
    effort: str | None = None,
    report: Callable[[str], None] | None = None,
    prompt: str | None = None,
) -> WorkOutcome:
    config, target, detail = _runtime_target(role, effort)
    if config is None or target is None:
        return _error(detail)
    outcome = _launch(config, target, role, workdir, trace_path, report, prompt)
    return replace(outcome, model=str(target))


def _launch(
    config: ModelsConfig,
    target: Target,
    role: str,
    workdir: Path,
    trace_path: Path,
    report: Callable[[str], None] | None,
    prompt: str | None,
) -> WorkOutcome:
    entry = config.roles[role]
    absent = ["timeout_seconds"] if entry.timeout_seconds is None else []
    if target.backend == MAX_TURNS_RUNTIME and entry.max_turns is None:
        absent.append("max_turns")
    if absent:
        return _error(f"role {role} is missing fields {', '.join(absent)}; add them under [roles] in {config_path()}.")
    executable = shutil.which(AGENTRUN_EXECUTABLE)
    if executable is None:
        return _error(f"{AGENTRUN_EXECUTABLE} is not in PATH; work runs every runtime through it.")

    skill_path = skill_dir(role)
    source = SKILL_ROOT / role
    if not source.is_dir():
        return _error(f"skill directory {source} does not exist; role {role} has no skill to copy into the worksite.")
    shutil.copytree(source, workdir / skill_path, dirs_exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tongtu-work-") as tmp_dir:
        python_bin = Path(tmp_dir) / "bin"
        interpreter = Path(sys.base_prefix) / "bin" / PYTHON_EXECUTABLE
        try:
            python_bin.mkdir()
            (python_bin / PYTHON_EXECUTABLE).symlink_to(interpreter)
        except OSError as error:
            return _error(
                f"failed to prepare the {PYTHON_EXECUTABLE} link to {interpreter} for the session"
                f" ({type(error).__name__}: {error})."
            )
        command = [
            executable,
            target.backend,
            "--cwd",
            str(workdir),
            "--format",
            "jsonl",
            "--timeout",
            str(math.ceil(entry.timeout_seconds)),
            "--path",
            str(python_bin),
            "--raw",
            str(trace_path),
            "--no-subagents",
        ]
        if target.model:
            command += ["--model", target.model]
        if target.effort:
            command += ["--effort", target.effort]
        if target.backend == MAX_TURNS_RUNTIME:
            command += ["--max-turns", str(entry.max_turns)]
        environment = fonts.environment(fonts.configured(), os.environ) | {"TONGTU_DISABLE": "1"}
        ends: list[dict] = []
        try:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            with events_path(trace_path).open("wb") as events_file:
                outcome = run_in_process_group(
                    command,
                    workdir,
                    entry.timeout_seconds + AGENTRUN_GRACE_SECONDS,
                    input_bytes=(prompt or PROMPT.format(skill_path=skill_path)).encode("utf-8"),
                    env=environment,
                    on_stdout_line=_event_line(events_file, report, ends),
                )
        except OSError as error:
            return _error(
                f"failed to open {events_path(trace_path)} or launch {executable} ({type(error).__name__}: {error})."
            )
    if outcome.timed_out:
        return _error(
            f"{executable} did not stop within {AGENTRUN_GRACE_SECONDS} seconds after its own timeout; tongtu killed it."
        )
    if not ends:
        stderr = outcome.stderr_text.strip()[-OUTPUT_EXCERPT_CHARS:]
        return _error(
            f"{executable} exited with code {outcome.returncode} without an end event; stderr: {stderr or '(empty)'}"
        )
    status = ends[-1].get("status")
    if status == "finished":
        return WorkOutcome(stop_reason=StopReason.FINISHED)
    if status == "timeout":
        return WorkOutcome(stop_reason=StopReason.TIMEOUT)
    return _error(f"{AGENTRUN_EXECUTABLE} {status}: {ends[-1].get('detail') or '(no detail)'}")


def skill_dir(role: str) -> str:
    return SKILL_DIR.format(role=role)


def events_path(trace_path: Path) -> Path:
    return trace_path.with_name(trace_path.name.removesuffix(".jsonl") + EVENTS_SUFFIX)


def _runtime_target(role: str, effort: str | None = None) -> tuple[ModelsConfig | None, Target | None, str]:
    config, detail = load_config()
    if config is None:
        return None, None, detail
    target, detail = role_target(config, role, effort)
    if target is None:
        return None, None, detail
    if not target.is_runtime:
        return (
            None,
            None,
            f"role {role} points at provider {target.backend}; work needs a runtime (codex, claude-code or pi).",
        )
    return config, target, ""


def _error(detail: str) -> WorkOutcome:
    return WorkOutcome(stop_reason=StopReason.ERROR, detail=detail)


def _event_line(
    events_file: IO[bytes], report: Callable[[str], None] | None, ends: list[dict]
) -> Callable[[bytes], None]:
    def handle(line: bytes) -> None:
        events_file.write(line)
        events_file.flush()
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "tool" and report is not None and isinstance(event.get("summary"), str):
            report(event["summary"])
        if event.get("type") == "end":
            ends.append(event)

    return handle
