from __future__ import annotations

import json
import os
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
from tongtu.artifacts.common import CompileReport, FixSession, Manifest
from tongtu.artifacts.compile import CompileManifest, CompileStatus
from tongtu.artifacts.fetch import FetchManifest, FetchStatus
from tongtu.artifacts.status import ABSENT_STATUS, ChunkCounts, PaperStatus, RunCommand, RunMarker
from tongtu.artifacts.survey import BriefFile, ChunkRecord, Part
from tongtu.artifacts.translate import ChunkTranslateRecord, ChunkTranslateStatus, TranslateManifest, TranslateStatus
from tongtu.cli import RunOptions, app, main
from tongtu.manifests import load_manifest
from tongtu.model.config import CONFIG_TEMPLATE
from tongtu.pipeline import STAGES
from tongtu.stages.translate import ChunkProgress, ChunkProgressState

from .test_arxiv import ERROR_FEED, LATEST_FEED, VERSIONED_FEED
from .test_pipeline import write_outputs

runner = CliRunner()

TABLE = """
[provider.demo]
base_url = "https://demo.example/v1"
api_key_env = "DEMO_KEY"
api = "chat"

[provider.written]
base_url = "https://written.example/v1"
api_key = "written-key"
api = "chat"

[provider.unused]
base_url = "https://unused.example/v1"
api_key_env = "UNUSED_KEY"
api = "chat"

[roles]
translate = { model = "demo/m1", effort = "low" }
survey_terms = { model = "written/m1", effort = "low" }
review = { model = "codex/m1", effort = "low", timeout_seconds = 60 }
precompile_fix = { model = "claude-code/m1", effort = "low", timeout_seconds = 60 }
compile_fix = { model = "pi/deepseek/m1", timeout_seconds = 60 }
"""


KEYLESS_TABLE = """
[provider.demo]
base_url = "https://demo.example/v1"
api = "chat"

[roles]
translate = { model = "demo/m1", effort = "low" }
"""


