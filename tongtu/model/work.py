from __future__ import annotations

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
from .events import parse_event
from .runtimes import RUNTIMES, Runtime, Session

SKILL_ROOT = asset_path("skill")

PROMPT = "读 {skill_path}/SKILL.md，按它做；现场是当前目录这棵树，只在其中读写。"

SYSTEM_PATH_ENTRIES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

TEX_EXECUTABLE = "xelatex"

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
    config, detail = load_config()
    if config is None:
        return _error(detail)
    target, detail = role_target(config, role, effort)
    if target is None:
        return _error(detail)
    if not target.is_runtime:
        return _error(
            f"role {role} points at provider {target.backend}; work needs a runtime (codex, claude-code or pi)."
        )
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
    runtime = RUNTIMES[target.backend]
    absent = ["timeout_seconds"] if entry.timeout_seconds is None else []
    if runtime.needs_max_turns and entry.max_turns is None:
        absent.append("max_turns")
    if absent:
        return _error(f"role {role} is missing fields {', '.join(absent)}; add them under [roles] in {config_path()}.")
    executable = shutil.which(runtime.executable)
    if executable is None:
        return _error(f"runtime {runtime.name} is not in PATH; its command is {runtime.executable}.")
    for tool in runtime.needs_on_path:
        if shutil.which(tool) is None:
            return _error(f"{tool} is not in PATH; runtime {runtime.name} needs it.")

    skill_path = runtime.skill_dir.format(role=role)
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
        session = Session(
            workdir=workdir,
            skill_path=skill_path,
            model=target.model,
            effort=target.effort,
            max_turns=entry.max_turns,
            tmp_dir=Path(tmp_dir),
            prompt=prompt or PROMPT.format(skill_path=skill_path),
        )
        environment, detail = runtime.prepare_env(_session_env(python_bin, runtime), session)
        if detail:
            return _error(detail)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        failures: list[str] = []
        try:
            with (
                tempfile.TemporaryDirectory(prefix=".tongtu-tmp-", dir=workdir) as sandbox_tmp,
                trace_path.open("wb") as trace_file,
            ):
                environment.update({name: str(Path(sandbox_tmp).resolve()) for name in ("TMPDIR", "TMP", "TEMP")})
                outcome = run_in_process_group(
                    [executable, *runtime.argv(session)],
                    workdir,
                    entry.timeout_seconds,
                    input_bytes=b"" if runtime.prompt_in_argv else session.prompt.encode("utf-8"),
                    env=environment,
                    on_stdout_line=_trace_line(trace_file, runtime, report, failures),
                )
        except OSError as error:
            return _error(
                f"failed to launch {executable} ({type(error).__name__}: {error}). Check that the working directory {workdir} exists."
            )
    if outcome.timed_out:
        return WorkOutcome(stop_reason=StopReason.TIMEOUT)
    if outcome.returncode != 0:
        stderr = outcome.stderr_text.strip()[-OUTPUT_EXCERPT_CHARS:]
        return _error(f"{executable} exited with code {outcome.returncode}; stderr: {stderr or '(empty)'}")
    if failures:
        return _error(failures[-1])
    return WorkOutcome(stop_reason=StopReason.FINISHED)


def skill_path(role: str) -> tuple[str | None, str]:
    config, detail = load_config()
    if config is None:
        return None, detail
    target, detail = role_target(config, role)
    if target is None:
        return None, detail
    if not target.is_runtime:
        return (
            None,
            f"role {role} points at provider {target.backend}; work needs a runtime (codex, claude-code or pi).",
        )
    return RUNTIMES[target.backend].skill_dir.format(role=role), ""


def _error(detail: str) -> WorkOutcome:
    return WorkOutcome(stop_reason=StopReason.ERROR, detail=detail)


def _trace_line(
    trace_file: IO[bytes],
    runtime: Runtime,
    report: Callable[[str], None] | None,
    failures: list[str],
) -> Callable[[bytes], None]:
    def handle(line: bytes) -> None:
        trace_file.write(line)
        trace_file.flush()
        event = parse_event(line)
        if event is None:
            return
        summary = runtime.summarize(event)
        if summary is not None and report is not None:
            report(summary)
        failure = runtime.failure(event)
        if failure is not None:
            failures.append(failure)

    return handle


def _session_env(python_bin: Path, runtime: Runtime) -> dict[str, str]:
    tex = shutil.which(TEX_EXECUTABLE)
    entries = [str(python_bin)] + ([str(Path(tex).parent)] if tex else []) + list(SYSTEM_PATH_ENTRIES)
    for tool in runtime.needs_on_path:
        found = shutil.which(tool)
        if found is not None and str(Path(found).parent) not in entries:
            entries.append(str(Path(found).parent))
    environment = fonts.environment(fonts.configured(), os.environ)
    return environment | {"TONGTU_DISABLE": "1", "PATH": ":".join(entries)}
