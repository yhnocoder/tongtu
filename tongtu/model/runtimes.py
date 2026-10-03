from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..manifests import describe_error
from ..processes import OUTPUT_EXCERPT_CHARS
from .config import Target
from .events import summarize_codex_json, summarize_pi_json, summarize_stream_json

LOGIN_CHECK_TIMEOUT_SECONDS = 30

SHEBANG_READ_BYTES = 256

CREDENTIAL_FILES: tuple[tuple[str, str], ...] = (
    ("TONGTU_CODEX_AUTH", ".codex/auth.json"),
    ("TONGTU_PI_AUTH", ".pi/agent/auth.json"),
)

CREDENTIAL_DIR_MODE = 0o700

CREDENTIAL_FILE_MODE = 0o600

CODEX_ENV_VARIABLES: tuple[str, ...] = ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")

CLAUDE_ENV_PREFIXES: tuple[str, ...] = ("ANTHROPIC_", "CLAUDE_CODE_USE_")

CLAUDE_TOOLS = "Read,Edit,Write,Glob,Grep"

CLAUDE_SANDBOX_PLATFORM = "darwin"

PI_TOOLS = "read,bash,edit,write,grep,find,ls"

PI_FAILED_STOP_REASONS: tuple[str, ...] = ("error", "aborted")

SANDBOX_SETTINGS: dict[str, object] = {
    "sandbox": {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "failIfUnavailable": True,
        "network": {"allowedDomains": []},
    }
}


@dataclass(frozen=True)
class Session:
    workdir: Path
    skill_path: str
    model: str
    effort: str | None
    max_turns: int | None
    tmp_dir: Path
    prompt: str


@dataclass(frozen=True)
class Runtime:
    name: str
    executable: str
    skill_dir: str
    needs_on_path: tuple[str, ...]
    needs_max_turns: bool
    prompt_in_argv: bool
    argv: Callable[[Session], list[str]]
    prepare_env: Callable[[dict[str, str], Session], tuple[dict[str, str], str]]
    summarize: Callable[[dict], str | None]
    failure: Callable[[dict], str | None]
    login_check: Callable[[Target, Mapping[str, str]], tuple[bool, str]]


def write_credentials(env: Mapping[str, str], home: Path) -> list[Path]:
    written: list[Path] = []
    for variable, relative in CREDENTIAL_FILES:
        content = env.get(variable)
        if not content:
            continue
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(CREDENTIAL_DIR_MODE)
        path.write_text(content, encoding="utf-8")
        path.chmod(CREDENTIAL_FILE_MODE)
        written.append(path)
    return written


def session_path_dirs(runtime: Runtime, executable: str) -> tuple[list[Path], str]:
    interpreter, detail = interpreter_dir(executable)
    if detail:
        return [], detail
    dirs = [interpreter] if interpreter is not None else []
    for tool in runtime.needs_on_path:
        found = shutil.which(tool)
        if found is None:
            return [], f"{tool} is not in PATH; runtime {runtime.name} needs it."
        dirs.append(Path(found).parent)
    return dirs, ""


def interpreter_dir(executable: str) -> tuple[Path | None, str]:
    try:
        with open(executable, "rb") as handle:
            head = handle.read(SHEBANG_READ_BYTES)
    except OSError:
        return None, ""
    if not head.startswith(b"#!"):
        return None, ""
    words = head[2:].partition(b"\n")[0].decode("utf-8", "replace").split()
    if not words:
        return None, ""
    if Path(words[0]).name == "env":
        name = next((word for word in words[1:] if not word.startswith("-")), "")
    else:
        name = words[0]
    found = shutil.which(name) if name else None
    if found is None:
        return None, f"{executable} is a script for {name or words[0]}, which is not in PATH."
    return Path(found).parent, ""


