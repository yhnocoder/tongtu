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

WORK_ROLE_FIELDS = ("max_turns", "timeout_seconds")

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
        return _error(f"role {role} is missing fields {', '.join(absent)}; add them under [roles] in {models_path()}.")
    name = resolved.runtime or ""
    runtime = config.runtime[name]
    skill_path = runtime.skill_path.format(role=role)

    base_url: str | None = None
    api_key: str | None = None
    if runtime.provider is not None:
        provider = config.provider.get(runtime.provider)
        if provider is None:
            return _error(
                f"runtime {name} declares provider {runtime.provider}, which is not configured;"
                f" add it under [provider.{runtime.provider}] in {models_path()}."
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
            return _error(f"runtime {name} is not in PATH; its command is {command[0]}.")

        source = SKILL_ROOT / role
        if not source.is_dir():
            return _error(
                f"skill directory {source} does not exist; role {role} has no skill to copy into the worksite."
            )
        shutil.copytree(source, workdir / skill_path, dirs_exist_ok=True)

        trace_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with trace_path.open("wb") as trace_file:
                outcome = run_in_process_group(
                    [executable, *command[1:]],
                    workdir,
                    entry.timeout_seconds or 0.0,
                    stdout=trace_file,
                    input_bytes=PROMPT.format(skill_path=skill_path).encode("utf-8"),
                    env=_session_env(runtime.provider is not None) | session_env,
                )
        except OSError as error:
            return _error(
                f"failed to launch {executable} ({type(error).__name__}: {error}). Check that the working directory {workdir} exists."
            )
    if outcome.timed_out:
        return WorkOutcome(stop_reason=StopReason.TIMEOUT)
    if outcome.returncode == 0:
        return WorkOutcome(stop_reason=StopReason.FINISHED)
    stderr = outcome.stderr_text.strip()[:OUTPUT_EXCERPT_CHARS]
    return _error(f"{executable} exited with code {outcome.returncode}; stderr: {stderr or '(empty)'}")


def _error(detail: str) -> WorkOutcome:
    return WorkOutcome(stop_reason=StopReason.ERROR, detail=detail)


def _session_env(provider_backed: bool) -> dict[str, str]:
    tex = shutil.which(TEX_EXECUTABLE)
    entries = ([str(Path(tex).parent)] if tex else []) + list(SYSTEM_PATH_ENTRIES)
    environment = dict(os.environ)
    if provider_backed:
        environment.pop("CLAUDE_CODE_REMOTE", None)
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
    templates = list(runtime.command) + list((runtime.env or {}).values())
    if runtime.settings is None and any("{settings}" in item for item in runtime.command):
        return None, (
            f"the command template of runtime {name} needs settings, but [runtime.{name}] has no settings table."
            f" Add it in {models_path()}."
        )
    if base_url is None and any("{base_url}" in item or "{api_key}" in item for item in templates):
        return None, (
            f"the command template or env table of runtime {name} needs {{base_url}} and {{api_key}},"
            f" but [runtime.{name}] has no provider field. Add it in {models_path()}."
        )
    values = {
        "{model}": resolved.model,
        "{effort}": resolved.effort,
        "{max_turns}": str(entry.max_turns),
        "{settings}": _settings_json(runtime.settings),
        "{base_url}": base_url or "",
        "{api_key}": api_key or "",
        "{tmp_dir}": tmp_dir,
    }

    def substituted(text: str) -> str:
        for placeholder, value in values.items():
            text = text.replace(placeholder, value)
        return text

    command = [substituted(item) for item in runtime.command]
    return (command, {key: substituted(value) for key, value in (runtime.env or {}).items()}), ""
