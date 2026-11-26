from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import typer
import typer.testing
from rich.progress import Progress
from typer.testing import CliRunner

from tongtu import __version__, cli
from tongtu.artifacts.common import CompileReport, Manifest, NotesFile, Session, StopReason, Usage
from tongtu.artifacts.compile import CompileManifest, CompileStatus
from tongtu.artifacts.fetch import FetchManifest, FetchStatus, PaperMeta
from tongtu.artifacts.status import (
    ABSENT_STATUS,
    PaperListing,
    PaperState,
    PaperStatus,
    PaperSummary,
    RunCommand,
    RunMarker,
)
from tongtu.artifacts.translate import GlossaryCounts, TranslateManifest, TranslateStatus
from tongtu.cli import RunOptions, app, main
from tongtu.manifests import load_manifest, write_manifest
from tongtu.model.config import ModelsConfig, ModeName, config_entries, load_config, write_config
from tongtu.pipeline import STAGES

from .test_arxiv import ERROR_FEED, LATEST_FEED, VERSIONED_FEED
from .test_pipeline import write_outputs

runner = CliRunner()

TABLE = """
[modes.fast]
model = "codex/m1"
effort = "low"

[modes.smart]
model = "pi/deepseek/m1"
timeout_seconds = 60
"""


SHARED_TABLE = """
[modes]
default = "fast"

[modes.fast]
model = "claude-code/m1"
effort = "low"

[modes.smart]
model = "claude-code/m1"
effort = "high"
"""


VERSION_2026 = "XeTeX 3.141592653-2.6-0.999998 (TeX Live 2026)\nkpathsea version 6.4.1\n"

VERSION_2022 = "XeTeX 3.141592653-2.6-0.999994 (TeX Live 2022)\nkpathsea version 6.3.4\n"

VERSION_WITHOUT_YEAR = "XeTeX 3.141592653-2.6-0.999996 (MiKTeX 24.1)\n"


def fake_xelatex_version(
    monkeypatch: pytest.MonkeyPatch, output: str, failing: dict[str, str] | None = None
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command == [cli.XELATEX, "--version"]:
            return subprocess.CompletedProcess(command, 0, output, "")
        runtime = command[2]
        checks = [
            {"runtime": runtime, "check": "executable", "status": "ok", "detail": f"/usr/bin/{runtime}"},
            {"runtime": runtime, "check": "login", "status": "ok", "detail": f"{runtime} logged in"},
            {"runtime": runtime, "check": "network", "status": "skip", "detail": "--network none"},
        ]
        if runtime in (failing or {}):
            checks[1] = {"runtime": runtime, "check": "login", "status": "fail", "detail": (failing or {})[runtime]}
        return subprocess.CompletedProcess(command, 1 if runtime in (failing or {}) else 0, json.dumps(checks), "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    return calls


def squeeze(text: str) -> str:
    return "".join(text.split())


@pytest.fixture(autouse=True)
def no_legacy_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "legacy_dirs_present", lambda: [])


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "isolated-home"))


def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    return tmp_path / "config.toml"


def written_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = config_path(tmp_path, monkeypatch)
    path.write_text(TABLE, encoding="utf-8")
    return path


def test_doctor_without_config_uses_the_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert f"[ok]config.tomlmodes,fontsandpdfoptions{path}absent;usingthedefaults" in squeeze(result.stdout)
    assert [command[1:] for command in calls if command[1:2] == ["doctor"]] == [
        ["doctor", "claude-code", "--json", "--model", "claude-sonnet-5-5"],
        ["doctor", "claude-code", "--json", "--model", "claude-opus-5-5"],
    ]
    assert not path.exists()


