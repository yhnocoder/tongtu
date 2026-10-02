from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tongtu.model.config import Target
from tongtu.model.runtimes import (
    LOGIN_CHECK_TIMEOUT_SECONDS,
    RUNTIMES,
    SANDBOX_SETTINGS,
    Session,
)

PROMPT = "读 skill 按它做"


def session(tmp_path: Path, skill_path: str, model: str = "m1", effort: str | None = "low") -> Session:
    return Session(
        workdir=tmp_path / "site",
        skill_path=skill_path,
        model=model,
        effort=effort,
        max_turns=4,
        tmp_dir=tmp_path / "tmp",
        prompt=PROMPT,
    )


def test_runtime_table_lists_the_three_names() -> None:
    assert list(RUNTIMES) == ["codex", "claude-code", "pi"]
    for name, runtime in RUNTIMES.items():
        assert runtime.name == name
    assert RUNTIMES["codex"].executable == "codex"
    assert RUNTIMES["claude-code"].executable == "claude"
    assert RUNTIMES["pi"].executable == "pi"
    assert RUNTIMES["codex"].skill_dir == ".codex/skills/{role}"
    assert RUNTIMES["claude-code"].skill_dir == ".claude/skills/{role}"
    assert RUNTIMES["pi"].skill_dir == ".pi/skills/{role}"
    assert RUNTIMES["pi"].needs_on_path == ("node",)
    assert RUNTIMES["codex"].needs_on_path == RUNTIMES["claude-code"].needs_on_path == ()
    assert [runtime.needs_max_turns for runtime in RUNTIMES.values()] == [False, True, False]
    assert [runtime.prompt_in_argv for runtime in RUNTIMES.values()] == [False, False, True]


def test_codex_argv_with_model_and_effort(tmp_path: Path) -> None:
    argv = RUNTIMES["codex"].argv(session(tmp_path, ".codex/skills/review"))
    assert argv[:2] == ["exec", "--json"]
    for flag in ("--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules"):
        assert flag in argv
    assert argv[argv.index("-s") + 1] == "workspace-write"
    settings = [argv[index + 1] for index, part in enumerate(argv) if part == "-c"]
    assert settings == [
        'approval_policy="never"',
        "sandbox_workspace_write.network_access=false",
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "allow_login_shell=false",
        "project_doc_max_bytes=0",
        'web_search="disabled"',
        "skills.include_instructions=false",
        'model_reasoning_effort="low"',
    ]
    assert argv[argv.index("-m") + 1] == "m1"
    assert PROMPT not in argv


def test_codex_argv_without_model_or_effort(tmp_path: Path) -> None:
    argv = RUNTIMES["codex"].argv(session(tmp_path, ".codex/skills/review", model="", effort=None))
    assert "-m" not in argv
    assert not any("model_reasoning_effort" in part for part in argv)


