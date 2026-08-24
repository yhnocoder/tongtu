from __future__ import annotations

import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tongtu import __version__, cli, validation
from tongtu.artifacts.common import Manifest
from tongtu.cli import RunOptions, app, main
from tongtu.model.config import MODELS_TEMPLATE
from tongtu.pipeline import STAGES

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

[provider.behind_runtime]
base_url = "https://behind.example"
api_key = "behind-key"
api = "chat"

[runtime.demo_runtime]
skill_path = ".agent/skills/{role}"
command = ["runner", "-p"]

[runtime.gateway_runtime]
provider = "written"
skill_path = ".agent/skills/{role}"
command = ["runner", "-p"]

[runtime.behind_runtime]
provider = "behind_runtime"
skill_path = ".agent/skills/{role}"
command = ["runner", "-p"]

[roles]
translate = { provider = "demo", model = "m1", effort = "low" }
survey_terms = { provider = "written", model = "m1", effort = "low" }
review = { runtime = "demo_runtime", model = "m1", effort = "low", max_turns = 4, timeout_seconds = 60, bash = [] }
precompile_fix = { runtime = "gateway_runtime", model = "m1", effort = "low", max_turns = 4, timeout_seconds = 60, bash = [] }
compile_fix = { runtime = "behind_runtime", model = "m1", effort = "low", max_turns = 4, timeout_seconds = 60, bash = [] }
"""


KEYLESS_TABLE = """
[provider.demo]
base_url = "https://demo.example/v1"
api = "chat"