def test_doctor_with_unreadable_config_exits_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text("[modes\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    output = squeeze(result.stdout)
    assert "[missing]config.toml" in output
    assert "toolchainandfontscomplete" in output
    assert "fetch,precompileandmaskstillcan" in output
    assert calls == [[cli.XELATEX, "--version"]]


def test_doctor_reports_an_old_roles_config_as_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text(
        TABLE + '\n[roles]\ntranslate = { model = "codex/m1" }\n', encoding="utf-8"
    )
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    output = squeeze(result.stdout)
    assert "[missing]config.toml" in output
    assert "roles" in output
    assert calls == [[cli.XELATEX, "--version"]]


def test_doctor_missing_fonts_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    monkeypatch.setattr(cli, "FONTS_DIR", tmp_path / "no-fonts")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_FAILURE
    assert f"environmentincomplete:{squeeze(cli.FONT_CHECK_NAME)}" in squeeze(result.stdout)


def test_doctor_missing_toolchain_and_config_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text("[modes\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_FAILURE


@pytest.mark.parametrize("tool", [name for name, _purpose in cli.TOOLCHAIN_CHECKS])
def test_doctor_missing_one_tool_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None if name == tool else f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_FAILURE


def test_doctor_missing_toolchain_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    output = squeeze(result.stdout)
    assert "environmentincomplete:xelatex" in output
    assert "[missing]TeXLive" in output
    assert "xelatexisnotinPATH;cannotcheck" in output


def test_doctor_accepts_the_required_texlive_year(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "[ok]TeXLive" in output
    assert "(TeXLive2026)" in output


def test_doctor_rejects_an_older_texlive_year(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2022)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    output = squeeze(result.stdout)
    assert "[missing]TeXLive" in output
    assert "TeXLive2022isbelowtherequired2026" in output
    assert "environmentincomplete:TeXLive" in output


def test_doctor_rejects_output_without_a_texlive_year(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_WITHOUT_YEAR)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    output = squeeze(result.stdout)
    assert "[missing]TeXLive" in output
    assert "(MiKTeX24.1)" in output


def test_doctor_checks_the_runtime_of_each_mode_through_agentrun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "environmentcomplete." in output
    assert "[ok]config.toml" in output
    for runtime, name in (("codex", "codex/m1"), ("pi", "pi/deepseek/m1")):
        assert f"[ok]{name}agentrundoctor:executable/usr/bin/{runtime}" in output
        assert f"[ok]{name}agentrundoctor:login{runtime}loggedin" in output
        assert f"[ok]{name}agentrundoctor:networkskipped:--networknone" in output
    assert [command[1:] for command in calls if command[1:2] == ["doctor"]] == [
        ["doctor", "codex", "--json", "--model", "m1"],
        ["doctor", "pi", "--json", "--model", "deepseek/m1"],
    ]


def test_doctor_runs_agentrun_once_for_modes_sharing_a_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text(SHARED_TABLE, encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert [command[1:] for command in calls if command[1:2] == ["doctor"]] == [
        ["doctor", "claude-code", "--json", "--model", "m1"],
    ]


def test_doctor_reports_a_failed_agentrun_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026, failing={"codex": "codex login file not found"})
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    output = squeeze(result.stdout)
    assert "[missing]codex/m1agentrundoctor:logincodexloginfilenotfound" in output
    assert "[ok]pi/deepseek/m1agentrundoctor:login" in output
    assert "codex/m1notusable" in output


def test_doctor_reports_agentrun_missing_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "agentrun" else f"/usr/bin/{name}")
    calls = fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    output = squeeze(result.stdout)
    assert "[missing]pi/deepseek/m1agentrun,whichstartseveryruntimeagentrunnotfoundinPATH" in output
    assert calls == [[cli.XELATEX, "--version"]]
    assert "toolchainandfontscomplete" in output


def test_doctor_reports_agentrun_output_that_is_not_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text(SHARED_TABLE.replace("claude-code/m1", "pi"), encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == [cli.XELATEX, "--version"]:
            return subprocess.CompletedProcess(command, 0, VERSION_2026, "")
        return subprocess.CompletedProcess(command, 2, "", "agentrun: unexpected argument\n")

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    assert "[missing]piagentrundoctoragentrun:unexpectedargument" in squeeze(result.stdout)


def test_doctor_reports_agentrun_json_that_is_not_a_check_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text(SHARED_TABLE.replace("claude-code/m1", "pi"), encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == [cli.XELATEX, "--version"]:
            return subprocess.CompletedProcess(command, 0, VERSION_2026, "")
        return subprocess.CompletedProcess(command, 0, '[{"runtime": "pi"}]', "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    assert "[missing]piagentrundoctoragentrundoctorprintednochecklist" in squeeze(result.stdout)


def note_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.lstrip().startswith("note")]


def test_doctor_notes_legacy_directories_without_changing_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    legacy = [tmp_path / "legacy-config" / "tongtu", tmp_path / "legacy-share" / "tongtu"]
    monkeypatch.setattr(cli, "legacy_dirs_present", lambda: legacy)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    notes = note_lines(result.stdout)
    assert len(notes) == 2
    assert all(
        f"directory {path} exists" in line and f"under {tmp_path} (config.toml" in line
        for path, line in zip(legacy, notes, strict=True)
    )
    assert "environmentcomplete." in squeeze(result.stdout)


def test_doctor_without_legacy_directories_prints_no_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    monkeypatch.setattr(cli, "legacy_dirs_present", lambda: [])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert note_lines(result.stdout) == []


def test_doctor_does_not_ask_to_delete_a_legacy_directory_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    monkeypatch.setattr(cli, "legacy_dirs_present", lambda: [tmp_path])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    notes = note_lines(result.stdout)
    assert len(notes) == 1
    assert f"points at the legacy directory {tmp_path}" in notes[0]
    assert "delete it" not in notes[0]
    assert "environmentcomplete." in squeeze(result.stdout)


def test_config_list_prints_the_defaults_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["config", "list"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert [line.split()[0] for line in lines] == [key for key, _value in config_entries(ModelsConfig())]
    assert lines[0].split() == ["modes.default", "smart"]
    assert lines[-1].split() == ["pdf.crop_margins", "false"]
    assert [line.split() for line in lines if line.startswith("fonts.mono")] == [["fonts.mono"]]
    assert len({line.index(line.split()[1]) for line in lines if len(line.split()) == 2}) == 1
    assert not path.exists()


def test_config_list_reports_a_broken_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text("[modes\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "list"])
    assert result.exit_code == cli.EXIT_FAILURE
    assert "notvalidTOML" in squeeze(result.stderr)


def loaded_config() -> ModelsConfig:
    config, detail = load_config()
    assert config is not None, detail
    return config


@pytest.mark.parametrize(
    ("key", "value", "expected", "printed"),
    [
        ("modes.fast.model", "codex/gpt-6", "codex/gpt-6", "codex/gpt-6"),
        ("modes.fast.model", '"pi/m"', "pi/m", "pi/m"),
        ("pdf.crop_margins", "true", True, "true"),
        ("modes.smart.timeout_seconds", "60", 60, "60"),
        ("modes.smart.timeout_seconds", "90.5", 90.5, "90.5"),
        ("modes.default", "fast", ModeName.FAST, "fast"),
        ("fonts.main.bold", "", None, ""),
    ],
)
def test_config_set_changes_one_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str, expected: object, printed: str
) -> None:
    path = config_path(tmp_path, monkeypatch)
    write_config(ModelsConfig())
    result = runner.invoke(app, ["config", "set", key, value])
    assert result.exit_code == 0
    assert result.stdout == f"{key} = {printed}\n"
    assert dict(config_entries(loaded_config()))[key] == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_config_set_writes_the_file_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["config", "set", "modes.fast.effort", "high"])
    assert result.exit_code == 0
    assert path.is_file()
    config = loaded_config()
    assert config.modes.fast.effort == "high"
    assert config.modes.smart == ModelsConfig().modes.smart


def test_config_set_rejects_an_unknown_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    write_config(ModelsConfig())
    before = path.read_text(encoding="utf-8")
    result = runner.invoke(app, ["config", "set", "modes.fast.jobs", "4"])
    assert result.exit_code == cli.EXIT_USAGE
    assert "runtongtuconfiglisttoseethekeys" in squeeze(result.stderr)
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(("key", "value"), [("modes.fast.model", "ghost/m"), ("pdf.crop_margins", "yes")])
def test_config_set_rejects_an_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text(TABLE, encoding="utf-8")
    result = runner.invoke(app, ["config", "set", key, value])
    assert result.exit_code == cli.EXIT_USAGE
    lines = result.stderr.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{key}: ")
    assert "errors.pydantic.dev" not in result.stderr
    assert "input_value" not in result.stderr
    assert path.read_text(encoding="utf-8") == TABLE


def test_config_set_without_a_file_leaves_no_file_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["config", "set", "modes.fast.jobs", "4"])
    assert result.exit_code == cli.EXIT_USAGE
    assert not path.exists()
    result = runner.invoke(app, ["config", "set", "modes.fast.model", "ghost/m"])
    assert result.exit_code == cli.EXIT_USAGE
    assert not path.exists()


def test_config_list_shows_the_same_numbers_before_and_after_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path(tmp_path, monkeypatch)
    before = runner.invoke(app, ["config", "list"]).stdout.splitlines()
    write_config(ModelsConfig())
    after = runner.invoke(app, ["config", "list"]).stdout.splitlines()
    timeout = [line for line in before if line.startswith("modes.fast.timeout_seconds")]
    assert timeout == [line for line in after if line.startswith("modes.fast.timeout_seconds")]
    assert timeout[0].split() == ["modes.fast.timeout_seconds", "3600"]


def test_config_set_reports_a_broken_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text("[modes\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "set", "pdf.crop_margins", "true"])
    assert result.exit_code == cli.EXIT_FAILURE
    assert path.read_text(encoding="utf-8") == "[modes\n"


@pytest.mark.parametrize("arguments", [["config", "--dev", "list"], ["config", "list", "--dev"]])
def test_config_dev_reads_the_dev_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr(cli, "DEV_HOME", tmp_path / "dev")
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "config.toml").write_text('[modes]\ndefault = "fast"\n', encoding="utf-8")
    result = runner.invoke(app, arguments, env={"TONGTU_HOME": str(tmp_path / "home")})
    assert result.exit_code == 0
    assert result.stdout.splitlines()[0].split() == ["modes.default", "fast"]


def test_config_set_dev_writes_under_the_dev_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEV_HOME", tmp_path / "dev")
    home = {"TONGTU_HOME": str(tmp_path / "home")}
    result = runner.invoke(app, ["config", "set", "pdf.crop_margins", "true", "--dev"], env=home)
    assert result.exit_code == 0
    assert (tmp_path / "dev" / "config.toml").is_file()
    assert not (tmp_path / "home").exists()


def test_config_menu_needs_a_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["config"])
    assert result.exit_code == cli.EXIT_USAGE
    assert "tongtuconfigneedsaterminal" in squeeze(result.stderr)
    assert not path.exists()


UP = "\x1b[A"

DOWN = "\x1b[B"

ENTER = "\r"


def menu_keys(monkeypatch: pytest.MonkeyPatch, keys: list[str]) -> list[str]:
    queue = list(keys)
    monkeypatch.setattr(typer.testing._NamedTextIOWrapper, "isatty", lambda self: True)
    monkeypatch.setattr(typer, "getchar", lambda: queue.pop(0))
    return queue


def menu_index(key: str) -> int:
    return [entry for entry, _value in config_entries(ModelsConfig())].index(key)


def test_config_menu_exits_from_the_last_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    queue = menu_keys(monkeypatch, ["x", UP, ENTER])
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert queue == []
    assert load_config() == (ModelsConfig(), "")
    assert path.is_file()


def test_config_menu_toggles_a_boolean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    queue = menu_keys(monkeypatch, [UP, UP, ENTER, UP, ENTER, DOWN, ENTER])
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert queue == []
    assert loaded_config().pdf.crop_margins is True
    assert "pdf.crop_margins = true" in result.stdout


def test_config_menu_keeps_an_unchanged_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text(TABLE, encoding="utf-8")
    queue = menu_keys(monkeypatch, [UP, UP, ENTER, DOWN, DOWN, ENTER, DOWN, ENTER])
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert queue == []
    assert path.read_text(encoding="utf-8") == TABLE


def test_config_menu_chooses_a_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    queue = menu_keys(monkeypatch, [ENTER, UP, ENTER, UP, ENTER])
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert queue == []
    assert loaded_config().modes.default is ModeName.FAST
    assert "modes.default = fast" in result.stdout


def test_config_menu_keeps_a_text_value_on_enter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text(TABLE, encoding="utf-8")
    queue = menu_keys(monkeypatch, [DOWN, ENTER, UP, UP, ENTER])
    result = runner.invoke(app, ["config"], input="\n")
    assert result.exit_code == 0
    assert queue == []
    assert path.read_text(encoding="utf-8") == TABLE


def test_config_menu_changes_a_text_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    downs = [DOWN] * menu_index("modes.fast.effort")
    queue = menu_keys(monkeypatch, [*downs, ENTER, UP, UP, UP, ENTER])
    result = runner.invoke(app, ["config"], input="high\n")
    assert result.exit_code == 0, result.output
    assert queue == []
    assert loaded_config().modes.fast.effort == "high"
    assert "modes.fast.effort = high" in result.stdout


def test_config_menu_asks_again_after_an_invalid_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    queue = menu_keys(monkeypatch, [DOWN, ENTER, UP, UP, ENTER])
    result = runner.invoke(app, ["config"], input="ghost/m\npi/m\n")
    assert result.exit_code == 0, result.output
    assert queue == []
    assert "modes.fast.model: " in result.stderr
    assert "errors.pydantic.dev" not in result.stderr
    assert loaded_config().modes.fast.model == "pi/m"


def test_config_menu_reports_a_broken_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text("[modes\n", encoding="utf-8")
    menu_keys(monkeypatch, [])
    result = runner.invoke(app, ["config"])
    assert result.exit_code == cli.EXIT_FAILURE
    assert "notvalidTOML" in squeeze(result.stderr)


def recording_editor(tmp_path: Path) -> tuple[Path, Path]:
    record = tmp_path / "editor-arguments"
    editor = tmp_path / "editor"
    editor.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\n', encoding="utf-8")
    editor.chmod(0o755)
    return editor, record


def test_config_open_runs_the_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text(TABLE, encoding="utf-8")
    editor, record = recording_editor(tmp_path)
    monkeypatch.setenv("EDITOR", f"{editor} --wait")
    result = runner.invoke(app, ["config", "open"])
    assert result.exit_code == 0
    assert str(path) in result.stdout
    assert record.read_text(encoding="utf-8").splitlines() == ["--wait", str(path)]
    assert path.read_text(encoding="utf-8") == TABLE


def test_config_open_writes_the_file_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    editor, record = recording_editor(tmp_path)
    monkeypatch.setenv("EDITOR", str(editor))
    result = runner.invoke(app, ["config", "open"])
    assert result.exit_code == 0
    assert record.read_text(encoding="utf-8").splitlines() == [str(path)]
    assert load_config() == (ModelsConfig(), "")


def test_config_open_without_an_editor_launches_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    monkeypatch.setenv("EDITOR", "")
    launched: list[str] = []
    monkeypatch.setattr(typer, "launch", lambda target: launched.append(target) or 0)
    result = runner.invoke(app, ["config", "open"])
    assert result.exit_code == 0
    assert launched == [str(path)]


def test_entry_point_refuses_to_run_inside_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_DISABLE", "1")
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def wire_entries(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    statuses: dict[str, str] | None = None,
    captured: list[RunOptions] | None = None,
) -> None:
    def make(name: str):
        def entry(options: RunOptions, display: cli.StageDisplay) -> Manifest:
            calls.append(name)
            if captured is not None:
                captured.append(options)
            status = (statuses or {}).get(name, "ok")
            return Manifest(status=status, warnings=[], message="失败原因一句话" if status != "ok" else "")

        return entry

    for name in STAGES:
        monkeypatch.setitem(cli.STAGE_ENTRIES, name, make(name))


def test_run_from_scratch_runs_all_stages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 0
    assert calls == list(STAGES)
    header = [line for line in result.stdout.splitlines() if "summary" in line and "elapsed" in line]
    assert len(header) == 1


def test_run_resumes_from_the_first_absent_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert calls == ["mask", "translate", "compile"]
    assert "resumingfrommask" in squeeze(result.stdout)


def test_run_resumes_at_a_failed_stage_whose_outputs_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    write_manifest(workdir.manifest_path("translate"), Manifest(status="translate_failed"))
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert calls == ["translate", "compile"]
    assert (workdir.sandbox("translate") / "zh.tex").is_file()
    assert workdir.zh_pdf.is_file()


def test_run_with_all_outputs_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert calls == []
    assert "allfivestageshavefinishedok" in squeeze(result.stdout)
    assert "--from" in result.stdout


def test_run_from_cleans_downstream_before_rerunning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(workdir.path), "--from", "translate"])
    assert result.exit_code == 0
    assert calls == ["translate", "compile"]
    assert workdir.masked.is_file()
    assert workdir.manifest_path("mask").is_file()
    assert not workdir.sandbox("translate").exists()
    assert not workdir.translated.exists()
    assert not workdir.zh_pdf.exists()
    assert not workdir.manifest_path("translate").exists()
    assert not workdir.manifest_path("compile").exists()
    notes_file = load_manifest(workdir.notes, NotesFile)
    assert notes_file is not None
    assert [note.session for note in notes_file.notes] == ["precompile_fix"]


