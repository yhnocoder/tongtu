"""`tongtu doctor`（架构 §6/§10）。

本机与 CI 的 text job 都没有 TeX——doctor 必须在这种环境下明确报缺，而不是假装就绪。
为免受宿主环境影响，探测函数被打桩，两种环境（全缺 / 全有）都可复现。
"""

import pytest

from tongtu import cli


@pytest.fixture
def no_tex(monkeypatch):
    """PATH 中什么都没有：xelatex / latexmk / latexpand / fc-list 全缺。"""
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)


@pytest.fixture
def full_env(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_list_font_families", lambda: ["DejaVu Sans", "Noto Sans CJK SC"])


def test_doctor_reports_missing_and_exits_1(no_tex, capsys):
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    for tool in ("xelatex", "latexmk", "latexpand"):
        assert tool in out, f"输出未提及缺失的 {tool}"
    assert "中文字体链" in out
    assert "未通过" in out
    assert "fc-list 不可用" in out


def test_doctor_all_present_exits_0(full_env, capsys):
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "环境就绪" in out
    assert "Noto Sans CJK SC" in out


def test_doctor_missing_font_chain_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_list_font_families", lambda: ["DejaVu Sans"])
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "中文字体链" in out
    assert "Hiragino Sans GB" in out  # 探测链在报错里列出来，用户才知道装什么


def test_font_chain_detected_case_insensitively(monkeypatch):
    monkeypatch.setattr(cli, "_list_font_families", lambda: ["lxgw wenkai light"])
    check = cli._probe_fonts()
    assert check.passed


def test_unknown_subcommand_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["nope"])
    assert exc.value.code == 2


def test_bare_invocation_prints_help(capsys):
    assert cli.main([]) == 2
    assert "usage: tongtu" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["retranslate", "2401.01234", "--all"],
        ["preview", "2401.01234"],
        ["stage", "survey", "2401.01234"],  # 阶段名合法但本期占位跳过
        ["stage", "figures", "2401.01234"],
        ["stage", "export", "2401.01234"],
    ],
)
def test_unimplemented_commands_exit_2(argv, capsys):
    assert cli.main(argv) == 2
    assert "零期施工中" in capsys.readouterr().err


def test_run_and_stage_are_wired_to_the_pipeline(tmp_path, capsys):
    """`run` / `stage` 已接线（M2）：失败走结构化退出码 1，不再是「尚未实现」的 2。

    工作目录一律指到 tmp_path——测试绝不在仓库里、也不在用户默认目录下建论文目录。
    """
    empty = tmp_path / "nothing-here"
    assert cli.main(["stage", "mask", "2401.01234", "--workdir", str(empty)]) == 1
    out = capsys.readouterr().out
    assert "先跑 fetch" in out and "failed" in out


def test_retranslate_requires_a_scope():
    with pytest.raises(SystemExit) as exc:
        cli.main(["retranslate", "2401.01234"])
    assert exc.value.code == 2


def test_stage_name_is_validated():
    with pytest.raises(SystemExit) as exc:
        cli.main(["stage", "nosuchstage", "2401.01234"])
    assert exc.value.code == 2


def test_json_flag_accepted_on_either_side():
    """--json 放子命令前后都算数，缺省为 False（事件流本身零期后续接）。"""
    assert cli.parse_args(["--json", "run", "x"]).json is True
    assert cli.parse_args(["run", "x", "--json"]).json is True
    assert cli.parse_args(["run", "x"]).json is False
    assert cli.parse_args(["doctor"]).json is False


def test_run_arguments_are_captured():
    args = cli.parse_args(
        ["run", "2401.01234", "--glossary", "a.json", "--glossary", "b.json", "--workdir", "/w", "--force"]
    )
    assert (args.target, args.glossary, args.workdir, args.force) == (
        "2401.01234",
        ["a.json", "b.json"],
        "/w",
        True,
    )