def test_claude_argv_with_model_and_effort(tmp_path: Path) -> None:
    argv = RUNTIMES["claude-code"].argv(session(tmp_path, ".claude/skills/review"))
    assert argv[:5] == ["-p", "--model", "m1", "--effort", "low"]
    assert argv[argv.index("--max-turns") + 1] == "4"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == "Read,Edit,Write,Glob,Grep,Bash"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--disallowedTools") + 1] == "Edit(.claude/skills/**)"
    assert json.loads(argv[argv.index("--settings") + 1]) == SANDBOX_SETTINGS
    assert SANDBOX_SETTINGS == {
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True,
            "network": {"allowedDomains": []},
        }
    }
    assert PROMPT not in argv


def test_claude_argv_without_model_or_effort(tmp_path: Path) -> None:
    argv = RUNTIMES["claude-code"].argv(session(tmp_path, ".claude/skills/review", model="", effort=None))
    assert argv[:3] == ["-p", "--max-turns", "4"]
    assert "--model" not in argv
    assert "--effort" not in argv


def test_pi_argv_with_model_and_effort(tmp_path: Path) -> None:
    argv = RUNTIMES["pi"].argv(session(tmp_path, ".pi/skills/review", model="deepseek/deepseek-flash"))
    assert argv[:3] == ["-p", "--mode", "json"]
    for flag in ("--no-session", "-ne", "-ns", "-np", "-nc", "-na", "--offline"):
        assert flag in argv
    assert argv[argv.index("-t") + 1] == "read,bash,edit,write,grep,find,ls"
    assert argv[argv.index("--skill") + 1] == str(tmp_path / "site" / ".pi" / "skills" / "review")
    assert argv[argv.index("--model") + 1] == "deepseek/deepseek-flash"
    assert argv[argv.index("--thinking") + 1] == "low"
    assert argv[-2:] == ["--", PROMPT]


def test_pi_argv_without_model_or_effort(tmp_path: Path) -> None:
    argv = RUNTIMES["pi"].argv(session(tmp_path, ".pi/skills/review", model="", effort=None))
    assert "--model" not in argv
    assert "--thinking" not in argv
    assert argv[-2:] == ["--", PROMPT]


def test_codex_prepare_env_without_login_file_is_an_error(tmp_path: Path) -> None:
    current = session(tmp_path, ".codex/skills/review")
    current.tmp_dir.mkdir()
    env = {"CODEX_HOME": str(tmp_path / "missing"), "PATH": "/usr/bin"}
    prepared, detail = RUNTIMES["codex"].prepare_env(env, current)
    assert prepared == {}
    assert "codex login" in detail
    assert str(tmp_path / "missing" / "auth.json") in detail
    assert not (current.tmp_dir / "auth.json").exists()


def test_codex_prepare_env_links_the_login_file_into_a_temporary_home(tmp_path: Path) -> None:
    current = session(tmp_path, ".codex/skills/review")
    current.tmp_dir.mkdir()
    login = tmp_path / "login"
    login.mkdir()
    (login / "auth.json").write_text('{"test":true}', encoding="utf-8")
    (login / "config.toml").write_text('sandbox_mode="danger-full-access"', encoding="utf-8")
    env = {
        "CODEX_HOME": str(login),
        "CODEX_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
        "OPENAI_BASE_URL": "https://x",
        "PATH": "/usr/bin",
        "TONGTU_DISABLE": "1",
    }
    prepared, detail = RUNTIMES["codex"].prepare_env(env, current)
    assert detail == ""
    assert prepared["CODEX_HOME"] == str(current.tmp_dir)
    assert (current.tmp_dir / "auth.json").is_symlink()
    assert (current.tmp_dir / "auth.json").resolve() == (login / "auth.json").resolve()
    assert not (current.tmp_dir / "config.toml").exists()
    for variable in ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        assert variable not in prepared
    assert prepared["PATH"] == "/usr/bin"
    assert prepared["TONGTU_DISABLE"] == "1"
    assert env["CODEX_HOME"] == str(login)


def test_codex_prepare_env_defaults_to_the_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    current = session(tmp_path, ".codex/skills/review")
    current.tmp_dir.mkdir()
    prepared, detail = RUNTIMES["codex"].prepare_env({"PATH": "/usr/bin"}, current)
    assert prepared == {}
    assert str(tmp_path / "user" / ".codex" / "auth.json") in detail


def test_claude_prepare_env_drops_anthropic_and_backend_variables(tmp_path: Path) -> None:
    env = {
        "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_AUTH_TOKEN": "t",
        "ANTHROPIC_BASE_URL": "u",
        "ANTHROPIC_PROFILE": "p",
        "ANTHROPIC_FEDERATION_RULE_ID": "f",
        "ANTHROPIC_ORGANIZATION_ID": "o",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDE_CODE_REMOTE": "true",
        "CLAUDE_CODE_OAUTH_TOKEN": "token",
        "PATH": "/usr/bin",
        "TONGTU_DISABLE": "1",
    }
    prepared, detail = RUNTIMES["claude-code"].prepare_env(env, session(tmp_path, ".claude/skills/review"))
    assert detail == ""
    assert prepared == {
        "CLAUDE_CODE_REMOTE": "true",
        "CLAUDE_CODE_OAUTH_TOKEN": "token",
        "PATH": "/usr/bin",
        "TONGTU_DISABLE": "1",
    }
    assert "ANTHROPIC_API_KEY" in env


def test_pi_prepare_env_keeps_the_environment(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin", "DEEPSEEK_API_KEY": "k", "HTTPS_PROXY": "http://proxy"}
    prepared, detail = RUNTIMES["pi"].prepare_env(env, session(tmp_path, ".pi/skills/review"))
    assert detail == ""
    assert prepared == env
    assert prepared is not env


def test_codex_failure_signal() -> None:
    failure = RUNTIMES["codex"].failure
    assert failure({"type": "turn.failed", "error": {"message": "model refused"}}) == "model refused"
    assert failure({"type": "turn.failed"}) == "turn.failed"
    assert failure({"type": "turn.completed", "usage": {}}) is None
    assert failure({"type": "item.completed", "item": {"type": "agent_message", "text": "turn.failed"}}) is None


def test_claude_failure_signal() -> None:
    failure = RUNTIMES["claude-code"].failure
    assert failure({"type": "result", "subtype": "success", "is_error": True, "result": "出错了"}) == "出错了"
    assert failure({"type": "result", "is_error": True}) == "result is_error"
    assert failure({"type": "result", "subtype": "success", "is_error": False, "result": "done"}) is None
    assert failure({"type": "assistant", "is_error": True}) is None


def test_pi_failure_signal() -> None:
    failure = RUNTIMES["pi"].failure
    error = {"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "401"}}
    assert failure(error) == "error: 401"
    aborted = {"type": "message_end", "message": {"role": "assistant", "stopReason": "aborted"}}
    assert failure(aborted) == "aborted: (no errorMessage)"
    assert failure({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}) is None
    assert failure({"type": "message_end", "message": {"role": "user"}}) is None
    assert failure({"type": "message_start", "message": {"stopReason": "error"}}) is None


def record_subprocess(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str = "", stderr: str = ""
) -> list[dict]:
    calls: list[dict] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_codex_login_check_passes_on_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_subprocess(monkeypatch, 0, stdout="Logged in using ChatGPT\nmore\n")
    env = {"PATH": "/usr/bin"}
    assert RUNTIMES["codex"].login_check(Target("codex", "m", None), env) == (True, "Logged in using ChatGPT")
    assert calls[0]["command"] == ["codex", "login", "status"]
    assert calls[0]["timeout"] == LOGIN_CHECK_TIMEOUT_SECONDS == 30
    assert calls[0]["env"] == env
    assert calls[0]["capture_output"] is True
    assert calls[0]["text"] is True


def test_codex_login_check_fails_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    record_subprocess(monkeypatch, 1, stderr="Not logged in\n")
    assert RUNTIMES["codex"].login_check(Target("codex", "", None), {}) == (False, "Not logged in")


def test_login_check_uses_stdout_when_stderr_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    record_subprocess(monkeypatch, 2, stdout="usage text\n")
    assert RUNTIMES["codex"].login_check(Target("codex", "", None), {}) == (False, "usage text")


def test_login_check_reports_a_launch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("codex")

    monkeypatch.setattr(subprocess, "run", fake_run)
    found, detail = RUNTIMES["codex"].login_check(Target("codex", "", None), {})
    assert found is False
    assert "FileNotFoundError" in detail


def test_login_check_reports_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, LOGIN_CHECK_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", fake_run)
    found, detail = RUNTIMES["claude-code"].login_check(Target("claude-code", "", None), {})
    assert found is False
    assert "TimeoutExpired" in detail


def test_claude_login_check_strips_variables_and_reports_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_subprocess(monkeypatch, 0, stdout='{"loggedIn": true}\n')
    env = {"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_USE_BEDROCK": "1", "PATH": "/usr/bin"}
    assert RUNTIMES["claude-code"].login_check(Target("claude-code", "opus", None), env) == (True, "logged in")
    assert calls[0]["command"] == ["claude", "auth", "status"]
    assert calls[0]["env"] == {"PATH": "/usr/bin"}


def test_claude_login_check_fails_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    record_subprocess(monkeypatch, 1, stderr="Not logged in")
    assert RUNTIMES["claude-code"].login_check(Target("claude-code", "", None), {}) == (False, "Not logged in")


def test_pi_login_check_with_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_subprocess(monkeypatch, 0, stdout='{"status":"ready"}\n')
    env = {"PATH": "/usr/bin"}
    target = Target("pi", "deepseek/deepseek-flash", None)
    assert RUNTIMES["pi"].login_check(target, env) == (True, '{"status":"ready"}')
    assert calls[0]["command"] == ["pi", "auth", "check", "--model", "deepseek/deepseek-flash", "--no-refresh"]
    assert calls[0]["env"] == env


def test_pi_login_check_reads_the_default_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "settings.json").write_text(json.dumps({"defaultProvider": "deepseek"}), encoding="utf-8")
    calls = record_subprocess(monkeypatch, 0, stdout="ready\n")
    env = {"PI_CODING_AGENT_DIR": str(agent_dir)}
    assert RUNTIMES["pi"].login_check(Target("pi", "", None), env) == (True, "ready")
    assert calls[0]["command"] == ["pi", "auth", "check", "--provider", "deepseek", "--no-refresh"]


def test_pi_login_check_defaults_to_the_home_agent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    agent_dir = tmp_path / "user" / ".pi" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "settings.json").write_text(json.dumps({"defaultProvider": "openai"}), encoding="utf-8")
    calls = record_subprocess(monkeypatch, 1, stderr="missing key")
    assert RUNTIMES["pi"].login_check(Target("pi", "", None), {}) == (False, "missing key")
    assert calls[0]["command"] == ["pi", "auth", "check", "--provider", "openai", "--no-refresh"]


@pytest.mark.parametrize("content", [None, "not json", '{"defaultModel": "x"}', '{"defaultProvider": ""}', "[]"])
def test_pi_login_check_without_a_default_provider_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    if content is not None:
        (agent_dir / "settings.json").write_text(content, encoding="utf-8")
    calls = record_subprocess(monkeypatch, 0)
    found, detail = RUNTIMES["pi"].login_check(Target("pi", "", None), {"PI_CODING_AGENT_DIR": str(agent_dir)})
    assert found is False
    assert "settings.json" in detail
    assert calls == []