def test_run_stops_at_the_first_failed_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    wire_entries(monkeypatch, calls, statuses={"mask": "mask_failed"})
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 1
    assert calls == ["fetch", "precompile", "mask"]
    assert "mask_failed" in result.stdout
    assert "失败原因一句话" in result.stdout
    assert str(Path(tmp_path, "paper", "build", "manifests", "mask.json")) in squeeze(result.stdout)


def test_run_passes_the_options_to_the_stage_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    captured: list[RunOptions] = []
    wire_entries(monkeypatch, calls, captured=captured)
    result = runner.invoke(
        app,
        [
            "run",
            "https://arxiv.org/abs/2002.05202v1",
            "--workdir",
            str(tmp_path / "paper"),
            "--glossary",
            "a.json",
            "--glossary",
            "b.json",
            "--mode",
            "fast",
        ],
    )
    assert result.exit_code == 0
    options = captured[0]
    assert (options.paper.arxiv_id, options.paper.version) == ("2002.05202", 1)
    assert options.meta is None
    assert options.workdir.path == tmp_path / "paper"
    assert options.glossary == (Path("a.json"), Path("b.json"))
    assert options.mode == "fast"
    assert all(entry is options for entry in captured)


def test_run_prints_the_workdir_and_the_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    calls: list[str] = []
    captured: list[RunOptions] = []
    wire_entries(monkeypatch, calls, captured=captured)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper"), "--mode", "fast"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0] == f"workdir {tmp_path / 'paper'}"
    assert lines[1] == "mode    fast  codex/m1  low"
    assert captured[0].mode == "fast"


def test_run_takes_the_default_mode_of_the_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text(SHARED_TABLE, encoding="utf-8")
    calls: list[str] = []
    captured: list[RunOptions] = []
    wire_entries(monkeypatch, calls, captured=captured)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 0
    assert result.stdout.splitlines()[1] == "mode    fast  claude-code/m1  low"
    assert captured[0].mode == "fast"


def test_run_without_effort_prints_only_the_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    wire_entries(monkeypatch, [])
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 0
    assert result.stdout.splitlines()[1] == "mode    smart  pi/deepseek/m1"


def test_run_without_a_config_uses_the_default_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    calls: list[str] = []
    captured: list[RunOptions] = []
    wire_entries(monkeypatch, calls, captured=captured)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 0
    mode_line = squeeze(result.stdout.splitlines()[1])
    assert mode_line == "modesmartclaude-code/claude-opus-5-5medium"
    assert calls == list(STAGES)
    assert captured[0].mode == "smart"


def test_run_with_a_broken_config_uses_smart_and_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text("[modes\n", encoding="utf-8")
    calls: list[str] = []
    captured: list[RunOptions] = []
    wire_entries(monkeypatch, calls, captured=captured)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 0
    assert squeeze(result.stdout.splitlines()[1]).startswith("modesmart(configfile")
    assert captured[0].mode == "smart"


def test_run_rejects_an_unknown_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper"), "--mode", "turbo"])
    assert result.exit_code == 2
    assert calls == []


@pytest.mark.parametrize(
    "option", [["--ask-model", "a/b"], ["--work-effort", "low"], ["--jobs", "4"], ["--no-terms"], ["--no-review"]]
)
def test_run_rejects_the_removed_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: list[str]) -> None:
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper"), *option])
    assert result.exit_code == 2
    assert calls == []


@pytest.mark.parametrize("option", [["--ask-model", "a/b"], ["--jobs", "4"], ["--no-terms"]])
def test_stage_rejects_the_removed_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: list[str]) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["stage", "mask", "2002.05202", "--workdir", str(workdir.path), *option])
    assert result.exit_code == 2
    assert calls == []


