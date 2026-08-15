"""CLI 命令面接口测试（架构 §6；`tex` 工具面见架构 §3 compile 节）。

全部命令当前是占位实现，统一退出码 EXIT_STUB。本文件测接口本身：命令与参数存在、
约束生效（互斥、id 形状、取值范围）、`--help` 可用、`run --json` 的事件流逐行通过
artifact model 校验。涉及 EXIT_STUB 的断言在对应命令接线后改为真实退出码语义。
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tongtu import __version__
from tongtu.artifacts import EVENT_ADAPTER, ResultEvent
from tongtu.cli import EXIT_STUB, app
from tongtu.stages import STAGES

USAGE_ERROR = 2

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args))


# --------------------------------------------------------------------- help 面


def test_top_level_help_lists_all_human_commands():
    result = invoke("--help")

    assert result.exit_code == 0
    for command in ("run", "retranslate", "stage", "validate", "doctor", "preview"):
        assert command in result.output


def test_tex_is_hidden_from_top_level_help():
    """tex 工具面不面向人：顶层 help 不列出，`tongtu tex --help` 仍可用。"""
    top = invoke("--help")
    assert "工具面" not in top.output

    tex = invoke("tex", "--help")
    assert tex.exit_code == 0
    for command in ("read", "patch", "compile", "render", "fallback", "retranslate"):
        assert command in tex.output


@pytest.mark.parametrize(
    "argv",
    [
        ("run",),
        ("retranslate",),
        ("stage",),
        ("validate",),
        ("doctor",),
        ("preview",),
        ("tex", "read"),
        ("tex", "patch"),
        ("tex", "compile"),
        ("tex", "render"),
        ("tex", "fallback"),
        ("tex", "retranslate"),
    ],
)
def test_every_command_answers_help(argv):
    result = invoke(*argv, "--help")

    assert result.exit_code == 0


def test_version_flag():
    result = invoke("--version")

    assert result.exit_code == 0
    assert __version__ in result.output


# ------------------------------------------------------------------- 主命令面


def test_run_accepts_the_documented_interface():
    result = invoke(
        "run",
        "2601.00001",
        "--glossary",
        "a.json",
        "--glossary",
        "b.json",
        "--workdir",
        "/tmp/w",
        "--force",
        "--agent",
        "claude-code",
        "--model",
        "m1",
    )

    assert result.exit_code == EXIT_STUB


def test_run_json_emits_a_model_valid_event_stream():
    result = invoke("run", "2601.00001", "--json")

    assert result.exit_code == EXIT_STUB
    lines = [line for line in result.output.splitlines() if line.strip()]
    events = [EVENT_ADAPTER.validate_json(line) for line in lines]
    assert len(events) == 2 * len(STAGES) + 1, "每阶段起止各一条 + 末行 result"
    last = events[-1]
    assert isinstance(last, ResultEvent)
    assert last.exit_code == EXIT_STUB and last.status == "failed"
    assert {e.run_id for e in events} == {events[0].run_id}, "同一次运行共享 run_id"


def test_retranslate_requires_exactly_one_selector():
    assert invoke("retranslate", "2601.00001").exit_code == USAGE_ERROR
    assert invoke("retranslate", "2601.00001", "--all", "--term", "attention").exit_code == USAGE_ERROR
    assert invoke("retranslate", "2601.00001", "--all").exit_code == EXIT_STUB
    assert invoke("retranslate", "2601.00001", "--term", "attention").exit_code == EXIT_STUB
    assert invoke("retranslate", "2601.00001", "--chunks", "c012,c045").exit_code == EXIT_STUB


def test_retranslate_rejects_malformed_chunk_ids():
    assert invoke("retranslate", "2601.00001", "--chunks", "12").exit_code == USAGE_ERROR
    assert invoke("retranslate", "2601.00001", "--chunks", " ,").exit_code == USAGE_ERROR


def test_stage_accepts_every_stage_name_and_rejects_unknown():
    for name in STAGES:
        assert invoke("stage", name, "2601.00001").exit_code == EXIT_STUB
    assert invoke("stage", "nosuchstage", "2601.00001").exit_code == USAGE_ERROR


def test_validate_reports_the_four_layers():
    result = invoke("validate", "src.tex", "dst.tex")

    assert result.exit_code == EXIT_STUB
    for layer in ("placeholders", "control_sequences", "braces_and_math", "paragraph_count"):
        assert layer in result.output


def test_doctor_lists_the_documented_checks():
    result = invoke("doctor")

    assert result.exit_code == EXIT_STUB
    for tool in ("xelatex", "latexmk", "latexpand", "pdftocairo", "epstopdf", "中文字体"):
        assert tool in result.output


def test_preview_interface():
    assert invoke("preview", "2601.00001").exit_code == EXIT_STUB
    assert invoke("preview", "2601.00001", "--serve", "--workdir", "/tmp/w").exit_code == EXIT_STUB


# ----------------------------------------------------------------- tex 工具面


def test_tex_read_requires_exactly_one_region():
    assert invoke("tex", "read").exit_code == USAGE_ERROR
    assert invoke("tex", "read", "--preamble", "--chunk", "c001").exit_code == USAGE_ERROR
    assert invoke("tex", "read", "--preamble").exit_code == EXIT_STUB
    assert invoke("tex", "read", "--chunk", "c001").exit_code == EXIT_STUB
    assert invoke("tex", "read", "--lines", "120-180").exit_code == EXIT_STUB


def test_tex_read_rejects_malformed_regions():
    assert invoke("tex", "read", "--chunk", "block-1").exit_code == USAGE_ERROR
    assert invoke("tex", "read", "--lines", "120").exit_code == USAGE_ERROR
    assert invoke("tex", "read", "--lines", "a-b").exit_code == USAGE_ERROR


def test_tex_patch_interface():
    assert invoke("tex", "patch", "--old", "a", "--new", "b").exit_code == EXIT_STUB
    assert invoke("tex", "patch", "--old", "a", "--new", "b", "--chunk", "c012").exit_code == EXIT_STUB
    assert invoke("tex", "patch", "--old", "a").exit_code == USAGE_ERROR
    assert invoke("tex", "patch", "--old", "a", "--new", "b", "--chunk", "x").exit_code == USAGE_ERROR


def test_tex_compile_is_a_stub():
    assert invoke("tex", "compile").exit_code == EXIT_STUB


def test_tex_render_requires_a_positive_page():
    assert invoke("tex", "render", "--page", "3").exit_code == EXIT_STUB
    assert invoke("tex", "render", "--page", "0").exit_code == USAGE_ERROR
    assert invoke("tex", "render").exit_code == USAGE_ERROR


def test_tex_fallback_interface():
    assert invoke("tex", "fallback", "c012").exit_code == EXIT_STUB
    assert invoke("tex", "fallback", "c012", "--paragraph", "2").exit_code == EXIT_STUB
    assert invoke("tex", "fallback", "12").exit_code == USAGE_ERROR
    assert invoke("tex", "fallback", "c012", "--paragraph", "-1").exit_code == USAGE_ERROR


def test_tex_retranslate_interface():
    assert invoke("tex", "retranslate", "c012").exit_code == EXIT_STUB
    assert invoke("tex", "retranslate", "12").exit_code == USAGE_ERROR