def _run_login_check(command: list[str], env: Mapping[str, str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=LOGIN_CHECK_TIMEOUT_SECONDS, env=dict(env)
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, describe_error(error)
    if completed.returncode == 0:
        return True, completed.stdout.strip().partition("\n")[0]
    return False, (completed.stderr.strip() or completed.stdout.strip())[:OUTPUT_EXCERPT_CHARS]


def _codex_argv(session: Session) -> list[str]:
    argv = [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-s",
        "workspace-write",
        "-c",
        'approval_policy="never"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c",
        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "-c",
        "allow_login_shell=false",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        'web_search="disabled"',
        "-c",
        "skills.include_instructions=false",
    ]
    if session.model:
        argv += ["-m", session.model]
    if session.effort:
        argv += ["-c", f'model_reasoning_effort="{session.effort}"']
    return argv


def _codex_env(env: dict[str, str], session: Session) -> tuple[dict[str, str], str]:
    auth = Path(env.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    if not auth.is_file():
        return {}, f"Codex login file {auth} is missing; run codex login first (file credential store)."
    try:
        (session.tmp_dir / "auth.json").symlink_to(auth.resolve())
    except OSError as error:
        return {}, f"cannot prepare Codex authentication ({describe_error(error)})."
    prepared = {name: value for name, value in env.items() if name not in CODEX_ENV_VARIABLES}
    prepared["CODEX_HOME"] = str(session.tmp_dir)
    return prepared, ""


def _codex_failure(event: dict) -> str | None:
    if event.get("type") != "turn.failed":
        return None
    error = event.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) and message else "turn.failed"


def _codex_login_check(target: Target, env: Mapping[str, str]) -> tuple[bool, str]:
    return _run_login_check(["codex", "login", "status"], env)


def _claude_argv(session: Session) -> list[str]:
    argv = ["-p"]
    if session.model:
        argv += ["--model", session.model]
    if session.effort:
        argv += ["--effort", session.effort]
    argv += [
        "--max-turns",
        str(session.max_turns),
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--allowedTools",
        CLAUDE_TOOLS,
        "--permission-mode",
        "auto",
        "--disallowedTools",
        "Edit(.claude/skills/**)",
    ]
    if sys.platform == CLAUDE_SANDBOX_PLATFORM:
        argv += ["--settings", json.dumps(SANDBOX_SETTINGS, separators=(",", ":"))]
    return argv


def _without_claude_variables(env: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in env.items() if not name.startswith(CLAUDE_ENV_PREFIXES)}


def _claude_env(env: dict[str, str], session: Session) -> tuple[dict[str, str], str]:
    return _without_claude_variables(env), ""


def _claude_failure(event: dict) -> str | None:
    if event.get("type") != "result" or event.get("is_error") is not True:
        return None
    return str(event.get("result") or "result is_error")


def _claude_login_check(target: Target, env: Mapping[str, str]) -> tuple[bool, str]:
    found, detail = _run_login_check(["claude", "auth", "status"], _without_claude_variables(env))
    return found, "logged in" if found else detail


def _pi_argv(session: Session) -> list[str]:
    argv = [
        "-p",
        "--mode",
        "json",
        "--no-session",
        "-ne",
        "-ns",
        "-np",
        "-nc",
        "-na",
        "--offline",
        "-t",
        PI_TOOLS,
        "--skill",
        str(session.workdir / session.skill_path),
    ]
    if session.model:
        argv += ["--model", session.model]
    if session.effort:
        argv += ["--thinking", session.effort]
    return argv + ["--", session.prompt]


def _pi_env(env: dict[str, str], session: Session) -> tuple[dict[str, str], str]:
    return dict(env), ""


def _pi_failure(event: dict) -> str | None:
    if event.get("type") != "message_end":
        return None
    message = event.get("message")
    stop_reason = message.get("stopReason") if isinstance(message, dict) else None
    if stop_reason not in PI_FAILED_STOP_REASONS:
        return None
    return f"{stop_reason}: {message.get('errorMessage') or '(no errorMessage)'}"


def _pi_login_check(target: Target, env: Mapping[str, str]) -> tuple[bool, str]:
    if target.model:
        return _run_login_check(["pi", "auth", "check", "--model", target.model, "--no-refresh"], env)
    settings = Path(env.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent") / "settings.json"
    try:
        provider = json.loads(settings.read_text(encoding="utf-8")).get("defaultProvider")
    except (OSError, ValueError, AttributeError):
        provider = None
    if not isinstance(provider, str) or not provider:
        return False, "cannot read pi settings.json for the default provider"
    return _run_login_check(["pi", "auth", "check", "--provider", provider, "--no-refresh"], env)


RUNTIMES: dict[str, Runtime] = {
    "codex": Runtime(
        name="codex",
        executable="codex",
        skill_dir=".codex/skills/{role}",
        needs_on_path=(),
        needs_max_turns=False,
        prompt_in_argv=False,
        argv=_codex_argv,
        prepare_env=_codex_env,
        summarize=summarize_codex_json,
        failure=_codex_failure,
        login_check=_codex_login_check,
    ),
    "claude-code": Runtime(
        name="claude-code",
        executable="claude",
        skill_dir=".claude/skills/{role}",
        needs_on_path=(),
        needs_max_turns=True,
        prompt_in_argv=False,
        argv=_claude_argv,
        prepare_env=_claude_env,
        summarize=summarize_stream_json,
        failure=_claude_failure,
        login_check=_claude_login_check,
    ),
    "pi": Runtime(
        name="pi",
        executable="pi",
        skill_dir=".pi/skills/{role}",
        needs_on_path=("node",),
        needs_max_turns=False,
        prompt_in_argv=True,
        argv=_pi_argv,
        prepare_env=_pi_env,
        summarize=summarize_pi_json,
        failure=_pi_failure,
        login_check=_pi_login_check,
    ),
}