def test_run_rejects_an_invalid_paper_argument(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "a b", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 2


def test_run_rejects_an_unknown_from_stage(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper"), "--from", "flatten"])
    assert result.exit_code == 2


def test_stage_without_arguments_lists_the_stage_order() -> None:
    result = runner.invoke(app, ["stage"])
    assert result.exit_code == 0
    assert "fetch → precompile → mask → translate → compile" in result.stdout


def test_stage_requires_the_paper_argument() -> None:
    result = runner.invoke(app, ["stage", "mask"])
    assert result.exit_code == 2


def test_stage_refuses_to_run_with_absent_upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["stage", "mask", "2002.05202", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 2
    assert calls == []
    assert not (tmp_path / "paper" / "build" / "manifests" / "mask.json").exists()


def test_stage_runs_only_the_named_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["stage", "mask", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert calls == ["mask"]
    assert [line for line in result.stdout.splitlines() if "summary" in line and "elapsed" in line]


def test_stage_cleans_its_own_outputs_before_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    seen: dict = {}

    def entry(options: RunOptions, display: cli.StageDisplay) -> Manifest:
        seen.update(
            session_dir=options.workdir.sandbox("translate").exists(),
            translated=options.workdir.translated.exists(),
            manifest=options.workdir.manifest_path("translate").exists(),
            mode=options.mode,
        )
        return Manifest(status="ok")

    monkeypatch.setitem(cli.STAGE_ENTRIES, "translate", entry)
    result = runner.invoke(app, ["stage", "translate", "2002.05202", "--workdir", str(workdir.path), "--mode", "fast"])
    assert result.exit_code == 0
    assert seen == {"session_dir": False, "translated": False, "manifest": False, "mode": "fast"}
    assert workdir.zh_pdf.is_file()
    assert workdir.manifest_path("compile").is_file()
    notes_file = load_manifest(workdir.notes, NotesFile)
    assert notes_file is not None
    assert [note.session for note in notes_file.notes] == ["precompile_fix", "compile_fix"]


def test_stage_exits_one_when_the_stage_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    calls: list[str] = []
    wire_entries(monkeypatch, calls, statuses={"mask": "mask_failed"})
    result = runner.invoke(app, ["stage", "mask", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 1
    assert calls == ["mask"]


def wire_marker_probe(
    monkeypatch: pytest.MonkeyPatch, seen: list[RunMarker | None], failure: BaseException | None = None
) -> None:
    def entry(options: RunOptions, display: cli.StageDisplay) -> Manifest:
        seen.append(load_manifest(options.workdir.run_marker, RunMarker))
        if failure is not None:
            raise failure
        return Manifest(status="ok", warnings=[])

    for name in STAGES:
        monkeypatch.setitem(cli.STAGE_ENTRIES, name, entry)


def test_run_keeps_the_run_marker_while_stages_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[RunMarker | None] = []
    wire_marker_probe(monkeypatch, seen)
    workdir = cli.Workdir(tmp_path / "paper")
    previous = signal.getsignal(signal.SIGTERM)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert len(seen) == len(STAGES)
    assert all(marker is not None and marker.pid == os.getpid() for marker in seen)
    assert all(marker is not None and marker.command is RunCommand.RUN for marker in seen)
    assert all(marker is not None and marker.mode == "smart" for marker in seen)
    assert not workdir.run_marker.exists()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_stage_keeps_the_run_marker_while_the_stage_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    seen: list[RunMarker | None] = []
    wire_marker_probe(monkeypatch, seen)
    result = runner.invoke(app, ["stage", "mask", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert [(marker.command, marker.mode) if marker else None for marker in seen] == [(RunCommand.STAGE, "smart")]
    assert not workdir.run_marker.exists()


def test_stage_with_absent_upstream_writes_no_run_marker(tmp_path: Path) -> None:
    result = runner.invoke(app, ["stage", "mask", "2002.05202", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 2
    assert not (tmp_path / "paper").exists()


def test_run_removes_the_run_marker_after_a_failed_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    wire_entries(monkeypatch, calls, statuses={"mask": "mask_failed"})
    workdir = cli.Workdir(tmp_path / "paper")
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(workdir.path)])
    assert result.exit_code == 1
    assert not workdir.run_marker.exists()


@pytest.mark.parametrize("failure", [RuntimeError("程序错误"), KeyboardInterrupt()])
def test_run_removes_the_run_marker_when_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    seen: list[RunMarker | None] = []
    wire_marker_probe(monkeypatch, seen, failure)
    workdir = cli.Workdir(tmp_path / "paper")
    previous = signal.getsignal(signal.SIGTERM)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(workdir.path)])
    assert result.exit_code != 0
    assert len(seen) == 1 and seen[0] is not None
    assert not workdir.run_marker.exists()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_run_leaves_a_marker_written_by_another_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = RunMarker(pid=os.getpid() + 1, started_at=datetime.now(UTC), command=RunCommand.RUN, mode="fast")

    def entry(options: RunOptions, display: cli.StageDisplay) -> Manifest:
        options.workdir.run_marker.write_text(other.model_dump_json(), encoding="utf-8")
        return Manifest(status="ok", warnings=[])

    for name in STAGES:
        monkeypatch.setitem(cli.STAGE_ENTRIES, name, entry)
    workdir = cli.Workdir(tmp_path / "paper")
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert load_manifest(workdir.run_marker, RunMarker) == other


BLOCKING_RUN = """
import sys
import time

from tongtu import cli
from tongtu.artifacts.common import Manifest


def entry(options, display):
    time.sleep(60)
    return Manifest(status="ok", warnings=[])


for name in list(cli.STAGE_ENTRIES):
    cli.STAGE_ENTRIES[name] = entry
cli.app(["run", "2002.05202v1", "--workdir", sys.argv[1]])
"""


def start_blocking_run(workdir: cli.Workdir) -> subprocess.Popen[bytes]:
    child = subprocess.Popen(
        [sys.executable, "-c", BLOCKING_RUN, str(workdir.path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "TONGTU_HOME": str(workdir.path.parent / "home")},
    )
    deadline = time.monotonic() + 60
    while load_manifest(workdir.run_marker, RunMarker) is None:
        if child.poll() is not None or time.monotonic() > deadline:
            child.kill()
            pytest.fail("the blocking run never wrote its run marker")
        time.sleep(0.05)
    return child


def test_sigterm_removes_the_run_marker(tmp_path: Path) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    child = start_blocking_run(workdir)
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert (snapshot.running, snapshot.pid) == (True, child.pid)
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=60) == -signal.SIGTERM
    assert not workdir.run_marker.exists()
    assert status_json("2002.05202", "--workdir", str(workdir.path)).running is False


def test_status_json_treats_a_killed_run_as_not_running(tmp_path: Path) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    child = start_blocking_run(workdir)
    child.kill()
    child.wait(timeout=60)
    assert load_manifest(workdir.run_marker, RunMarker) is not None
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert (snapshot.running, snapshot.pid) == (False, None)


STARTED_AT = datetime.now(UTC).replace(microsecond=0)


def write_run_marker(workdir: cli.Workdir, pid: int, started_at: datetime = STARTED_AT) -> None:
    workdir.path.mkdir(parents=True, exist_ok=True)
    marker = RunMarker(pid=pid, started_at=started_at, command=RunCommand.STAGE, mode="fast")
    workdir.run_marker.write_text(marker.model_dump_json(), encoding="utf-8")


def start_sleeper() -> subprocess.Popen[bytes]:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"], stdout=subprocess.PIPE
    )
    assert child.stdout is not None
    assert child.stdout.readline() == b"ready\n"
    return child


def test_a_run_marker_older_than_its_process_is_not_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    papers = papers_home(tmp_path, monkeypatch)
    workdir = write_paper(papers, "1412.6980v8", arxiv_fetch_manifest("1412.6980", 8), {"precompile": "ok"})
    child = start_sleeper()
    try:
        write_run_marker(workdir, child.pid, datetime.now(UTC) - timedelta(hours=1))
        assert status_json("1412.6980v8").running is False
        summary = summaries_by_name(list_json())["1412.6980v8"]
        assert (summary.state, summary.started_at) == (PaperState.INTERRUPTED, None)
        result = runner.invoke(app, ["stop", "1412.6980v8"])
        assert result.exit_code == 0
        assert result.stdout == "1412.6980v8 is not running\n"
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_a_run_marker_within_the_clock_slack_is_live(tmp_path: Path) -> None:
    child = start_sleeper()
    try:
        workdir = cli.Workdir(tmp_path / "paper")
        slack = timedelta(seconds=cli.MARKER_CLOCK_SLACK_SECONDS - 1)
        write_run_marker(workdir, child.pid, datetime.now(UTC).replace(microsecond=0) - slack)
        assert status_json("2002.05202", "--workdir", str(workdir.path)).pid == child.pid
    finally:
        child.kill()
        child.wait()


def test_a_run_marker_without_a_process_start_time_is_not_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    write_run_marker(workdir, os.getpid())
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", ""))
    assert status_json("2002.05202", "--workdir", str(workdir.path)).running is False


def test_status_json_reports_the_live_pid_of_the_run_marker(tmp_path: Path) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    write_run_marker(workdir, os.getpid())
    write_manifest(workdir.manifest_path("translate"), TranslateManifest(status=TranslateStatus.OK, mode="smart"))
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert (snapshot.running, snapshot.pid) == (True, os.getpid())
    assert (snapshot.started_at, snapshot.mode) == (STARTED_AT, "fast")
    assert workdir.run_marker.exists()


def test_status_json_ignores_a_run_marker_whose_process_is_gone(tmp_path: Path) -> None:
    finished = subprocess.Popen([sys.executable, "-c", ""])
    finished.wait()
    workdir = cli.Workdir(tmp_path / "paper")
    write_run_marker(workdir, finished.pid)
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert (snapshot.running, snapshot.pid, snapshot.started_at, snapshot.mode) == (False, None, None, None)


@pytest.mark.parametrize("content", ["{", '{"pid": 0, "started_at": "2026-01-01T00:00:00Z", "command": "run"}'])
def test_status_json_ignores_an_unreadable_run_marker(tmp_path: Path, content: str) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    workdir.path.mkdir(parents=True)
    workdir.run_marker.write_text(content, encoding="utf-8")
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert (snapshot.running, snapshot.pid) == (False, None)


def test_status_prints_one_row_per_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "400")
    workdir = write_outputs(tmp_path, "fetch")
    mask_path = workdir.manifest_path("mask")
    mask_path.write_text(
        Manifest(status="mask_failed", warnings=["警告一条"], message="哨兵已存在").model_dump_json(),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["status", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    for name in STAGES:
        assert name in output
    assert "mask_failed" in output
    assert "哨兵已存在" in output
    assert "警告一条" in output
    assert "present" in output
    assert "absent" in output
    assert squeeze(str(mask_path.relative_to(workdir.path))) in output
    assert squeeze(str(workdir.manifest_path("fetch").relative_to(workdir.path))) in output


def test_status_does_not_create_the_workdir(tmp_path: Path) -> None:
    target = tmp_path / "absent"
    result = runner.invoke(app, ["status", "2002.05202", "--workdir", str(target)])
    assert result.exit_code == 0
    assert not target.exists()


def status_json(*arguments: str) -> PaperStatus:
    result = runner.invoke(app, ["status", *arguments, "--json"])
    assert result.exit_code == 0
    return PaperStatus.model_validate_json(result.stdout)


def test_status_json_of_an_absent_workdir_marks_every_stage_absent(tmp_path: Path) -> None:
    target = tmp_path / "absent"
    snapshot = status_json("2002.05202", "--workdir", str(target))
    assert snapshot.workdir == target
    assert snapshot.arxiv_id == "2002.05202"
    assert [state.name for state in snapshot.stages] == list(STAGES)
    for state in snapshot.stages:
        assert state.status == ABSENT_STATUS
        assert not state.outputs_present
        assert (state.message, state.warnings, state.manifest) == ("", [], None)
    assert (snapshot.pdf, snapshot.meta, snapshot.glossary, snapshot.notes) == (None, None, None, None)
    assert (snapshot.running, snapshot.pid, snapshot.started_at, snapshot.mode) == (False, None, None, None)
    assert not target.exists()


def test_status_json_reports_each_stage_from_its_manifest(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    workdir.manifest_path("mask").write_text(
        Manifest(status="mask_failed", warnings=["警告一条"], message="哨兵已存在").model_dump_json(),
        encoding="utf-8",
    )
    workdir.manifest_path("translate").write_text("{", encoding="utf-8")
    workdir.precompile_pdf.unlink()
    for path in (workdir.zh_tex, workdir.zh_pdf):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("compile", encoding="utf-8")
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    states = {state.name: state for state in snapshot.stages}
    assert (states["fetch"].status, states["fetch"].outputs_present) == ("ok", True)
    assert states["fetch"].manifest == workdir.manifest_path("fetch")
    assert (states["precompile"].status, states["precompile"].outputs_present) == ("ok", False)
    assert states["mask"].status == "mask_failed"
    assert states["mask"].message == "哨兵已存在"
    assert states["mask"].warnings == ["警告一条"]
    assert not states["mask"].outputs_present
    assert states["mask"].manifest == workdir.manifest_path("mask")
    assert states["translate"].status == ABSENT_STATUS
    assert states["translate"].manifest == workdir.manifest_path("translate")
    assert (states["compile"].status, states["compile"].manifest) == (ABSENT_STATUS, None)
    assert [name for name, state in states.items() if state.outputs_present] == ["fetch", "compile"]
    assert snapshot.pdf == workdir.zh_pdf


def test_status_json_takes_the_mode_of_the_translate_manifest(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile", "mask")
    write_manifest(
        workdir.manifest_path("translate"), TranslateManifest(status=TranslateStatus.TRANSLATE_FAILED, mode="fast")
    )
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert (snapshot.running, snapshot.mode) == (False, "fast")
    assert next(state for state in snapshot.stages if state.name == "translate").status == "translate_failed"


def test_status_json_gives_the_four_output_files_once_they_exist(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    for path in (workdir.meta, workdir.paper_glossary):
        path.write_text("{}", encoding="utf-8")
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert (snapshot.pdf, snapshot.meta, snapshot.glossary, snapshot.notes) == (
        workdir.zh_pdf,
        workdir.meta,
        workdir.paper_glossary,
        workdir.notes,
    )


def test_status_json_gives_the_pdf_once_it_exists(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert snapshot.pdf == workdir.zh_pdf
    assert all(state.status == "ok" and state.outputs_present for state in snapshot.stages)
    workdir.zh_pdf.unlink()
    assert status_json("2002.05202", "--workdir", str(workdir.path)).pdf is None


def test_status_json_of_a_local_source_has_no_arxiv_id(tmp_path: Path) -> None:
    source = tmp_path / "article"
    source.mkdir()
    snapshot = status_json(str(source), "--workdir", str(tmp_path / "paper"))
    assert snapshot.arxiv_id is None


def test_status_json_writes_utf8_bytes(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, "fetch")
    workdir.manifest_path("fetch").write_text(
        Manifest(status="ok", warnings=["中文警告"]).model_dump_json(), encoding="utf-8"
    )
    result = runner.invoke(app, ["status", "2002.05202", "--workdir", str(workdir.path), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout_bytes.decode("utf-8"))["stages"][0]["warnings"] == ["中文警告"]


def test_status_json_keeps_the_usage_error_exit_and_stdout_empty(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "not an id", "--workdir", str(tmp_path / "paper"), "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr


VALIDATE_SOURCE = "We use the large language model with $x$ layers in every experiment.\n"


def test_validate_exits_zero_when_the_check_passes(tmp_path: Path) -> None:
    src = tmp_path / "en.tex"
    dst = tmp_path / "zh.tex"
    src.write_text(VALIDATE_SOURCE, encoding="utf-8")
    dst.write_text("每个实验都使用 $x$ 层的大语言模型。\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(src), str(dst)])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["translated paragraphs 1/1", "check passed"]


def test_validate_exits_one_when_the_check_reports_a_problem(tmp_path: Path) -> None:
    src = tmp_path / "en.tex"
    dst = tmp_path / "zh.tex"
    src.write_text(VALIDATE_SOURCE, encoding="utf-8")
    dst.write_text("每个实验都使用 x 层的大语言模型。\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(src), str(dst)])
    assert result.exit_code == 1
    assert "[structure] paragraph starting at line 1 of the translation, braces_and_math" in result.stdout
    assert "[formula] " in result.stdout


def test_validate_passes_repeated_glossaries_in_order(tmp_path: Path) -> None:
    src = tmp_path / "en.tex"
    dst = tmp_path / "zh.tex"
    src.write_text(VALIDATE_SOURCE, encoding="utf-8")
    dst.write_text("每个实验都使用 $x$ 层的大语言模型。\n", encoding="utf-8")
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"terms": {"large language model": "巨型模型"}}', encoding="utf-8")
    right = tmp_path / "right.json"
    right.write_text('{"terms": {"large language model": "大语言模型"}}', encoding="utf-8")
    failing = runner.invoke(app, ["validate", str(src), str(dst), "--glossary", str(right), "--glossary", str(wrong)])
    assert failing.exit_code == 1
    assert "[glossary] " in failing.stdout
    passing = runner.invoke(app, ["validate", str(src), str(dst), "--glossary", str(wrong), "--glossary", str(right)])
    assert passing.exit_code == 0


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_link_and_id_resolve_to_the_same_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path))
    make_paper_dirs(tmp_path, "2002.05202v1")
    first_lines = []
    for paper in ("2002.05202", "https://arxiv.org/abs/2002.05202"):
        result = runner.invoke(app, ["status", paper])
        assert result.exit_code == 0
        first_lines.append(result.stdout.splitlines()[0])
    assert first_lines[0] == first_lines[1]
    assert str(tmp_path / "papers" / "2002.05202v1") in first_lines[0]


def test_status_dev_uses_the_dev_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEV_HOME", tmp_path / "dev")
    result = runner.invoke(app, ["status", "2002.05202v1", "--dev"], env={"TONGTU_HOME": None})
    assert result.exit_code == 0
    assert str(tmp_path / "dev" / "papers" / "2002.05202v1") in result.stdout.splitlines()[0]


def test_dev_wins_over_tongtu_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEV_HOME", tmp_path / "dev")
    result = runner.invoke(app, ["status", "2002.05202v1", "--dev"], env={"TONGTU_HOME": str(tmp_path / "home")})
    assert result.exit_code == 0
    first_line = result.stdout.splitlines()[0]
    assert str(tmp_path / "dev" / "papers" / "2002.05202v1") in first_line
    assert str(tmp_path / "home") not in first_line


@pytest.fixture
def terminal_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(cli.console), "is_terminal", property(lambda self: True))


def test_stage_display_action_aligns_status_and_summary(terminal_console: None) -> None:
    progress = Progress(disable=True)
    task = progress.add_task("precompile", total=None)
    display = cli.StageDisplay(progress=progress, task=task, name="precompile")
    display.action("fix session", "Read: main.tex")
    expected = f"{'precompile':<{cli.NAME_WIDTH}}{'fix session':<{cli.STATUS_WIDTH}}Read: main.tex"
    width = cli.NAME_WIDTH + cli.STATUS_WIDTH + cli.SUMMARY_WIDTH
    assert progress.tasks[0].description == f"{expected:<{width}}"


def test_stage_display_action_truncates_long_summaries(terminal_console: None) -> None:
    progress = Progress(disable=True)
    task = progress.add_task("precompile", total=None)
    display = cli.StageDisplay(progress=progress, task=task, name="precompile")
    display.action("fix session", "x" * 500)
    description = progress.tasks[0].description
    assert (
        description == f"{'precompile':<{cli.NAME_WIDTH}}{'fix session':<{cli.STATUS_WIDTH}}{'x' * cli.SUMMARY_WIDTH}"
    )
    assert len(description) == cli.NAME_WIDTH + cli.STATUS_WIDTH + cli.SUMMARY_WIDTH


def test_stage_display_action_pads_a_bare_status_to_the_same_width(terminal_console: None) -> None:
    progress = Progress(disable=True)
    task = progress.add_task("precompile", total=None)
    display = cli.StageDisplay(progress=progress, task=task, name="precompile")
    display.action("compiling", "main.tex")
    display.action("fix session")
    description = progress.tasks[0].description
    assert description.rstrip() == f"{'precompile':<{cli.NAME_WIDTH}}fix session"
    assert len(description) == cli.NAME_WIDTH + cli.STATUS_WIDTH + cli.SUMMARY_WIDTH


def test_stage_display_prints_steps_but_not_session_actions_when_not_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(type(cli.console), "is_terminal", property(lambda self: False))
    progress = Progress(disable=True)
    task = progress.add_task("precompile", total=None)
    display = cli.StageDisplay(progress=progress, task=task, name="precompile")
    display.action("compiling", "main.tex")
    display.action("fix session", "running")
    display.action("fix session", "Read: main.tex")
    display.action("fix session", "Edit: main.tex")
    display.action("verifying", "main.tex")
    assert capsys.readouterr().out.splitlines() == [
        "    compiling: main.tex",
        "    fix session: running",
        "    verifying: main.tex",
    ]
    assert progress.tasks[0].description == "precompile"


def test_every_stage_has_an_entry() -> None:
    assert list(cli.STAGE_ENTRIES) == list(STAGES)
    assert cli.STAGE_ENTRIES["compile"] is cli._compile_entry


def test_compile_summary_lists_pages_baseline_and_fix_session() -> None:
    report = CompileReport(
        pages=6,
        pdf_bytes=1,
        overfull_hboxes=0,
        undefined_references=0,
        undefined_citations=0,
        missing_characters=0,
        duration_seconds=1.0,
    )
    baseline = report.model_copy(update={"pages": 5})
    session = Session(stop_reason=StopReason.FINISHED, model="rt/m", duration_seconds=1.0)
    assert cli.stage_summary(CompileManifest(status=CompileStatus.OK)) == ""
    assert cli.stage_summary(CompileManifest(status=CompileStatus.OK, report=report)) == "6 pages"
    assert (
        cli.stage_summary(CompileManifest(status=CompileStatus.OK, report=report, baseline=baseline))
        == "6 pages, baseline 5"
    )
    assert (
        cli.stage_summary(
            CompileManifest(status=CompileStatus.OK, report=report, baseline=baseline, fix_session=session)
        )
        == "6 pages, baseline 5, 1 fix session"
    )


def test_translate_summary_counts_paragraphs_and_new_terms() -> None:
    session = Session(stop_reason=StopReason.FINISHED, model="rt/m", duration_seconds=1.0)
    manifest = TranslateManifest(
        status=TranslateStatus.OK,
        mode="smart",
        session=session,
        paragraphs_total=170,
        paragraphs_translated=164,
        glossary=GlossaryCounts(user=2, learned=5, added=31),
    )
    assert cli.stage_summary(manifest) == "164 paragraphs, 31 new terms"
    reused = manifest.model_copy(update={"session": None})
    assert cli.stage_summary(reused) == "164 paragraphs, 31 new terms, reused existing translation"


@pytest.mark.parametrize(("name", "module"), [("precompile", "precompile"), ("compile", "compile")])
def test_fix_stage_entries_pass_the_workdir_mode_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, module: str
) -> None:
    captured: dict = {}

    def fake_run(workdir, *, mode, report):
        captured.update(workdir=workdir, mode=mode, report=report)
        return CompileManifest(status=CompileStatus.OK)

    monkeypatch.setattr(getattr(cli, module), "run", fake_run)
    workdir = write_outputs(tmp_path, *STAGES[: STAGES.index(name)])
    result = runner.invoke(app, ["stage", name, "2002.05202", "--workdir", str(workdir.path), "--mode", "fast"])
    assert result.exit_code == 0
    assert captured["workdir"].path == workdir.path
    assert captured["mode"] == "fast"
    assert callable(captured["report"])


def test_translate_entry_passes_the_mode_glossaries_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(workdir, *, mode, glossary_files, report):
        captured.update(workdir=workdir, mode=mode, glossary_files=glossary_files, report=report)
        return TranslateManifest(status=TranslateStatus.OK, mode=mode)

    monkeypatch.setattr(cli.translate, "run", fake_run)
    workdir = write_outputs(tmp_path, "fetch", "precompile", "mask")
    result = runner.invoke(
        app, ["stage", "translate", "2002.05202", "--workdir", str(workdir.path), "--glossary", "a.json"]
    )
    assert result.exit_code == 0
    assert captured["workdir"].path == workdir.path
    assert (captured["mode"], captured["glossary_files"]) == ("smart", (Path("a.json"),))
    assert callable(captured["report"])


LATEST_API_URL = "https://export.arxiv.org/api/query?id_list=2106.04426"


def wire_api(monkeypatch: pytest.MonkeyPatch, feed: str = LATEST_FEED, status_code: int = 200) -> list[str]:
    urls: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        urls.append(url)
        return httpx.Response(status_code, text=feed, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    return urls


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def run_with_home(
    home: Path, monkeypatch: pytest.MonkeyPatch, *arguments: str
) -> tuple[object, list[RunOptions], list[str]]:
    calls: list[str] = []
    captured: list[RunOptions] = []
    wire_entries(monkeypatch, calls, captured=captured)
    result = runner.invoke(app, list(arguments), env={"TONGTU_HOME": str(home)})
    return result, captured, calls


def make_paper_dirs(home: Path, *names: str) -> None:
    for name in names:
        (home / "papers" / name).mkdir(parents=True)


def write_fetch_manifest(workdir: Path, arxiv_id: str | None, version: int | None, source: str = "x") -> None:
    manifest = FetchManifest(status=FetchStatus.OK, source=source, arxiv_id=arxiv_id, version=version)
    path = cli.Workdir(workdir).manifest_path("fetch")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(), encoding="utf-8")


def test_run_with_a_version_uses_it_without_asking_arxiv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    make_paper_dirs(tmp_path, "2106.04426v3")
    result, captured, _ = run_with_home(tmp_path, monkeypatch, "run", "https://arxiv.org/abs/2106.04426v2")
    assert result.exit_code == 0
    assert urls == []
    assert captured[0].workdir.path == tmp_path / "papers" / "2106.04426v2"
    assert (captured[0].paper.arxiv_id, captured[0].paper.version, captured[0].meta) == ("2106.04426", 2, None)


def test_run_without_a_version_takes_the_highest_local_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    make_paper_dirs(tmp_path, "2106.04426v1", "2106.04426v2", "2106.04426")
    result, captured, _ = run_with_home(tmp_path, monkeypatch, "run", "2106.04426")
    assert result.exit_code == 0
    assert urls == []
    assert captured[0].workdir.path == tmp_path / "papers" / "2106.04426v2"
    assert (captured[0].paper.version, captured[0].meta) == (2, None)


def test_run_without_a_local_version_asks_arxiv_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    result, captured, calls = run_with_home(tmp_path, monkeypatch, "run", "2106.04426")
    assert result.exit_code == 0
    assert urls == [LATEST_API_URL]
    assert captured[0].workdir.path == tmp_path / "papers" / "2106.04426v3"
    assert captured[0].paper.version == 3
    assert captured[0].meta is not None and captured[0].meta.version == 3
    assert calls == list(STAGES)


def test_run_ignores_the_unversioned_directory_of_v01(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    make_paper_dirs(tmp_path, "2106.04426")
    result, captured, _ = run_with_home(tmp_path, monkeypatch, "run", "2106.04426")
    assert result.exit_code == 0
    assert urls == [LATEST_API_URL]
    assert captured[0].workdir.path == tmp_path / "papers" / "2106.04426v3"


@pytest.mark.parametrize("local", ["2106.04426v1", "2106.04426v3"])
def test_run_latest_asks_arxiv_even_with_a_local_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local: str
) -> None:
    urls = wire_api(monkeypatch)
    make_paper_dirs(tmp_path, local)
    (tmp_path / "papers" / local / "kept").write_text("x", encoding="utf-8")
    result, captured, _ = run_with_home(tmp_path, monkeypatch, "run", "2106.04426", "--latest")
    assert result.exit_code == 0
    assert urls == [LATEST_API_URL]
    assert captured[0].workdir.path == tmp_path / "papers" / "2106.04426v3"
    assert captured[0].meta is not None
    assert (tmp_path / "papers" / local / "kept").is_file()


def test_run_latest_with_a_version_is_a_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    result, _, calls = run_with_home(tmp_path, monkeypatch, "run", "2106.04426v2", "--latest")
    assert result.exit_code == 2
    assert (urls, calls) == ([], [])
    assert not (tmp_path / "papers").exists()


@pytest.mark.parametrize(
    ("feed", "status_code"), [(ERROR_FEED, 200), ("<feed xmlns='http://www.w3.org/2005/Atom'/>", 200), ("", 503)]
)
def test_run_whose_version_query_fails_exits_one_without_a_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feed: str, status_code: int
) -> None:
    urls = wire_api(monkeypatch, feed, status_code)
    result, _, calls = run_with_home(tmp_path, monkeypatch, "run", "2106.04426")
    assert result.exit_code == 1
    assert urls == [LATEST_API_URL]
    assert calls == []
    assert "2106.04426" in result.stderr
    assert not (tmp_path / "papers").exists()


def test_run_with_workdir_takes_the_version_of_its_fetch_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = wire_api(monkeypatch)
    workdir = tmp_path / "chosen"
    write_fetch_manifest(workdir, "2106.04426", 2)
    result, captured, _ = run_with_home(tmp_path, monkeypatch, "run", "2106.04426", "--workdir", str(workdir))
    assert result.exit_code == 0
    assert urls == []
    assert captured[0].workdir.path == workdir
    assert (captured[0].paper.version, captured[0].meta) == (2, None)


@pytest.mark.parametrize("manifest", [None, ("2106.04426", None)])
def test_run_with_workdir_without_a_fetched_version_asks_arxiv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: tuple[str, int | None] | None
) -> None:
    urls = wire_api(monkeypatch)
    workdir = tmp_path / "chosen"
    if manifest is not None:
        write_fetch_manifest(workdir, *manifest)
    result, captured, _ = run_with_home(tmp_path, monkeypatch, "run", "2106.04426", "--workdir", str(workdir))
    assert result.exit_code == 0
    assert urls == [LATEST_API_URL]
    assert captured[0].workdir.path == workdir
    assert captured[0].paper.version == 3


def test_run_of_a_local_source_has_no_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    source = tmp_path / "article"
    source.mkdir()
    result, captured, _ = run_with_home(tmp_path / "home", monkeypatch, "run", str(source))
    assert result.exit_code == 0
    assert urls == []
    assert captured[0].workdir.path == tmp_path / "home" / "papers" / "article"
    assert (captured[0].paper.version, captured[0].meta) == (None, None)


def test_stage_resolves_only_local_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    write_outputs(tmp_path / "papers", "fetch", "precompile").path.rename(tmp_path / "papers" / "2106.04426v2")
    result, captured, calls = run_with_home(tmp_path, monkeypatch, "stage", "mask", "2106.04426")
    assert result.exit_code == 0
    assert urls == []
    assert calls == ["mask"]
    assert captured[0].workdir.path == tmp_path / "papers" / "2106.04426v2"


def test_stage_rejects_the_latest_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    result, _, calls = run_with_home(tmp_path, monkeypatch, "stage", "fetch", "2106.04426", "--latest")
    assert result.exit_code == 2
    assert (urls, calls) == ([], [])


def test_stage_fetch_asks_arxiv_when_no_version_is_known(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    result, captured, calls = run_with_home(tmp_path, monkeypatch, "stage", "fetch", "2106.04426")
    assert result.exit_code == 0
    assert urls == [LATEST_API_URL]
    assert calls == ["fetch"]
    assert captured[0].workdir.path == tmp_path / "papers" / "2106.04426v3"
    assert captured[0].meta is not None and captured[0].meta.version == 3


def test_stage_fetch_reuses_the_local_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    make_paper_dirs(tmp_path, "2106.04426v3")
    result, captured, _ = run_with_home(tmp_path, monkeypatch, "stage", "fetch", "2106.04426")
    assert result.exit_code == 0
    assert urls == []
    assert (captured[0].paper.version, captured[0].meta) == (3, None)


def test_stage_fetch_whose_version_query_fails_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_api(monkeypatch, ERROR_FEED)
    result, _, calls = run_with_home(tmp_path, monkeypatch, "stage", "fetch", "2106.04426")
    assert result.exit_code == 1
    assert calls == []
    assert not (tmp_path / "papers").exists()


def status_with_home(home: Path, *arguments: str) -> PaperStatus:
    result = runner.invoke(app, ["status", *arguments, "--json"], env={"TONGTU_HOME": str(home)})
    assert result.exit_code == 0
    return PaperStatus.model_validate_json(result.stdout)


def test_status_resolves_the_highest_local_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    make_paper_dirs(tmp_path, "1412.6980v8", "1412.6980v9")
    snapshot = status_with_home(tmp_path, "1412.6980")
    assert urls == []
    assert snapshot.workdir == tmp_path / "papers" / "1412.6980v9"
    assert (snapshot.arxiv_id, snapshot.version) == ("1412.6980", 9)


UNVERSIONED_COMMANDS: list[list[str]] = [["status"], ["status", "--json"], ["stage", "precompile"]]


@pytest.mark.parametrize("command", UNVERSIONED_COMMANDS)
@pytest.mark.parametrize("local", [[], ["2106.04426"]])
def test_unversioned_id_without_a_local_versioned_workdir_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str], local: list[str]
) -> None:
    urls = wire_api(monkeypatch)
    for name in local:
        write_outputs(tmp_path / "papers", "fetch").path.rename(tmp_path / "papers" / name)
    result, _, calls = run_with_home(tmp_path, monkeypatch, *command, "2106.04426")
    assert result.exit_code == 1
    assert (urls, calls, result.stdout) == ([], [], "")
    stderr = " ".join(ANSI_ESCAPE_RE.sub("", result.stderr).split())
    assert f"no working directory for 2106.04426 in {tmp_path / 'papers'}" in stderr
    assert "run tongtu run 2106.04426 first" in stderr
    papers = tmp_path / "papers"
    assert (sorted(entry.name for entry in papers.iterdir()) if papers.is_dir() else []) == local


@pytest.mark.parametrize("command", UNVERSIONED_COMMANDS)
def test_unversioned_id_resolves_to_the_local_versioned_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    urls = wire_api(monkeypatch)
    write_outputs(tmp_path / "papers", "fetch").path.rename(tmp_path / "papers" / "2106.04426v2")
    make_paper_dirs(tmp_path, "2106.04426")
    result, captured, calls = run_with_home(tmp_path, monkeypatch, *command, "2106.04426")
    assert result.exit_code == 0
    assert urls == []
    workdir = tmp_path / "papers" / "2106.04426v2"
    if command[0] == "stage":
        assert calls == ["precompile"]
        assert (captured[0].workdir.path, captured[0].paper.version) == (workdir, 2)
    elif "--json" in command:
        assert PaperStatus.model_validate_json(result.stdout).workdir == workdir
    else:
        assert str(workdir) in result.stdout.splitlines()[0]


def test_status_with_workdir_reads_the_version_of_its_fetch_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = wire_api(monkeypatch)
    workdir = tmp_path / "chosen"
    write_fetch_manifest(workdir, "1412.6980", 8)
    snapshot = status_with_home(tmp_path, "1412.6980", "--workdir", str(workdir))
    assert urls == []
    assert (snapshot.workdir, snapshot.version) == (workdir, 8)
    assert status_with_home(tmp_path, "1412.6980v8", "--workdir", str(workdir)).version == 8


MISMATCHED_WORKDIRS: list[tuple[tuple[str | None, int | None], list[str], str, str]] = [
    (("1412.6980", 8), ["2106.04426"], "1412.6980v8", "2106.04426"),
    (("1412.6980", 8), ["1412.6980v9"], "1412.6980v8", "1412.6980v9"),
    (("2106.04426", 2), ["2106.04426", "--latest"], "2106.04426v2", "2106.04426v3"),
    ((None, None), ["2106.04426v3"], "a local source directory", "2106.04426v3"),
    (("2106.04426", 2), ["SOURCE"], "2106.04426v2", "a local source directory"),
]


@pytest.mark.parametrize(
    ("command", "fetched", "arguments", "holds", "asks"),
    [
        (command, *case)
        for command in ("run", "stage", "status")
        for case in MISMATCHED_WORKDIRS
        if command == "run" or "--latest" not in case[1]
    ],
)
def test_workdir_holding_another_paper_is_a_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    fetched: tuple[str | None, int | None],
    arguments: list[str],
    holds: str,
    asks: str,
) -> None:
    wire_api(monkeypatch)
    source = tmp_path / "article"
    source.mkdir()
    workdir = tmp_path / "chosen"
    write_fetch_manifest(workdir, *fetched)
    paper = [str(source) if argument == "SOURCE" else argument for argument in arguments]
    head = ["stage", "fetch"] if command == "stage" else [command]
    result, _, calls = run_with_home(tmp_path / "home", monkeypatch, *head, *paper, "--workdir", str(workdir))
    assert result.exit_code == 2
    assert calls == []
    stderr = " ".join(ANSI_ESCAPE_RE.sub("", result.stderr).replace("│", " ").split())
    assert f"holds {holds}, but the command asks for {asks}" in stderr


def local_source_workdir(tmp_path: Path, *stages: str) -> tuple[Path, Path]:
    source = tmp_path / "article"
    source.mkdir()
    workdir = write_outputs(tmp_path / "home" / "papers", *stages).path
    write_fetch_manifest(workdir, None, None, str(source))
    return source, workdir


@pytest.mark.parametrize("by_name", [True, False])
def test_status_with_workdir_of_a_local_source_accepts_its_name_or_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by_name: bool
) -> None:
    urls = wire_api(monkeypatch)
    source, workdir = local_source_workdir(tmp_path)
    paper = "article" if by_name else str(source)
    snapshot = status_with_home(tmp_path / "home", paper, "--workdir", str(workdir))
    assert urls == []
    assert (snapshot.workdir, snapshot.arxiv_id, snapshot.version) == (workdir, None, None)


@pytest.mark.parametrize("paper", ["OTHER", "draft"])
def test_status_with_workdir_of_a_local_source_rejects_another_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paper: str
) -> None:
    urls = wire_api(monkeypatch)
    _, workdir = local_source_workdir(tmp_path)
    other = tmp_path / "other" / "article"
    other.mkdir(parents=True)
    argument = str(other) if paper == "OTHER" else paper
    result = runner.invoke(
        app, ["status", argument, "--workdir", str(workdir), "--json"], env={"TONGTU_HOME": str(tmp_path / "home")}
    )
    assert result.exit_code == 2
    assert (urls, result.stdout) == ([], "")
    assert "holds a local source directory" in " ".join(ANSI_ESCAPE_RE.sub("", result.stderr).replace("│", " ").split())


@pytest.mark.parametrize("head", [["run"], ["stage", "mask"]])
def test_run_and_stage_with_workdir_of_a_local_source_accept_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, head: list[str]
) -> None:
    urls = wire_api(monkeypatch)
    source, workdir = local_source_workdir(tmp_path, "fetch", "precompile")
    result, captured, _ = run_with_home(tmp_path / "home", monkeypatch, *head, "article", "--workdir", str(workdir))
    assert result.exit_code == 0
    assert urls == []
    assert captured[0].workdir.path == workdir
    assert captured[0].paper.source_dir == source


def test_status_rejects_the_latest_option(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "1412.6980", "--latest"], env={"TONGTU_HOME": str(tmp_path)})
    assert result.exit_code == 2


def test_fetch_summary_starts_with_the_version() -> None:
    remote = FetchManifest(
        status=FetchStatus.OK,
        source="2412.19437v2",
        arxiv_id="2412.19437",
        version=2,
        kind="tar.gz",
        tex_files=["a.tex", "b.tex"],
    )
    local = FetchManifest(status=FetchStatus.OK, source="/src", kind="local", tex_files=["a.tex"])
    assert cli.stage_summary(remote) == "v2, tar.gz, 2 tex files"
    assert cli.stage_summary(local) == "local, 1 tex files"


def test_fetch_entry_passes_the_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple] = []

    def fake_run(paper, workdir, meta=None):
        captured.append((paper, workdir, meta))
        return FetchManifest(status=FetchStatus.OK, source="x")

    monkeypatch.setattr(cli.fetch, "run", fake_run)
    wire_api(monkeypatch, VERSIONED_FEED)
    result = runner.invoke(app, ["stage", "fetch", "1412.6980"], env={"TONGTU_HOME": str(tmp_path)})
    assert result.exit_code == 0
    paper, workdir, meta = captured[0]
    assert (paper.arxiv_id, paper.version) == ("1412.6980", 8)
    assert workdir.path == tmp_path / "papers" / "1412.6980v8"
    assert meta is not None and meta.title == "Adam: A Method for Stochastic Optimization"


