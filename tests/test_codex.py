"""Codex CLI 适配器（架构 §9 两原语、§13 选型）。

**本机没有 codex CLI 也要能测**：真 subprocess 只住在 `subprocess_runner` 里，适配器其余
部分（argv 模板组装、沙箱与工作目录圈定、超时、结构化错误、输出清洗、转录落盘）全部经
注入的假 runner 验证——与 `tongtu.compiler` 把 latexmk 封在 `Compiler` 之后是同一套纪律。

真 codex 的调用路径 `skipif(没装 codex)`，这是预期内的 skip；默认 runner 本身则借 `cat`
这个「读 stdin 吐回来」的可执行文件走一遍真 subprocess。
"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tongtu.agent import DEFAULT_AGENT, agent_names, get_agent, resolve_agent_name
from tongtu.agent.codex import (
    ERROR,
    FAILED,
    MISSING_TOOL,
    OK,
    TIMEOUT,
    CodexAgent,
    CodexError,
    RunResult,
    clean_output,
    join_prompt,
    render_argv,
    subprocess_runner,
)
from tongtu.agent.mock import MockAgent, PseudoAgent
from tongtu.workdir import Workdir

ANSWER = "第一段译文 ⟦BLK-1⟧"

#: 构造 CodexAgent 必须显式给模型（缓存 key 认它），测试统一用这一个。
MODEL = "gpt-x"

#: 真 codex CLI 用例里下发的模型（本机没装 codex 时该用例 skip）。
REAL_MODEL = "gpt-5-codex"


# --------------------------------------------------------------------------- #
# 夹具：可编程假 runner
# --------------------------------------------------------------------------- #


def flag_value(argv, flag: str) -> str | None:
    argv = list(argv)
    return argv[argv.index(flag) + 1] if flag in argv else None


def runner(
    answer: str | None = ANSWER,
    *,
    status: str = OK,
    returncode: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    message: str = "",
):
    """假 runner：把 `answer` 写进 `--output-last-message` 指向的文件，记录每次调用。"""
    calls: list[SimpleNamespace] = []

    def run(argv, *, cwd=None, stdin="", timeout=None, env=None):
        calls.append(SimpleNamespace(argv=tuple(argv), cwd=cwd, stdin=stdin, timeout=timeout, env=env))
        out = flag_value(argv, "--output-last-message")
        if answer is not None and out:
            Path(out).write_text(answer, encoding="utf-8")
        return RunResult(
            status=status,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message=message,
        )

    run.calls = calls  # type: ignore[attr-defined]
    return run


def paper(tmp_path: Path) -> Workdir:
    return Workdir(path=tmp_path / "2501.00001", arxiv_id="2501.00001").create()


# --------------------------------------------------------------------------- #
# argv 模板
# --------------------------------------------------------------------------- #


def test_render_argv_drops_segments_with_empty_fields():
    template = (("codex",), ("exec",), ("--model", "{model}"), ("--sandbox", "{sandbox}"))

    assert render_argv(template, {"model": "gpt-x", "sandbox": "read-only"}) == (
        "codex",
        "exec",
        "--model",
        "gpt-x",
        "--sandbox",
        "read-only",
    )
    # 模型没给 → 连 flag 一起消失，不留裸 flag
    assert render_argv(template, {"model": "", "sandbox": "read-only"}) == (
        "codex",
        "exec",
        "--sandbox",
        "read-only",
    )


def test_render_argv_rejects_unknown_field():
    with pytest.raises(CodexError) as excinfo:
        render_argv((("--wat", "{nope}"),), {"model": "x"})

    assert excinfo.value.kind == "bad_template"


def test_session_argv_pins_sandbox_model_and_workdir(tmp_path):
    work = paper(tmp_path)
    agent = CodexAgent(model=MODEL, runner=runner(), session_timeout=99.0)

    agent.session("修一下", workdir=work)

    call = agent.runner.calls[0]  # type: ignore[attr-defined]
    argv = list(call.argv)
    assert argv[:2] == ["codex", "exec"]
    assert flag_value(argv, "--sandbox") == "workspace-write", "会话要写工作目录"
    assert flag_value(argv, "-C") == str(work.path), "工作目录必须圈进 argv"
    assert flag_value(argv, "--model") == MODEL
    assert "--json" in argv and argv[-1] == "-", "转录走事件流，提示词走 stdin"
    assert call.stdin == "修一下" and call.cwd == str(work.path)
    assert call.timeout == 99.0


def test_complete_argv_is_read_only_and_isolated(tmp_path):
    agent = CodexAgent(model=MODEL, runner=runner(), complete_timeout=42.0)

    agent.complete("规则", "正文")

    call = agent.runner.calls[0]  # type: ignore[attr-defined]
    argv = list(call.argv)
    assert flag_value(argv, "--sandbox") == "read-only", "无状态判断不该有写权限"
    assert flag_value(argv, "--model") == MODEL, "模型是必填项，两个原语都要下发"
    assert "--json" not in argv, "要的是最后一条消息，不是事件流"
    assert flag_value(argv, "-C") != str(tmp_path), "complete 在临时目录里跑，不碰论文目录"
    assert call.stdin == join_prompt("规则", "正文") == "规则\n\n正文"
    assert call.timeout == 42.0


def test_argv_template_and_extras_are_overridable(tmp_path):
    """CLI 细节变了只改模板：换 flag 名、加预算段，逻辑一行不动。"""
    agent = CodexAgent(
        model=MODEL,
        runner=runner(),
        session_argv=(("{cli}",), ("run",), ("--cd", "{workdir}"), ("{prompt_arg}",)),
        budget_args=(("--max-turns", "{budget}"),),
        extra_args=("--verbose",),
        cli="codex-next",
    )

    agent.session("修", workdir=tmp_path, budget=7)

    argv = list(agent.runner.calls[0].argv)  # type: ignore[attr-defined]
    assert argv[0] == "codex-next" and argv[1] == "run"
    assert flag_value(argv, "--cd") == str(tmp_path)
    assert flag_value(argv, "--max-turns") == "7"
    assert argv[-1] == "-", "追加片段插在位置参数之前"
    assert "--verbose" in argv


def test_budget_is_not_sent_by_default(tmp_path):
    """codex CLI 没有稳定的『最大轮数』开关——默认只进转录，不瞎编 flag。"""
    work = paper(tmp_path)
    agent = CodexAgent(model=MODEL, runner=runner())

    outcome = agent.session("修", workdir=work, budget=3)

    assert "3" not in agent.runner.calls[0].argv  # type: ignore[attr-defined]
    meta = json.loads(Path(str(outcome.transcript_path)).with_suffix(".json").read_text("utf-8"))
    assert meta["budget"] == 3


# --------------------------------------------------------------------------- #
# session：成功、转录、失败模式
# --------------------------------------------------------------------------- #


def test_session_writes_transcript_into_workdir_logs(tmp_path):
    work = paper(tmp_path)
    agent = CodexAgent(runner=runner(stdout='{"type":"item"}\n'), model=MODEL)

    outcome = agent.session("编不过，修", workdir=work, joint="fixup")

    assert outcome.done is True, "进程正常结束 = 会话结束（不是『修好了』的裁决）"
    transcript = Path(str(outcome.transcript_path))
    assert transcript.parent == work.logs, "转录一律落 logs/（架构 §9）"
    assert transcript.suffix == ".log" and transcript.read_text("utf-8").startswith("{")

    meta = json.loads(transcript.with_suffix(".json").read_text("utf-8"))
    assert meta["prompt"] == "编不过，修"
    assert meta["joint"] == "fixup" and meta["model"] == MODEL
    assert meta["status"] == OK and meta["returncode"] == 0

    record = agent.calls[0]
    assert record.kind == "session" and record.status == OK and record.joint == "fixup"


def test_session_reports_missing_cli_without_raising(tmp_path):
    agent = CodexAgent(
        model=MODEL,
        runner=runner(status=MISSING_TOOL, returncode=None, message="PATH 中没有 codex"),
        log_dir=tmp_path / "logs",
    )

    outcome = agent.session("修", workdir=tmp_path)

    assert outcome.done is False and "codex" in outcome.message
    assert [e.kind for e in agent.errors] == [MISSING_TOOL]
    assert outcome.transcript_path is not None, "失败也要留转录——它是促升的数据源"


def test_session_reports_timeout_and_nonzero_exit(tmp_path):
    timed_out = CodexAgent(
        model=MODEL,
        runner=runner(status=TIMEOUT, returncode=None, message="codex 超时（1800s）"),
    )
    assert timed_out.session("修", workdir=tmp_path).done is False
    assert timed_out.errors[0].kind == TIMEOUT

    failed = CodexAgent(model=MODEL, runner=runner(returncode=3, stderr="boom: not logged in\n"))
    outcome = failed.session("修", workdir=tmp_path)
    assert outcome.done is False and "3" in outcome.message
    assert failed.errors[0].kind == FAILED and "not logged in" in failed.errors[0].detail


def test_session_survives_a_runner_that_raises(tmp_path):
    def explode(argv, **kwargs):
        raise OSError("fd 用光了")

    agent = CodexAgent(model=MODEL, runner=explode)

    outcome = agent.session("修", workdir=tmp_path)

    assert outcome.done is False and agent.errors[0].kind == ERROR


def test_as_session_fn_prepends_the_repair_skill(tmp_path):
    """关节②/⑥ 的规则住在 `skill/repair/SKILL.md`，现场信息由阶段驱动器给。"""
    work = paper(tmp_path)
    agent = CodexAgent(model=MODEL, runner=runner())
    request = SimpleNamespace(joint="fixup", prompt="第一个错误：! Undefined", workdir=work)

    agent.as_session_fn()(request)

    stdin = agent.runner.calls[0].stdin  # type: ignore[attr-defined]
    assert "编译修复会话" in stdin, "prompt 资产要拼进去"
    assert stdin.endswith("第一个错误：! Undefined"), "现场信息在规则之后"


# --------------------------------------------------------------------------- #
# complete：输出清洗与失败
# --------------------------------------------------------------------------- #


def test_complete_returns_the_last_message(tmp_path):
    agent = CodexAgent(model=MODEL, runner=runner(f"  {ANSWER}  \n"))

    assert agent.complete("规则", "src") == ANSWER


def test_complete_falls_back_to_stdout(tmp_path):
    """`--output-last-message` 没写出来（CLI 换了 flag 名）也不该白跑一趟。"""
    agent = CodexAgent(model=MODEL, runner=runner(None, stdout=f"{ANSWER}\n"))

    assert agent.complete("规则", "src") == ANSWER


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("```latex\n译文 $x$\n```", "译文 $x$"),
        ("```\n译文\n```", "译文"),
        ("以下是翻译：\n译文", "译文"),
        ("Here is the translation:\n译文", "译文"),
        ("好的，我来翻译：\n\n译文", "译文"),
        # 不该动的：正文里的围栏、带反斜杠的行、长得像开场白的正文
        ("译文一\n\n```\ncode\n```", "译文一\n\n```\ncode\n```"),
        ("\\textbf{左}：\n译文", "\\textbf{左}：\n译文"),
        ("定理的证明如下，读者可以跳过：\n译文", "定理的证明如下，读者可以跳过：\n译文"),
    ],
)
def test_clean_output_strips_decoration_conservatively(raw, expected):
    assert clean_output(raw) == expected


def test_complete_raises_structured_error_on_failure(tmp_path):
    agent = CodexAgent(model=MODEL, runner=runner(status=MISSING_TOOL, returncode=None, message="没装"))

    with pytest.raises(CodexError) as excinfo:
        agent.complete("规则", "src")

    assert excinfo.value.kind == MISSING_TOOL
    assert excinfo.value.to_json()["kind"] == MISSING_TOOL


def test_complete_treats_empty_output_as_failure(tmp_path):
    agent = CodexAgent(model=MODEL, runner=runner(""))

    with pytest.raises(CodexError) as excinfo:
        agent.complete("规则", "src")

    assert excinfo.value.kind == "empty_output"


def test_complete_logs_only_failures_by_default(tmp_path):
    quiet = CodexAgent(model=MODEL, runner=runner(), log_dir=tmp_path / "logs")
    quiet.complete("规则", "src")
    assert not (tmp_path / "logs").exists(), "每块一份转录只是噪声"

    loud = CodexAgent(model=MODEL, runner=runner(), log_dir=tmp_path / "all", log_completions=True)
    loud.complete("规则", "src")
    assert list((tmp_path / "all").glob("codex-complete-*.json"))

    broken = CodexAgent(model=MODEL, runner=runner(""), log_dir=tmp_path / "bad")
    with pytest.raises(CodexError):
        broken.complete("规则", "src")
    assert list((tmp_path / "bad").glob("codex-complete-*.json")), "失败必须留证据"


def test_transcript_failure_does_not_break_the_session(tmp_path):
    """转录写不下去（只读盘）时记警告继续——它不是流水线的裁决者。"""
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    agent = CodexAgent(model=MODEL, runner=runner(), log_dir=blocked)

    outcome = agent.session("修", workdir=tmp_path)

    assert outcome.done is True and outcome.transcript_path is None
    assert agent.errors[0].kind == "transcript"


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def test_get_agent_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("TONGTU_AGENT", raising=False)

    assert isinstance(get_agent(), MockAgent), "真运行时必须是显式选择"
    assert resolve_agent_name() == DEFAULT_AGENT == "mock"
    assert set(agent_names()) == {"mock", "pseudo", "codex"}


def test_get_agent_by_name_and_env(monkeypatch):
    assert isinstance(get_agent("codex", model=MODEL), CodexAgent)

    monkeypatch.setenv("TONGTU_AGENT", "codex")
    assert isinstance(get_agent(model=MODEL), CodexAgent)
    assert isinstance(get_agent("mock"), MockAgent), "显式参数压过环境变量"


def test_get_agent_drops_model_for_the_fake_runtimes(monkeypatch):
    """mock / pseudo 的模型标识就是它们的身份（进缓存 key），`--model` 对它们无意义。"""
    monkeypatch.delenv("TONGTU_AGENT", raising=False)

    assert get_agent("mock", model=MODEL).model == "mock"
    assert get_agent("pseudo", model=MODEL).model == "pseudo"


def test_get_agent_pseudo_is_the_chinese_path_variant(monkeypatch):
    """`pseudo` 与 `mock` 同族但互相独立：译文带中文、模型标识不同（tests/test_e2e_pseudo.py）。"""
    monkeypatch.delenv("TONGTU_AGENT", raising=False)
    agent = get_agent("pseudo")

    assert isinstance(agent, PseudoAgent) and isinstance(agent, MockAgent)
    assert agent.model == "pseudo" != get_agent("mock").model


def test_get_agent_passes_kwargs_and_rejects_unknown_names(monkeypatch):
    monkeypatch.delenv("TONGTU_AGENT", raising=False)
    agent = get_agent("codex", cli="codex-next", model="gpt-x")
    assert isinstance(agent, CodexAgent) and agent.cli == "codex-next"

    with pytest.raises(ValueError) as excinfo:
        get_agent("gpt-cli")
    assert "codex" in str(excinfo.value) and "mock" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 模型必须显式指定（缓存 key 认它）
# --------------------------------------------------------------------------- #


def test_codex_refuses_to_be_built_without_a_model():
    """空 model 会让缓存 key 记空串，而 CLI 那边照样换着模型跑——必须当场拒绝。"""
    for kwargs in ({}, {"model": ""}, {"model": "   "}):
        with pytest.raises(CodexError) as excinfo:
            CodexAgent(**kwargs)
        assert excinfo.value.kind == "no_model"
        assert "缓存" in str(excinfo.value) and "--model" in str(excinfo.value)

    with pytest.raises(CodexError):
        get_agent("codex")  # 工厂不许绕过这一条


def test_codex_model_is_trimmed_and_reaches_argv(tmp_path):
    agent = CodexAgent(model=f"  {MODEL} ", runner=runner())

    assert agent.model == MODEL
    agent.complete("规则", "正文")
    assert flag_value(list(agent.runner.calls[0].argv), "--model") == MODEL  # type: ignore[attr-defined]


# CLI 侧 --agent / --model 的透传测试随 CLI 接线（docs/BACKLOG.md）补回，当前 CLI
# 为占位实现，接口测试在 tests/test_cli.py。


# --------------------------------------------------------------------------- #
# 真 subprocess（默认 runner）
# --------------------------------------------------------------------------- #


def test_subprocess_runner_reports_missing_tool():
    result = subprocess_runner(["tongtu-no-such-binary-42"])

    assert result.status == MISSING_TOOL and not result.ok


@pytest.mark.skipif(shutil.which("cat") is None, reason="没有 cat")
def test_default_runner_really_runs_a_subprocess(tmp_path):
    """不依赖 codex 也能走通默认 runner：`cat` 把 stdin 原样吐回 stdout。"""
    agent = CodexAgent(model=MODEL, cli="cat", complete_argv=(("{cli}",),))

    assert agent.complete("规则", "正文") == "规则\n\n正文"
    assert agent.calls[0].status == OK


@pytest.mark.skipif(shutil.which("codex") is None, reason="本机没有 codex CLI（预期）")
def test_real_codex_cli_answers(tmp_path):
    agent = CodexAgent(model=REAL_MODEL, log_dir=tmp_path / "logs", complete_timeout=120.0)

    assert agent.complete("只回答一个词：ok", "").strip()
