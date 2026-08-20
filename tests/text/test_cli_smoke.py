from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from tongtu import __version__
from tongtu.cli import EXIT_STUB, app
from tongtu.stages import STAGES

from ..conftest import TONGTU_BIN

runner = CliRunner()

WIRED_STAGES = ("fetch", "flatten", "precompile", "mask", "survey", "chunk", "translate")

STUB_STAGES = tuple(name for name in STAGES if name not in WIRED_STAGES)


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_root_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "retranslate", "stage", "validate", "doctor", "preview"):
        assert command in result.stdout


def test_stage_help_lists_all_stage_names() -> None:
    result = runner.invoke(app, ["stage", "--help"])
    assert result.exit_code == 0
    for name in STAGES:
        assert name in result.stdout


@pytest.mark.parametrize("name", STUB_STAGES)
def test_stub_stages_exit_with_stub_code(name: str, tmp_path) -> None:
    result = runner.invoke(app, ["stage", name, "somepaper", "--workdir", str(tmp_path)])
    assert result.exit_code == EXIT_STUB


def test_usage_error_exit_code() -> None:
    result = runner.invoke(app, ["stage", "not-a-stage-name", "paper"])
    assert result.exit_code == 2


def test_validate_requires_two_arguments() -> None:
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 2


def test_entry_point_is_installed() -> None:
    assert TONGTU_BIN.exists(), f"入口点未安装：{TONGTU_BIN}"
    completed = subprocess.run([str(TONGTU_BIN), "--version"], capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr
    assert __version__ in completed.stdout