def papers_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    return tmp_path / "home" / "papers"


def arxiv_fetch_manifest(arxiv_id: str, version: int | None) -> FetchManifest:
    return FetchManifest(status=FetchStatus.OK, source=arxiv_id, arxiv_id=arxiv_id, version=version)


def paper_meta(title: str) -> PaperMeta:
    return PaperMeta(
        arxiv_id="2412.19437",
        version=2,
        title=title,
        authors=["作者"],
        abstract="摘要",
        primary_category="cs.CL",
        categories=["cs.CL"],
        published=STARTED_AT,
        updated=STARTED_AT,
        comment=None,
        journal_ref=None,
        doi=None,
        abs_url="https://arxiv.org/abs/2412.19437v2",
        pdf_url="https://arxiv.org/pdf/2412.19437v2",
    )


COMPILE_REPORT = CompileReport(
    pages=56,
    pdf_bytes=1,
    overfull_hboxes=0,
    undefined_references=0,
    undefined_citations=0,
    missing_characters=0,
    duration_seconds=1.0,
)


def write_paper(
    papers: Path, name: str, fetched: FetchManifest, statuses: dict[str, str], mtime: float = 1_800_000_000
) -> cli.Workdir:
    workdir = cli.Workdir(papers / name)
    workdir.path.mkdir(parents=True)
    write_manifest(workdir.manifest_path("fetch"), fetched)
    for stage_name, stage_status in statuses.items():
        write_manifest(workdir.manifest_path(stage_name), Manifest(status=stage_status))
    for path in workdir.manifests.iterdir():
        os.utime(path, (mtime, mtime))
    return workdir