BROKEN_ROLE_TABLE = """
[provider.demo]
base_url = "https://demo.example/v1"
api_key = "demo-key"
api = "chat"

[roles]
translate = { model = "demo/m1", effort = "low" }
review = { model = "ghost/m1", effort = "low", timeout_seconds = 60 }
compile_fix = { model = "demo", timeout_seconds = 60 }
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
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    output = squeeze(result.stdout)
    assert "tongtusetup" in output
    assert "toolchainandfontscomplete" in output


def test_doctor_with_unreadable_config_exits_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch).write_text("[roles\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED


def test_doctor_missing_fonts_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
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
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: None if name == tool else f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_FAILURE


def test_doctor_missing_toolchain_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    output = squeeze(result.stdout)
    assert "environmentincomplete:xelatex" in output
    assert "[missing]TeXLive" in output
    assert "xelatexisnotinPATH;cannotcheck" in output


def test_doctor_accepts_the_required_texlive_year(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "[ok]TeXLive" in output
    assert "(TeXLive2026)" in output


def test_doctor_rejects_an_older_texlive_year(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
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
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_WITHOUT_YEAR)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    output = squeeze(result.stdout)
    assert "[missing]TeXLive" in output
    assert "(MiKTeX24.1)" in output


def test_doctor_lists_only_referenced_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "environmentcomplete." in output
    assert "keydemo" in output
    assert output.count("keywritten") == 1
    assert "keyunused" not in output
    assert "environmentvariableDEMO_KEY" in output
    assert "api_keyinconfig.toml" in output
    assert "providers,chunking,fontsandroles" in output


def test_doctor_checks_each_referenced_runtime_once_through_agentrun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    for backend, name in (("codex", "codex/m1"), ("claude-code", "claude-code/m1"), ("pi", "pi/deepseek/m1")):
        assert f"[ok]{name}agentrundoctor:executable/usr/bin/{backend}" in output
        assert f"[ok]{name}agentrundoctor:login{backend}loggedin" in output
        assert f"[ok]{name}agentrundoctor:networkskipped:--networknone" in output
    assert ["/usr/bin/agentrun", "doctor", "pi", "--json", "--model", "deepseek/m1"] in calls
    assert sum(1 for command in calls if command[1:3] == ["doctor", "codex"]) == 1


def test_doctor_reports_a_failed_agentrun_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026, failing={"codex": "codex login file not found"})
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    output = squeeze(result.stdout)
    assert "[missing]codex/m1agentrundoctor:logincodexloginfilenotfound" in output
    assert "[ok]claude-code/m1agentrundoctor:login" in output
    assert "codex/m1notconfigured" in output


def test_doctor_reports_agentrun_missing_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "agentrun" else f"/usr/bin/{name}")
    calls = fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    output = squeeze(result.stdout)
    assert "[missing]pi/deepseek/m1agentrun,whichstartseveryruntimeagentrunnotfoundinPATH" in output
    assert calls == [[cli.XELATEX, "--version"]]
    assert "toolchainandfontscomplete" in output


def test_doctor_reports_agentrun_output_that_is_not_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text('[roles]\nreview = { model = "pi", timeout_seconds = 60 }\n', encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == [cli.XELATEX, "--version"]:
            return subprocess.CompletedProcess(command, 0, VERSION_2026, "")
        return subprocess.CompletedProcess(command, 2, "", "agentrun: unexpected argument\n")

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    assert "[missing]piagentrundoctoragentrun:unexpectedargument" in squeeze(result.stdout)


def test_doctor_reports_roles_that_do_not_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text(BROKEN_ROLE_TABLE, encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    output = squeeze(result.stdout)
    assert "[ok]keydemo" in output
    assert "[missing]rolereview" in output
    assert "providerghost" in output
    assert "[missing]rolecompile_fix" in output
    assert "backend/model" in output
    assert "rolereview,rolecompile_fixnotconfigured" in output


def test_doctor_reports_missing_key_as_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.delenv("DEMO_KEY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    output = squeeze(result.stdout)
    assert "toolchainandfontscomplete" in output
    assert "keydemo" in output


def test_doctor_keeps_table_names_in_the_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text(KEYLESS_TABLE, encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    assert "[provider.demo]" in result.stdout


def note_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.lstrip().startswith("note")]


def test_doctor_notes_legacy_directories_without_changing_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
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
    monkeypatch.setenv("DEMO_KEY", "demo-key")
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
    monkeypatch.setenv("DEMO_KEY", "demo-key")
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
    assert calls == ["mask", "survey", "translate", "review", "compile"]
    assert "resumingfrommask" in squeeze(result.stdout)


def test_run_with_all_outputs_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert calls == []
    assert "--from" in result.stdout


def test_run_from_cleans_downstream_before_rerunning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(workdir.path), "--from", "review"])
    assert result.exit_code == 0
    assert calls == ["review", "compile"]
    assert (workdir.build / "translated" / "c000.tex").is_file()
    assert workdir.manifest_path("translate").is_file()
    assert not (workdir.build / "reviewed").exists()
    assert not workdir.zh_pdf.exists()
    assert not workdir.manifest_path("review").exists()
    assert not workdir.manifest_path("compile").exists()


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
            "--jobs",
            "9",
            "--no-terms",
            "--no-review",
        ],
    )
    assert result.exit_code == 0
    options = captured[0]
    assert (options.paper.arxiv_id, options.paper.version) == ("2002.05202", 1)
    assert options.meta is None
    assert options.workdir.path == tmp_path / "paper"
    assert options.glossary == (Path("a.json"), Path("b.json"))
    assert options.jobs == 9
    assert options.no_terms is True
    assert options.no_review is True
    assert all(entry is options for entry in captured)


@pytest.mark.parametrize("option", ["--ask-model", "--ask-effort", "--work-model", "--work-effort"])
def test_run_rejects_the_removed_model_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str) -> None:
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202v1", "--workdir", str(tmp_path / "paper"), option, "a/b"])
    assert result.exit_code == 2
    assert calls == []


@pytest.mark.parametrize("option", ["--ask-model", "--work-model"])
def test_stage_rejects_the_removed_model_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["stage", "mask", "2002.05202", "--workdir", str(workdir.path), option, "a/b"])
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
    assert "fetch → precompile → mask → survey → translate → review → compile" in result.stdout


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
    assert not workdir.run_marker.exists()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_stage_keeps_the_run_marker_while_the_stage_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    seen: list[RunMarker | None] = []
    wire_marker_probe(monkeypatch, seen)
    result = runner.invoke(app, ["stage", "mask", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert [marker.command if marker else None for marker in seen] == [RunCommand.STAGE]
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
    other = RunMarker(pid=os.getpid() + 1, started_at=datetime.now(UTC), command=RunCommand.RUN)

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


def write_run_marker(workdir: cli.Workdir, pid: int) -> None:
    workdir.path.mkdir(parents=True, exist_ok=True)
    marker = RunMarker(pid=pid, started_at=datetime.now(UTC), command=RunCommand.STAGE)
    workdir.run_marker.write_text(marker.model_dump_json(), encoding="utf-8")


def test_status_json_reports_the_live_pid_of_the_run_marker(tmp_path: Path) -> None:
    workdir = cli.Workdir(tmp_path / "paper")
    write_run_marker(workdir, os.getpid())
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert (snapshot.running, snapshot.pid) == (True, os.getpid())
    assert workdir.run_marker.exists()


def test_status_json_ignores_a_run_marker_whose_process_is_gone(tmp_path: Path) -> None:
    finished = subprocess.Popen([sys.executable, "-c", ""])
    finished.wait()
    workdir = cli.Workdir(tmp_path / "paper")
    write_run_marker(workdir, finished.pid)
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert (snapshot.running, snapshot.pid) == (False, None)


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
    assert snapshot.translate_chunks is None
    assert snapshot.pdf is None
    assert (snapshot.running, snapshot.pid) == (False, None)
    assert not target.exists()


def test_status_json_reports_each_stage_from_its_manifest(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    workdir.manifest_path("mask").write_text(
        Manifest(status="mask_failed", warnings=["警告一条"], message="哨兵已存在").model_dump_json(),
        encoding="utf-8",
    )
    workdir.manifest_path("survey").write_text("{", encoding="utf-8")
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
    assert states["survey"].status == ABSENT_STATUS
    assert states["survey"].manifest == workdir.manifest_path("survey")
    for name in ("translate", "review", "compile"):
        assert (states[name].status, states[name].manifest) == (ABSENT_STATUS, None)
    assert [name for name, state in states.items() if state.outputs_present] == ["fetch", "compile"]
    assert snapshot.pdf == workdir.zh_pdf


def test_status_json_counts_the_translatable_chunks(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile", "mask")
    records = [
        ChunkRecord(id=chunk_id, start=0, end=1, part=Part.BODY, tokens=1, paragraphs=1, translatable_chars=chars)
        for chunk_id, chars in (("c000", 5), ("c001", 0), ("c002", 7))
    ]
    workdir.brief.write_text(BriefFile(chunks=records).model_dump_json(), encoding="utf-8")
    assert status_json("2002.05202", "--workdir", str(workdir.path)).translate_chunks == ChunkCounts(total=2, done=0)
    manifest = TranslateManifest(
        status=TranslateStatus.OK,
        chunks={
            "c000": ChunkTranslateRecord(status=ChunkTranslateStatus.TRANSLATED, attempts=1),
            "c001": ChunkTranslateRecord(status=ChunkTranslateStatus.SKIPPED, attempts=0),
            "c002": ChunkTranslateRecord(status=ChunkTranslateStatus.FALLBACK, attempts=2),
        },
    )
    workdir.manifest_path("translate").write_text(manifest.model_dump_json(), encoding="utf-8")
    snapshot = status_json("2002.05202", "--workdir", str(workdir.path))
    assert snapshot.translate_chunks == ChunkCounts(total=2, done=2)
    assert next(state for state in snapshot.stages if state.name == "translate").status == "ok"


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
    assert "[structure] whole text, braces_and_math" in result.stdout
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
    first_lines = []
    for paper in ("2002.05202", "https://arxiv.org/abs/2002.05202"):
        result = runner.invoke(app, ["status", paper])
        assert result.exit_code == 0
        first_lines.append(result.stdout.splitlines()[0])
    assert first_lines[0] == first_lines[1]
    assert str(tmp_path / "papers" / "2002.05202") in first_lines[0]


def test_status_dev_uses_the_dev_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEV_HOME", tmp_path / "dev")
    result = runner.invoke(app, ["status", "2002.05202", "--dev"], env={"TONGTU_HOME": None})
    assert result.exit_code == 0
    assert str(tmp_path / "dev" / "papers" / "2002.05202") in result.stdout.splitlines()[0]


def test_dev_wins_over_tongtu_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEV_HOME", tmp_path / "dev")
    result = runner.invoke(app, ["status", "2002.05202", "--dev"], env={"TONGTU_HOME": str(tmp_path / "home")})
    assert result.exit_code == 0
    first_line = result.stdout.splitlines()[0]
    assert str(tmp_path / "dev" / "papers" / "2002.05202") in first_line
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


def test_stage_display_action_prints_a_line_when_not_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(type(cli.console), "is_terminal", property(lambda self: False))
    progress = Progress(disable=True)
    task = progress.add_task("precompile", total=None)
    display = cli.StageDisplay(progress=progress, task=task, name="precompile")
    display.action("fix session", "Read: main.tex")
    display.action("verifying")
    out = capsys.readouterr().out
    assert "    fix session: Read: main.tex" in out
    assert "    verifying: " in out
    assert progress.tasks[0].description == "precompile"


def test_stage_display_chunks_prints_a_line_when_not_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(type(cli.console), "is_terminal", property(lambda self: False))
    progress = Progress(disable=True)
    task = progress.add_task("translate", total=None, chunks="", tokens="", chunk_progress=())
    cli.StageDisplay(progress=progress, task=task, name="translate").chunks(CHUNKS)
    assert "    chunks 2/4, 0.0k/0.1k tok" in capsys.readouterr().out
    assert progress.tasks[0].fields["chunks"] == ""


CHUNKS = (
    ChunkProgress("c000", 10, ChunkProgressState.DONE),
    ChunkProgress("c001", 1, ChunkProgressState.WARNING),
    ChunkProgress("c002", 30, ChunkProgressState.RUNNING),
    ChunkProgress("c003", 20, ChunkProgressState.PENDING),
)


def spans(text) -> list[tuple[str, str]]:
    return [(text.plain[span.start : span.end], str(span.style)) for span in text.spans]


@pytest.mark.parametrize("width", [20, 45, 85])
def test_chunk_line_splits_the_width_by_tokens(width: int) -> None:
    text = cli._chunk_line(CHUNKS, width, False)
    assert len(text.plain) == width
    parts = spans(text)
    assert [style for _run, style in parts] == ["green", "yellow", "cyan", "dim"]
    assert [run[0] for run, _style in parts] == ["━", "━", "─", "─"]
    lengths = [len(run) for run, _style in parts]
    assert lengths[1] == 1
    assert lengths[2] > lengths[3] > lengths[0] > lengths[1]
    assert sum(lengths) == width


def test_chunk_line_shares_cells_taking_the_worst_state() -> None:
    many = tuple(
        ChunkProgress(f"c{index:03d}", 1, ChunkProgressState.WARNING if index == 5 else ChunkProgressState.DONE)
        for index in range(50)
    )
    text = cli._chunk_line(many, 20, False)
    assert text.plain == "━" * 20
    assert [style for _run, style in spans(text)] == ["green"] * 2 + ["yellow"] + ["green"] * 17


def test_chunk_line_ascii_fallback() -> None:
    assert cli._chunk_line(CHUNKS, 20, True).plain == "===!~~~~~~~~~~------"


def test_chunk_line_is_empty_below_the_minimum_width() -> None:
    assert cli._chunk_line(CHUNKS, cli.MIN_CHUNK_LINE_WIDTH - 1, False).plain == ""


def test_stage_display_chunks_counts_finished_tokens(terminal_console: None) -> None:
    progress = Progress(disable=True)
    task = progress.add_task("translate", total=None, chunks="", tokens="", chunk_progress=())
    cli.StageDisplay(progress=progress, task=task, name="translate").chunks(CHUNKS)
    fields = progress.tasks[0].fields
    assert progress.tasks[0].completed == 11
    assert progress.tasks[0].total == 61
    assert fields["chunks"] == "2/4"
    assert fields["tokens"] == "0.0k/0.1k tok"
    assert fields["chunk_progress"] == CHUNKS


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
    session = FixSession(stop_reason="finished", model="rt/m", duration_seconds=1.0)
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


def test_compile_entry_passes_the_workdir_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(workdir, *, report):
        captured.update(workdir=workdir, report=report)
        return CompileManifest(status=CompileStatus.OK)

    monkeypatch.setattr(cli.compile, "run", fake_run)
    workdir = write_outputs(tmp_path, *STAGES[:-1])
    result = runner.invoke(app, ["stage", "compile", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert captured["workdir"].path == workdir.path
    assert callable(captured["report"])


def test_doctor_reports_agentrun_json_that_is_not_a_check_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text('[roles]\nreview = { model = "pi", timeout_seconds = 60 }\n', encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == [cli.XELATEX, "--version"]:
            return subprocess.CompletedProcess(command, 0, VERSION_2026, "")
        return subprocess.CompletedProcess(command, 0, '[{"runtime": "pi"}]', "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == cli.EXIT_NOT_CONFIGURED
    assert "[missing]piagentrundoctoragentrundoctorprintednochecklist" in squeeze(result.stdout)


def test_doctor_checks_each_runtime_and_model_pair_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.write_text(
        "[roles]\n"
        'review = { model = "pi/a/m1", timeout_seconds = 60 }\n'
        'compile_fix = { model = "pi/a/m1", timeout_seconds = 60 }\n'
        'precompile_fix = { model = "pi/b/m2", timeout_seconds = 60 }\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    doctor_calls = [command[1:] for command in calls if command[1:2] == ["doctor"]]
    assert doctor_calls == [
        ["doctor", "pi", "--json", "--model", "a/m1"],
        ["doctor", "pi", "--json", "--model", "b/m2"],
    ]


LATEST_API_URL = "https://export.arxiv.org/api/query?id_list=2106.04426"


def wire_api(monkeypatch: pytest.MonkeyPatch, feed: str = LATEST_FEED, status_code: int = 200) -> list[str]:
    urls: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        urls.append(url)
        return httpx.Response(status_code, text=feed, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    return urls


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


@pytest.mark.parametrize("manifest", [None, ("1412.6980", 8), ("2106.04426", None)])
def test_run_with_workdir_of_another_paper_asks_arxiv(
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


def test_stage_without_a_local_version_uses_the_bare_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    write_outputs(tmp_path / "papers", "fetch", "precompile").path.rename(tmp_path / "papers" / "2106.04426")
    result, captured, calls = run_with_home(tmp_path, monkeypatch, "stage", "mask", "2106.04426")
    assert result.exit_code == 0
    assert (urls, calls) == ([], ["mask"])
    assert captured[0].workdir.path == tmp_path / "papers" / "2106.04426"
    assert captured[0].paper.version is None


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


def test_status_without_a_local_version_uses_the_bare_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = wire_api(monkeypatch)
    make_paper_dirs(tmp_path, "1412.6980")
    snapshot = status_with_home(tmp_path, "1412.6980")
    assert urls == []
    assert snapshot.workdir == tmp_path / "papers" / "1412.6980"
    assert (snapshot.arxiv_id, snapshot.version) == ("1412.6980", None)


def test_status_with_workdir_reads_the_version_of_its_fetch_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = wire_api(monkeypatch)
    workdir = tmp_path / "chosen"
    write_fetch_manifest(workdir, "1412.6980", 8)
    snapshot = status_with_home(tmp_path, "1412.6980", "--workdir", str(workdir))
    assert urls == []
    assert (snapshot.workdir, snapshot.version) == (workdir, 8)
    assert status_with_home(tmp_path, "1412.6980v9", "--workdir", str(workdir)).version == 9


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