[roles]
translate = { provider = "demo", model = "m1", effort = "low" }
"""


VERSION_2026 = "XeTeX 3.141592653-2.6-0.999998 (TeX Live 2026)\nkpathsea version 6.4.1\n"

VERSION_2022 = "XeTeX 3.141592653-2.6-0.999994 (TeX Live 2022)\nkpathsea version 6.3.4\n"

VERSION_WITHOUT_YEAR = "XeTeX 3.141592653-2.6-0.999996 (MiKTeX 24.1)\n"


def fake_xelatex_version(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    def run(command: list[str], capture_output: bool, text: bool, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(cli.subprocess, "run", run)


def squeeze(text: str) -> str:
    return "".join(text.split())


def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "tongtu" / "models.toml"


def written_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = config_path(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(TABLE, encoding="utf-8")
    return path


def test_setup_writes_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert path.read_text(encoding="utf-8") == MODELS_TEMPLATE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_setup_does_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text("手改过的配置\n", encoding="utf-8")
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert path.read_text(encoding="utf-8") == "手改过的配置\n"
    assert "不覆盖" in squeeze(result.stdout)


def test_setup_interactive_fills_first_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup", "-i"], input="y\nzen-key\nn\n")
    assert result.exit_code == 0
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["provider"]["opencode"]["api_key"] == "zen-key"
    assert written["provider"]["opencode"]["api_key_env"] == "OPENCODE_API_KEY"
    assert written["provider"]["anthropic"]["api_key"] == ""
    assert written["roles"]["translate"] == {
        "provider": "opencode",
        "model": "deepseek-v4-flash",
        "effort": "low",
    }
    assert written["roles"]["survey_terms"]["provider"] == "opencode"
    assert written["roles"]["review"]["runtime"] == "claude_code"
    assert written["roles"]["review"]["model"] == "claude-sonnet-5"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_setup_interactive_points_ask_roles_at_second_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup", "-i"], input="n\ny\nsk-key\n")
    assert result.exit_code == 0
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["provider"]["anthropic"]["api_key"] == "sk-key"
    assert written["provider"]["opencode"]["api_key"] == ""
    assert written["roles"]["translate"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "effort": "low",
    }


def test_setup_interactive_without_any_provider_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    result = runner.invoke(app, ["setup", "-i"], input="n\nn\n")
    assert result.exit_code == 2
    assert not path.exists()


def test_doctor_without_config_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "tongtusetup" in output
    assert "工具链与字体齐全" in output


def test_doctor_missing_toolchain_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    output = squeeze(result.stdout)
    assert "环境有缺失：xelatex" in output
    assert "[缺失]TeXLive" in output
    assert "xelatex不在PATH里，无法检查" in output


def test_doctor_accepts_the_required_texlive_year(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "[通过]TeXLive" in output
    assert "(TeXLive2026)" in output


def test_doctor_rejects_an_older_texlive_year(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2022)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    output = squeeze(result.stdout)
    assert "[缺失]TeXLive" in output
    assert "TeXLive2022低于要求的2026" in output
    assert "环境有缺失：TeXLive" in output


def test_doctor_rejects_output_without_a_texlive_year(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_WITHOUT_YEAR)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    output = squeeze(result.stdout)
    assert "[缺失]TeXLive" in output
    assert "(MiKTeX24.1)" in output


def test_doctor_lists_only_referenced_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "环境齐全。" in output
    assert "密钥demo" in output
    assert "密钥unused" not in output
    assert "运行时demo_runtime" in output
    assert "环境变量DEMO_KEY" in output
    assert "models.toml的api_key" in output


def test_doctor_checks_the_provider_a_runtime_points_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "密钥behind_runtime" in output
    assert output.count("密钥written") == 1
    assert "运行时gateway_runtime" in output


def test_doctor_reports_missing_key_without_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written_config(tmp_path, monkeypatch)
    monkeypatch.delenv("DEMO_KEY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert "工具链与字体齐全" in output
    assert "密钥demo" in output


def test_doctor_keeps_table_names_in_the_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(KEYLESS_TABLE, encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_xelatex_version(monkeypatch, VERSION_2026)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "[provider.demo]" in result.stdout


def test_setup_keeps_the_path_on_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_path(tmp_path, monkeypatch)
    monkeypatch.setenv("COLUMNS", "60")
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert [line for line in result.stdout.splitlines() if "已写出" in line and str(path) in line]


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
        def entry(options: RunOptions) -> Manifest:
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
    result = runner.invoke(app, ["run", "2002.05202", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 0
    assert calls == list(STAGES)


def test_run_resumes_from_the_first_absent_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert calls == ["mask", "survey", "translate", "review", "compile"]
    assert "从mask开始" in squeeze(result.stdout)


def test_run_with_all_outputs_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 0
    assert calls == []
    assert "--from" in result.stdout


def test_run_from_cleans_downstream_before_rerunning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    calls: list[str] = []
    wire_entries(monkeypatch, calls)
    result = runner.invoke(app, ["run", "2002.05202", "--workdir", str(workdir.path), "--from", "review"])
    assert result.exit_code == 0
    assert calls == ["review", "compile"]
    assert (workdir.build / "translated" / "c000.tex").is_file()
    assert workdir.manifest_path("translate").is_file()
    assert not (workdir.build / "reviewed").exists()
    assert not (workdir.path / "out" / "zh.pdf").exists()
    assert not workdir.manifest_path("review").exists()
    assert not workdir.manifest_path("compile").exists()


def test_run_stops_at_the_first_failed_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    wire_entries(monkeypatch, calls, statuses={"mask": "mask_failed"})
    result = runner.invoke(app, ["run", "2002.05202", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 1
    assert calls == ["fetch", "precompile", "mask"]
    assert "mask_failed" in result.stdout
    assert "失败原因一句话" in result.stdout
    assert str(Path(tmp_path, "paper", "build", "manifests", "mask.json")) in squeeze(result.stdout)


def test_run_exits_one_at_an_unbuilt_stage(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "2002.05202", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 1


def test_run_passes_the_options_to_the_stage_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    captured: list[RunOptions] = []
    wire_entries(monkeypatch, calls, captured=captured)
    result = runner.invoke(
        app,
        [
            "run",
            "https://arxiv.org/abs/2002.05202",
            "--workdir",
            str(tmp_path / "paper"),
            "--ask-model",
            "opencode/deepseek-v4-flash",
            "--ask-effort",
            "low",
            "--work-model",
            "claude_code/claude-sonnet-5",
            "--work-effort",
            "high",
            "--glossary",
            "a.json",
            "--glossary",
            "b.json",
            "--jobs",
            "9",
            "--no-terms",
        ],
    )
    assert result.exit_code == 0
    options = captured[0]
    assert options.paper.arxiv_id == "2002.05202"
    assert options.workdir.path == tmp_path / "paper"
    assert options.ask_model == "opencode/deepseek-v4-flash"
    assert options.ask_effort == "low"
    assert options.work_model == "claude_code/claude-sonnet-5"
    assert options.work_effort == "high"
    assert options.glossary == (Path("a.json"), Path("b.json"))
    assert options.jobs == 9
    assert options.no_terms is True
    assert all(entry is options for entry in captured)


def test_run_rejects_an_invalid_paper_argument(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "a b", "--workdir", str(tmp_path / "paper")])
    assert result.exit_code == 2


def test_run_rejects_an_unknown_from_stage(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "2002.05202", "--workdir", str(tmp_path / "paper"), "--from", "flatten"])
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


def test_stage_exits_one_when_the_stage_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    calls: list[str] = []
    wire_entries(monkeypatch, calls, statuses={"mask": "mask_failed"})
    result = runner.invoke(app, ["stage", "mask", "2002.05202", "--workdir", str(workdir.path)])
    assert result.exit_code == 1
    assert calls == ["mask"]


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
    assert "哨兵已存在；警告一条" in output
    assert "在" in output
    assert "不在" in output
    assert squeeze(str(mask_path)) in output
    assert squeeze(str(workdir.manifest_path("fetch"))) in output


def test_status_does_not_create_the_workdir(tmp_path: Path) -> None:
    target = tmp_path / "absent"
    result = runner.invoke(app, ["status", "2002.05202", "--workdir", str(target)])
    assert result.exit_code == 0
    assert not target.exists()


def test_validate_exits_zero_when_every_layer_passes(tmp_path: Path) -> None:
    src = tmp_path / "c000.tex"
    dst = tmp_path / "zh.tex"
    src.write_text("We use $x$ here.\n", encoding="utf-8")
    dst.write_text("我们这里用 $x$。\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(src), str(dst)])
    assert result.exit_code == 0
    output = squeeze(result.stdout)
    assert all(f"[通过]{layer}" in output for layer in validation.CHECK_NAMES)


def test_validate_exits_one_when_a_layer_fails(tmp_path: Path) -> None:
    src = tmp_path / "c000.tex"
    dst = tmp_path / "zh.tex"
    src.write_text("We use $x$ here.\n", encoding="utf-8")
    dst.write_text("我们这里用 x。\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(src), str(dst)])
    assert result.exit_code == 1
    output = squeeze(result.stdout)
    assert "[失败]braces_and_math" in output
    assert "[通过]placeholders" in output


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
    assert str(tmp_path / "2002.05202") in first_lines[0]