ALL_OK = {name: "ok" for name in STAGES[1:]}


def list_json(*arguments: str) -> PaperListing:
    result = runner.invoke(app, ["list", *arguments, "--json"])
    assert result.exit_code == 0
    return PaperListing.model_validate_json(result.stdout)


def summaries_by_name(listing: PaperListing) -> dict[str, PaperSummary]:
    return {summary.name: summary for summary in listing.papers}


def exited_pid() -> int:
    finished = subprocess.Popen([sys.executable, "-c", ""])
    finished.wait()
    return finished.pid


def test_list_json_without_a_papers_directory_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    papers_home(tmp_path, monkeypatch)
    assert list_json().papers == []


def test_list_json_of_an_empty_papers_directory_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    papers_home(tmp_path, monkeypatch).mkdir(parents=True)
    assert list_json().papers == []


def write_finished_paper(papers: Path) -> cli.Workdir:
    workdir = write_paper(papers, "2412.19437v2", arxiv_fetch_manifest("2412.19437", 2), {})
    for name in ("precompile", "mask"):
        write_manifest(workdir.manifest_path(name), Manifest(status="ok"))
    usage = Usage(turns=3, input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0, cost_usd=4.25)
    session = Session(stop_reason=StopReason.FINISHED, model="rt/m", duration_seconds=595.5, usage=usage)
    write_manifest(
        workdir.manifest_path("translate"), TranslateManifest(status=TranslateStatus.OK, mode="smart", session=session)
    )
    baseline = COMPILE_REPORT.model_copy(update={"pages": 66})
    write_manifest(
        workdir.manifest_path("compile"),
        CompileManifest(status=CompileStatus.OK, report=COMPILE_REPORT, baseline=baseline),
    )
    write_manifest(workdir.meta, paper_meta("DeepSeek-V3 技术报告"))
    workdir.zh_pdf.write_bytes(b"%PDF")
    return workdir


