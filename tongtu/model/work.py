from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..assets import asset_path
from ..processes import OUTPUT_EXCERPT_CHARS, run_in_process_group
from .config import (
    ResolvedRole,
    RoleConfig,
    RoleTable,
    RuntimeConfig,
    load_config,
    models_path,
    provider_key,
    resolve_role,
    role_config,
)

SKILL_ROOT = asset_path("skill")

PROMPT = "读 {skill_path}/SKILL.md，按它做；现场是当前目录这棵树，只在其中读写。"

WORK_ROLE_FIELDS = ("max_turns", "timeout_seconds", "bash")

SYSTEM_PATH_ENTRIES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

TEX_EXECUTABLE = "xelatex"


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
    name = resolved.runtime or ""
    runtime = config.runtime[name]

    base_url: str | None = None
    api_key: str | None = None
    if runtime.provider is not None:
        provider = config.provider.get(runtime.provider)
        if provider is None:
            return _error(
                f"运行时 {name} 声明的服务商 {runtime.provider} 没有配置，"
                f" 在 {models_path()} 的 [provider.{runtime.provider}] 下补上。"
            )
        api_key, detail = provider_key(runtime.provider, provider)
        if api_key is None:
            return _error(detail)
        base_url = provider.base_url

    with tempfile.TemporaryDirectory(prefix="tongtu-work-") as tmp_dir:
        built, detail = _build_invocation(runtime, name, resolved, entry, base_url, api_key, tmp_dir)
        if built is None:
            return _error(detail)
        command, session_env = built
        executable = shutil.which(command[0])
        if executable is None:
            return _error(f"运行时 {name} 不在 PATH 里， 它的命令是 {command[0]}。")

        source = SKILL_ROOT / role
        if not source.is_dir():
            return _error(f"skill 目录 {source} 不存在， 角色 {role} 没有可拷进现场的 skill。")
        skill_path = runtime.skill_path.format(role=role)
        destination = workdir / skill_path
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(source, destination)

        trace_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with trace_path.open("wb") as trace_file:
                outcome = run_in_process_group(
                    [executable, *command[1:]],
                    workdir,
                    entry.timeout_seconds or 0.0,
                    stdout=trace_file,
                    input_bytes=PROMPT.format(skill_path=skill_path).encode("utf-8"),
                    env=_session_env() | session_env,
                )
        except OSError as error:
            return _error(f"拉起 {executable} 失败（{type(error).__name__}： {error}）。 确认工作目录 {workdir} 存在。")
    if outcome.timed_out:
        return WorkOutcome(stop_reason=StopReason.TIMEOUT)
    if outcome.returncode == 0:
        return WorkOutcome(stop_reason=StopReason.FINISHED)
    stderr = outcome.stderr_text.strip()[:OUTPUT_EXCERPT_CHARS]
    return _error(f"{executable} 退出码 {outcome.returncode}； stderr： {stderr or '（空）'}")


def _error(detail: str) -> WorkOutcome:
    return WorkOutcome(stop_reason=StopReason.ERROR, detail=detail)


def _session_env() -> dict[str, str]:
    tex = shutil.which(TEX_EXECUTABLE)
    entries = ([str(Path(tex).parent)] if tex else []) + list(SYSTEM_PATH_ENTRIES)
    environment = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_REMOTE"}
    return environment | {"TONGTU_DISABLE": "1", "PATH": ":".join(entries)}


def _settings_json(settings: dict | None) -> str:
    sandbox = settings.get("sandbox") if settings else None
    if isinstance(sandbox, dict) and os.environ.get("TONGTU_NESTED_SANDBOX"):
        settings = (settings or {}) | {"sandbox": sandbox | {"enableWeakerNestedSandbox": True}}
    return json.dumps(settings, separators=(",", ":"))


def _build_invocation(
    runtime: RuntimeConfig,
    name: str,
    resolved: ResolvedRole,
    entry: RoleConfig,
    base_url: str | None,
    api_key: str | None,
    tmp_dir: str,
) -> tuple[tuple[list[str], dict[str, str]] | None, str]:
    bash_allow = ",".join(f"Bash({prefix}:*)" for prefix in entry.bash or [])
    templates = list(runtime.command) + list((runtime.env or {}).values())
    if runtime.settings is None and any("{settings}" in item for item in runtime.command):
        return None, (
            f"运行时 {name} 的命令模板要填 settings， 但 [runtime.{name}] 没有 settings 表。"
            f" 在 {models_path()} 里补上。"
        )
    if base_url is None and any("{base_url}" in item or "{api_key}" in item for item in templates):
        return None, (
            f"运行时 {name} 的命令模板或 env 表要填 {{base_url}} 与 {{api_key}}，"
            f" 但 [runtime.{name}] 没有 provider 字段。 在 {models_path()} 里补上。"
        )
    values = {
        "{model}": resolved.model,
        "{effort}": resolved.effort,
        "{max_turns}": str(entry.max_turns),
        "{bash_allow}": bash_allow,
        "{settings}": _settings_json(runtime.settings),
        "{base_url}": base_url or "",
        "{api_key}": api_key or "",
        "{tmp_dir}": tmp_dir,
    }

    def substituted(text: str) -> str:
        for placeholder, value in values.items():
            text = text.replace(placeholder, value)
        return text

    command = []
    for item in runtime.command:
        drop_empty = "{bash_allow}" in item and not bash_allow
        filled = substituted(item)
        command.append(",".join(piece for piece in filled.split(",") if piece) if drop_empty else filled)
    return (command, {key: substituted(value) for key, value in (runtime.env or {}).items()}), ""
