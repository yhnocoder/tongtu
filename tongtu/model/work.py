from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..assets import asset_path
from ..processes import OUTPUT_EXCERPT_CHARS, run_in_process_group
from .config import ResolvedRole, RoleConfig, RoleTable, load_config, models_path, resolve_role, role_config

SKILL_ROOT = asset_path("skill")

PROMPT = "读 {skill_path}/SKILL.md，按它做；现场是当前目录这棵树，只在其中读写。"

WORK_ROLE_FIELDS = ("max_turns", "timeout_seconds", "bash")


class StopReason(StrEnum):
    FINISHED = "finished"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class WorkOutcome:
    stop_reason: StopReason
    detail: str = ""


def work(
    role: str,
    workdir: Path,
    *,
    trace_path: Path,
    model: str | None = None,
    effort: str | None = None,
) -> WorkOutcome:
    config, detail = load_config()
    if config is None:
        return _error(detail)
    resolved, detail = resolve_role(config, role, RoleTable.RUNTIME, model, effort)
    if resolved is None:
        return _error(detail)
    entry, detail = role_config(config, role)
    if entry is None:
        return _error(detail)
    absent = [name for name in WORK_ROLE_FIELDS if getattr(entry, name) is None]
    if absent:
        return _error(f"角色 {role} 缺字段 {'、'.join(absent)}，在 {models_path()} 的 [roles] 里补上。")
    runtime = config.runtime[resolved.runtime or ""]

    source = SKILL_ROOT / role
    if not source.is_dir():
        return _error(f"skill 目录 {source} 不存在，角色 {role} 没有可拷进现场的 skill。")
    skill_path = runtime.skill_path.format(role=role)
    destination = workdir / skill_path
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source, destination)

    command = _build_command(runtime.command, resolved, entry)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with trace_path.open("wb") as trace_file:
            outcome = run_in_process_group(
                command,
                workdir,
                entry.timeout_seconds or 0.0,
                stdout=trace_file,
                input_bytes=PROMPT.format(skill_path=skill_path).encode("utf-8"),
            )
    except OSError as error:
        return _error(
            f"拉起 {command[0]} 失败（{type(error).__name__}：{error}）。"
            f"确认 {command[0]} 在 PATH 里，且工作目录 {workdir} 存在。"
        )
    if outcome.timed_out:
        return WorkOutcome(stop_reason=StopReason.TIMEOUT)
    if outcome.returncode == 0:
        return WorkOutcome(stop_reason=StopReason.FINISHED)
    stderr = outcome.stderr_text.strip()[:OUTPUT_EXCERPT_CHARS]
    return _error(f"{command[0]} 退出码 {outcome.returncode}；stderr：{stderr or '（空）'}")


def _error(detail: str) -> WorkOutcome:
    return WorkOutcome(stop_reason=StopReason.ERROR, detail=detail)


def _build_command(template: list[str], resolved: ResolvedRole, entry: RoleConfig) -> list[str]:
    bash_allow = ",".join(f"Bash({prefix}:*)" for prefix in entry.bash or [])
    filled = [
        item.format(
            model=resolved.model,
            effort=resolved.effort,
            max_turns=entry.max_turns,
            bash_allow=bash_allow,
        )
        for item in template
    ]
    return [",".join(piece for piece in item.split(",") if piece) for item in filled]