def test_list_json_summarizes_a_finished_paper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_finished_paper(papers_home(tmp_path, monkeypatch))
    fetch_time = 1_700_000_000
    os.utime(workdir.manifest_path("fetch"), (fetch_time, fetch_time))
    latest_time = 1_900_000_000
    os.utime(workdir.manifest_path("compile"), (latest_time, latest_time))
    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0
    assert result.stdout.count("\n") == 1
    raw = json.loads(result.stdout)["papers"][0]
    assert set(raw) == set(PaperSummary.model_fields)
    assert "abstract" not in result.stdout
    summary = PaperListing.model_validate_json(result.stdout).papers[0]
    assert summary == PaperSummary(
        name="2412.19437v2",
        workdir=workdir.path,
        state=PaperState.OK,
        stage=None,
        arxiv_id="2412.19437",
        version=2,
        title="DeepSeek-V3 技术报告",
        primary_category="cs.CL",
        added_at=datetime.fromtimestamp(fetch_time, UTC),
        updated_at=datetime.fromtimestamp(latest_time, UTC),
        started_at=None,
        pdf=workdir.zh_pdf,
    )


def test_status_json_gives_the_details_of_a_finished_paper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_finished_paper(papers_home(tmp_path, monkeypatch))
    snapshot = status_json("2412.19437v2")
    assert (snapshot.state, snapshot.stage) == (PaperState.OK, None)
    assert (snapshot.pages, snapshot.source_pages) == (56, 66)
    assert (snapshot.translate_seconds, snapshot.translate_cost_usd) == (595.5, 4.25)
    assert snapshot.recent_actions == []


def test_list_and_status_report_the_failed_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    papers = papers_home(tmp_path, monkeypatch)
    statuses = {"precompile": "ok", "mask": "mask_failed"}
    write_paper(papers, "1412.6980v8", arxiv_fetch_manifest("1412.6980", 8), statuses)
    summary = summaries_by_name(list_json())["1412.6980v8"]
    assert (summary.state, summary.stage) == (PaperState.FAILED, "mask")
    assert (summary.title, summary.primary_category, summary.pdf, summary.started_at) == (None, None, None, None)
    snapshot = status_json("1412.6980v8")
    assert (snapshot.state, snapshot.stage) == (PaperState.FAILED, "mask")
    assert (snapshot.pages, snapshot.source_pages) == (None, None)
    assert (snapshot.translate_seconds, snapshot.translate_cost_usd) == (None, None)


def test_list_and_status_report_an_interrupted_paper_at_the_first_absent_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    papers = papers_home(tmp_path, monkeypatch)
    write_paper(papers, "1412.6980v8", arxiv_fetch_manifest("1412.6980", 8), {"precompile": "ok", "translate": "ok"})
    summary = summaries_by_name(list_json())["1412.6980v8"]
    assert (summary.state, summary.stage) == (PaperState.INTERRUPTED, "mask")
    snapshot = status_json("1412.6980v8")
    assert (snapshot.state, snapshot.stage) == (PaperState.INTERRUPTED, "mask")


def write_events(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(line.encode("utf-8", errors="surrogateescape") + b"\n" for line in lines))


def tool_event(summary: object) -> str:
    return json.dumps({"type": "tool", "summary": summary}, ensure_ascii=False)


def test_status_json_reports_a_running_paper_with_its_recent_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    papers = papers_home(tmp_path, monkeypatch)
    statuses = {"precompile": "ok", "mask": "ok"}
    workdir = write_paper(papers, "1412.6980v8", arxiv_fetch_manifest("1412.6980", 8), statuses)
    write_run_marker(workdir, os.getpid())
    actions = [f"读取第 {index} 段" for index in range(10)]
    events = [tool_event(action) for action in actions[:5]]
    events += ["{", json.dumps({"type": "usage"}), tool_event(7), json.dumps(["tool"]), "\udcff"]
    events += [tool_event(action) for action in actions[5:]]
    write_events(workdir.translate_log, events)
    write_events(workdir.compile_fix_log, [tool_event("另一个会话")])
    snapshot = status_json("1412.6980v8")
    assert (snapshot.state, snapshot.stage) == (PaperState.RUNNING, "translate")
    assert snapshot.recent_actions == actions[-cli.RECENT_ACTIONS :]
    assert (snapshot.running, snapshot.pid) == (True, os.getpid())
    summary = summaries_by_name(list_json())["1412.6980v8"]
    assert (summary.state, summary.stage, summary.started_at) == (PaperState.RUNNING, "translate", STARTED_AT)


def test_status_json_gives_no_recent_actions_without_a_session_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    papers = papers_home(tmp_path, monkeypatch)
    workdir = write_paper(papers, "1412.6980v8", arxiv_fetch_manifest("1412.6980", 8), {"precompile": "ok"})
    write_run_marker(workdir, os.getpid())
    snapshot = status_json("1412.6980v8")
    assert (snapshot.state, snapshot.stage, snapshot.recent_actions) == (PaperState.RUNNING, "mask", [])


