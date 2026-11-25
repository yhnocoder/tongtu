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
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from rich.progress import Progress
from typer.testing import CliRunner

from tongtu import __version__, cli
from tongtu.artifacts.common import CompileReport, Manifest, NotesFile, Session, StopReason
from tongtu.artifacts.compile import CompileManifest, CompileStatus
from tongtu.artifacts.fetch import FetchManifest, FetchStatus
from tongtu.artifacts.status import ABSENT_STATUS, PaperStatus, RunCommand, RunMarker
from tongtu.artifacts.translate import GlossaryCounts, TranslateManifest, TranslateStatus
from tongtu.cli import RunOptions, app, main
from tongtu.manifests import load_manifest, write_manifest
from tongtu.model.config import CONFIG_TEMPLATE
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


def test_setup_writes_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert path.read_text(encoding="utf-8") == CONFIG_TEMPLATE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_setup_does_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text("手改过的配置\n", encoding="utf-8")
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert path.read_text(encoding="utf-8") == "手改过的配置\n"
    assert "notoverwriting" in squeeze(result.stdout)


def test_setup_rejects_the_interactive_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup", "-i"])
    assert result.exit_code == 2
    assert not path.exists()


def test_doctor_without_config_exits_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    output = squeeze(result.stdout)
    assert "tongtusetup" in output
    assert "toolchainandfontscomplete" in output
    assert "fetch,precompileandmaskstillcan" in output
    assert calls == [[cli.XELATEX, "--version"]]


def test_doctor_with_unreadable_config_exits_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text("[modes\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED


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
    config_path(tmp_path, monkeypatch)
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


def test_setup_dev_writes_under_the_dev_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEV_HOME", tmp_path / "dev")
    result = runner.invoke(app, ["setup", "--dev"], env={"TONGTU_HOME": str(tmp_path / "home")})
    assert result.exit_code == 0
    assert (tmp_path / "dev" / "config.toml").read_text(encoding="utf-8") == CONFIG_TEMPLATE
    assert not (tmp_path / "home").exists()


def test_setup_keeps_the_path_on_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    monkeypatch.setenv("COLUMNS", "60")
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert [line for line in result.stdout.splitlines() if "wrote" in line and str(path) in line]


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


def test_run_without_a_config_uses_smart_and_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    calls: list[str] = []
    captured: list[RunOptions] = []
    wire_entries(monkeypatch, calls, captured=captured)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 0
    mode_line = squeeze(result.stdout.splitlines()[1])
    assert mode_line.startswith("modesmart(cannotreadconfigfile")
    assert calls == list(STAGES)
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


STARTED_AT = datetime(2026, 11, 25, 8, 30, tzinfo=UTC)


def write_run_marker(workdir: cli.Workdir, pid: int) -> None:
    workdir.path.mkdir(parents=True, exist_ok=True)
    marker = RunMarker(pid=pid, started_at=STARTED_AT, command=RunCommand.STAGE, mode="fast")
    workdir.run_marker.write_text(marker.model_dump_json(), encoding="utf-8")


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


def write_fetch_manifest(workdir: Path, arxiv_id: str | None, version: int | None) -> None:
    manifest = FetchManifest(status=FetchStatus.OK, source="x", arxiv_id=arxiv_id, version=version)
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


def test_workdir_of_a_local_source_accepts_the_same_kind_of_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "article"
    source.mkdir()
    workdir = tmp_path / "chosen"
    write_fetch_manifest(workdir, None, None)
    result, captured, _ = run_with_home(tmp_path / "home", monkeypatch, "run", str(source), "--workdir", str(workdir))
    assert result.exit_code == 0
    assert captured[0].workdir.path == workdir


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
