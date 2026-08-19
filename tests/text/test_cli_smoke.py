"""CLI 冒烟：各命令能启动、给出预期输出、返回登记过的退出码。

ruff 的 F 规则查不出运行时的导入错误与命令注册遗漏，这一层补上。判定分两种：多数命令
用 typer 的 CliRunner 在进程内调用；另有一条走真实子进程，判定 `pyproject.toml` 声明的
入口点确实被安装成可执行文件。

`doctor` 的退出码取决于本机 toolchain 是否齐全（runner 上没有 TeX），因此只判定它逐项
报告并落在 0 或 1，不判定具体取值——「环境有缺失退 1」本身是 doctor 的正常结果。凭证
那一组不计入退出码，这一条则可以定值判定，见 test_doctor_ignores_missing_credentials。
"""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from tongtu import __version__, cli
from tongtu.cli import DOCTOR_CHECKS, DOCTOR_CREDENTIAL_CHECKS, DOCTOR_TOOLCHAIN_CHECKS, EXIT_STUB, app
from tongtu.stages import STAGES

from ..conftest import TONGTU_BIN

runner = CliRunner()

#: 已接线的阶段：`stage <名字> --help` 之外还要能给出真实执行路径。
WIRED_STAGES = ("fetch", "flatten", "precompile", "mask", "survey", "chunk")

#: 仍是占位实现的阶段，执行时统一退 `EXIT_STUB`。
STUB_STAGES = tuple(name for name in STAGES if name not in WIRED_STAGES)


def test_version_flag() -> None:
    """`--version` 打印版本号并退 0。"""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_root_help_lists_commands() -> None:
    """根 `--help` 列出命令面上的各子命令。"""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "retranslate", "stage", "validate", "doctor", "preview"):
        assert command in result.stdout


def test_stage_help_lists_all_stage_names() -> None:
    """`stage --help` 的取值约束覆盖全部阶段名，包括尚未接线的。"""
    result = runner.invoke(app, ["stage", "--help"])
    assert result.exit_code == 0
    for name in STAGES:
        assert name in result.stdout


@pytest.mark.parametrize("name", STUB_STAGES)
def test_stub_stages_exit_with_stub_code(name: str, tmp_path) -> None:
    """未接线的阶段退 99，且这个码不与成功、一般失败或业务分支段重合。"""
    result = runner.invoke(app, ["stage", name, "somepaper", "--workdir", str(tmp_path)])
    assert result.exit_code == EXIT_STUB


def test_doctor_reports_each_check() -> None:
    """`doctor` 逐项报告检查结果；退出码取决于本机环境，只判定落在 0 或 1。"""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code in (0, 1)
    for name, _purpose in DOCTOR_CHECKS:
        assert name in result.stdout


def test_doctor_check_groups_partition_all_checks() -> None:
    """两组检查项不重不漏地拼成 DOCTOR_CHECKS，新增检查项必须落进其中一组。"""
    assert DOCTOR_TOOLCHAIN_CHECKS + DOCTOR_CREDENTIAL_CHECKS == DOCTOR_CHECKS
    names = [name for name, _ in DOCTOR_CHECKS]
    assert len(names) == len(set(names)), "检查项名字重复"


def test_doctor_ignores_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """凭证缺失如实报告但退 0。

    参考镜像在构建期跑 `tongtu doctor` 自检（docker/Dockerfile），而构建镜像的机器不该
    需要运行期凭证——镜像是要推 GHCR 的可分发产物。凭证若计入退出码，镜像构建会卡在自检
    那一层，那正是这一改动要解决的问题。
    """
    monkeypatch.setattr(cli, "_check_opencode_key", lambda: (False, "三处都没有"))
    result = runner.invoke(app, ["doctor"])
    for name, _purpose in DOCTOR_CREDENTIAL_CHECKS:
        assert name in result.stdout, f"{name} 没有出现在报告里"
    if result.exit_code == 0:
        assert "未配置" in result.stdout, "凭证缺失时应给出说明"
    else:
        # 本机 toolchain 不全（例如 runner 上没有 TeX），那一组才是退 1 的原因
        assert "环境有缺失" in result.stdout
        assert all(name not in result.stdout.split("环境有缺失")[-1] for name, _ in DOCTOR_CREDENTIAL_CHECKS), (
            "凭证不应出现在「环境有缺失」那一行里"
        )


def test_usage_error_exit_code() -> None:
    """参数用法错误退 2（typer 默认），与业务失败的 1 区分开。"""
    result = runner.invoke(app, ["stage", "not-a-stage-name", "paper"])
    assert result.exit_code == 2


def test_validate_requires_two_arguments() -> None:
    """`validate` 缺参数时是用法错误，不是业务失败。"""
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 2


def test_entry_point_is_installed() -> None:
    """`pyproject.toml` 声明的 `tongtu` 入口点被安装成可执行文件，能从命令行启动。"""
    assert TONGTU_BIN.exists(), f"入口点未安装：{TONGTU_BIN}"
    completed = subprocess.run([str(TONGTU_BIN), "--version"], capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr
    assert __version__ in completed.stdout