def test_status_json_gives_no_recent_actions_for_a_paper_that_is_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    papers = papers_home(tmp_path, monkeypatch)
    statuses = {"precompile": "ok", "mask": "ok"}
    workdir = write_paper(papers, "1412.6980v8", arxiv_fetch_manifest("1412.6980", 8), statuses)
    write_events(workdir.translate_log, [tool_event("读取原文")])
    snapshot = status_json("1412.6980v8")
    assert (snapshot.state, snapshot.recent_actions) == (PaperState.INTERRUPTED, [])


def test_list_json_selects_the_directories_of_papers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    papers = papers_home(tmp_path, monkeypatch)
    write_paper(papers, "2002.05202v1", arxiv_fetch_manifest("2002.05202", 1), ALL_OK)
    write_paper(papers, "2002.05202", arxiv_fetch_manifest("2002.05202", None), ALL_OK)
    write_paper(papers, "2002.05202v2", arxiv_fetch_manifest("2002.05202", 1), ALL_OK)
    write_paper(papers, "hep-th_9901001v1", arxiv_fetch_manifest("hep-th/9901001", 1), ALL_OK)
    local = FetchManifest(status=FetchStatus.OK, source=str(tmp_path / "my-paper"), kind="local")
    write_paper(papers, "my-paper", local, ALL_OK)
    running = cli.Workdir(papers / "1412.6980v8")
    write_run_marker(running, os.getpid())
    (papers / "unrelated").mkdir()
    write_run_marker(cli.Workdir(papers / "stale"), exited_pid())
    broken = cli.Workdir(papers / "broken")
    broken.manifests.mkdir(parents=True)
    broken.manifest_path("fetch").write_text("{", encoding="utf-8")
    (papers / "a-file").write_text("", encoding="utf-8")
    summaries = summaries_by_name(list_json())
    assert set(summaries) == {"2002.05202v1", "hep-th_9901001v1", "my-paper", "1412.6980v8"}
    assert (summaries["my-paper"].arxiv_id, summaries["my-paper"].state) == (None, PaperState.OK)
    assert (summaries["hep-th_9901001v1"].arxiv_id, summaries["hep-th_9901001v1"].version) == ("hep-th/9901001", 1)
    fresh = summaries["1412.6980v8"]
    assert (fresh.state, fresh.stage, fresh.arxiv_id) == (PaperState.RUNNING, "fetch", None)
    directory_time = datetime.fromtimestamp(running.path.stat().st_mtime, UTC)
    assert (fresh.added_at, fresh.updated_at) == (directory_time, directory_time)


def test_list_json_sorts_by_the_latest_manifest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    papers = papers_home(tmp_path, monkeypatch)
    write_paper(papers, "1412.6980v8", arxiv_fetch_manifest("1412.6980", 8), ALL_OK, mtime=1_700_000_000)
    write_paper(papers, "2002.05202v1", arxiv_fetch_manifest("2002.05202", 1), ALL_OK, mtime=1_900_000_000)
    write_paper(papers, "1701.06538v1", arxiv_fetch_manifest("1701.06538", 1), ALL_OK, mtime=1_800_000_000)
    assert [summary.name for summary in list_json().papers] == ["2002.05202v1", "1701.06538v1", "1412.6980v8"]


def test_list_prints_one_row_per_paper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "400")
    papers = papers_home(tmp_path, monkeypatch)
    finished = write_paper(papers, "2412.19437v2", arxiv_fetch_manifest("2412.19437", 2), ALL_OK)
    write_manifest(finished.meta, paper_meta("DeepSeek-V3 技术报告"))
    write_paper(papers, "1412.6980v8", arxiv_fetch_manifest("1412.6980", 8), {"precompile": "precompile_failed"})
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    rows = [line.split() for line in result.stdout.splitlines()]
    assert rows[0] == ["paper", "state", "stage", "title"]
    assert sorted(rows[1:]) == [
        ["1412.6980v8", "failed", "precompile"],
        ["2412.19437v2", "ok", cli.ABSENT_CELL, "DeepSeek-V3", "技术报告"],
    ]


def test_list_dev_reads_the_dev_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEV_HOME", tmp_path / "dev")
    write_paper(tmp_path / "dev" / "papers", "1412.6980v8", arxiv_fetch_manifest("1412.6980", 8), ALL_OK)
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    assert [summary.name for summary in list_json("--dev").papers] == ["1412.6980v8"]


def test_stop_of_a_paper_that_is_not_running(tmp_path: Path) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    write_run_marker(workdir, exited_pid())
    result = runner.invoke(app, ["stop", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert result.stdout == "paper is not running\n"


def test_stop_ends_a_running_paper(tmp_path: Path) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    child = start_blocking_run(workdir)
    try:
        result = runner.invoke(app, ["stop", "2002.05202", "--workdir", str(workdir.path)])
        assert result.exit_code == 0
        assert result.stdout == "stopped paper\n"
        assert child.wait(timeout=60) == -signal.SIGTERM
    finally:
        child.kill()
        child.wait()
    assert not workdir.run_marker.exists()


IGNORE_SIGTERM = """\
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(60)
"""


def test_stop_gives_up_on_a_process_that_ignores_sigterm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "STOP_WAIT_SECONDS", 0.3)
    child = subprocess.Popen([sys.executable, "-c", IGNORE_SIGTERM], stdout=subprocess.PIPE)
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == b"ready\n"
        workdir = cli.Workdir(tmp_path / "paper")
        write_run_marker(workdir, child.pid, datetime.now(UTC))
        result = runner.invoke(app, ["stop", "2002.05202", "--workdir", str(workdir.path)])
        assert result.exit_code == cli.EXIT_FAILURE
        assert child.poll() is None
        assert f"paper (pid {child.pid}) did not stop within" in result.stderr
        assert workdir.run_marker.exists()
    finally:
        child.kill()
        child.wait()


def test_stop_treats_a_vanished_process_as_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    write_run_marker(workdir, os.getpid())

    def vanished(pid: int, signum: int) -> None:
        if signum == signal.SIGTERM:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(cli.os, "kill", vanished)
    result = runner.invoke(app, ["stop", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert result.stdout == "stopped paper\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root may signal pid 1, so the test would terminate it")
def test_stop_reports_a_process_of_another_user(tmp_path: Path) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    write_run_marker(workdir, 1)
    result = runner.invoke(app, ["stop", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == cli.EXIT_FAILURE
    assert result.stdout == ""
    assert result.stderr.startswith("paper (pid 1) ")
    assert len(result.stderr.splitlines()) == 1
    assert workdir.run_marker.exists()


GROUP_RUN = """
import sys

from tongtu import cli, processes
from tongtu.artifacts.common import Manifest

GRANDCHILD = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"


def entry(options, display):
    processes.run_in_process_group([sys.executable, "-c", GRANDCHILD, sys.argv[2]], options.workdir.path, 60.0)
    return Manifest(status="ok", warnings=[])


for name in list(cli.STAGE_ENTRIES):
    cli.STAGE_ENTRIES[name] = entry
cli.app(["run", "2002.05202v1", "--workdir", sys.argv[1]])
"""


def process_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_stop_ends_the_process_group_of_the_running_stage(tmp_path: Path) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    pid_file = tmp_path / "grandchild.pid"
    child = subprocess.Popen(
        [sys.executable, "-c", GROUP_RUN, str(workdir.path), str(pid_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "TONGTU_HOME": str(tmp_path / "home")},
    )
    try:
        deadline = time.monotonic() + 60
        while not (pid_file.is_file() and pid_file.read_text(encoding="utf-8")):
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        grandchild = int(pid_file.read_text(encoding="utf-8"))
        result = runner.invoke(app, ["stop", "2002.05202", "--workdir", str(workdir.path)])
        assert result.exit_code == 0
        assert child.wait(timeout=60) == -signal.SIGTERM
        deadline = time.monotonic() + 10
        while not process_gone(grandchild):
            assert time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        child.kill()
        child.wait()


def test_config_list_json_gives_every_key_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text(TABLE.replace("timeout_seconds = 60", "timeout_seconds = 90.5"), encoding="utf-8")
    result = runner.invoke(app, ["config", "list", "--json"])
    assert result.exit_code == 0
    values = json.loads(result.stdout)
    assert list(values) == [key for key, _value in config_entries(loaded_config())]
    assert values["modes.default"] == "smart"
    assert values["modes.smart.timeout_seconds"] == 90.5
    assert values["modes.fast.timeout_seconds"] == 3600
    assert values["pdf.crop_margins"] is False
    assert values["fonts.mono"] is None
    assert isinstance(values["fonts.main.regular"], str)


def counted_writes(monkeypatch: pytest.MonkeyPatch) -> list[ModelsConfig]:
    writes: list[ModelsConfig] = []

    def write(config: ModelsConfig) -> Path:
        writes.append(config)
        return write_config(config)

    monkeypatch.setattr(cli, "write_config", write)
    return writes


def test_config_set_writes_several_keys_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    write_config(ModelsConfig())
    writes = counted_writes(monkeypatch)
    pairs = ["modes.default", "fast", "pdf.crop_margins", "true", "modes.fast.effort", "high"]
    result = runner.invoke(app, ["config", "set", *pairs])
    assert result.exit_code == 0
    assert result.stdout == "modes.default = fast\npdf.crop_margins = true\nmodes.fast.effort = high\n"
    assert len(writes) == 1
    config = loaded_config()
    assert (config.modes.default, config.pdf.crop_margins, config.modes.fast.effort) == (ModeName.FAST, True, "high")


@pytest.mark.parametrize(
    ("pairs", "message"),
    [
        (["pdf.crop_margins", "true", "modes.fast.jobs", "4"], "configkeymodes.fast.jobsdoesnotexist"),
        (["pdf.crop_margins", "true", "modes.fast.model", "ghost/m"], "modes.fast.model:"),
    ],
)
def test_config_set_writes_nothing_when_one_pair_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pairs: list[str], message: str
) -> None:
    path = written_config(tmp_path, monkeypatch)
    result = runner.invoke(app, ["config", "set", *pairs])
    assert result.exit_code == cli.EXIT_USAGE
    assert message in squeeze(result.stderr)
    assert path.read_text(encoding="utf-8") == TABLE


@pytest.mark.parametrize("pairs", [["pdf.crop_margins", "true", "modes.default"], []])
def test_config_set_rejects_an_incomplete_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pairs: list[str]
) -> None:
    path = written_config(tmp_path, monkeypatch)
    result = runner.invoke(app, ["config", "set", *pairs])
    assert result.exit_code == cli.EXIT_USAGE
    assert path.read_text(encoding="utf-8") == TABLE
