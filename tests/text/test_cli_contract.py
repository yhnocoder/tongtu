r"""CLI 调用约定：论文参数的三种形态、用法错误的退出码、`--json` 的当前行为、工作目录解析。

判据是 docs/CLI.md 与 docs/stages/fetch.md。这些是 wenshu 容器调度侧直接依赖的约定，且都
不需要 TeX 或网络——参数解析是纯函数，本地目录入口只做文件拷贝——因此归文本层。

`docs/stages/fetch.md` 的验收里，「`2002.05202` 走编号与链接两种写法」与「重拷语义正确」
两条此前没有对应用例；链接形态是 CLI.md 明列的三种输入形态之一。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tongtu import workdir
from tongtu.cli import EXIT_FAILURE, app
from tongtu.stages.fetch import PaperArgumentError, parse_arxiv_url, parse_paper_argument

from ..conftest import PAPERS_DIR, TONGTU_BIN

runner = CliRunner()

#: typer 对用法错误的退出码，docs/CLI.md 退出码表的第三行。
EXIT_USAGE = 2


# ------------------------------------------------------------------ 链接形态


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://arxiv.org/abs/2002.05202", "2002.05202"),
        ("http://arxiv.org/abs/2002.05202", "2002.05202"),
        ("https://arxiv.org/pdf/2002.05202", "2002.05202"),
        ("https://arxiv.org/html/2002.05202", "2002.05202"),
        # 末尾的 .pdf 扩展名去掉（/pdf/ 链接的旧写法）
        ("https://arxiv.org/pdf/2002.05202.pdf", "2002.05202"),
        # 版本号后缀原样保留
        ("https://arxiv.org/abs/2002.05202v1", "2002.05202v1"),
        # 编号本身含斜杠，取前缀之后的整段剩余路径
        ("https://arxiv.org/abs/hep-th/9901001", "hep-th/9901001"),
        # 查询串与锚点丢弃
        ("https://arxiv.org/abs/2002.05202?context=cs", "2002.05202"),
        ("https://arxiv.org/abs/2002.05202#comments", "2002.05202"),
        # 尾斜杠去掉
        ("https://arxiv.org/abs/2002.05202/", "2002.05202"),
        # 子域接受
        ("https://www.arxiv.org/abs/2002.05202", "2002.05202"),
    ],
)
def test_parse_arxiv_url(url: str, expected: str) -> None:
    """链接解析出的编号与 docs/stages/fetch.md 的规则一致。"""
    assert parse_arxiv_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/abs/2002.05202",  # 主机不对
        "https://arxiv-mirror.org/abs/2002.05202",  # 主机不对，且不是 arxiv.org 的子域
        "https://arxiv.org/list/cs.CL/2002",  # 路径无 /abs/、/pdf/、/html/ 前缀
        "https://arxiv.org/",  # 同上
        "https://arxiv.org/abs/",  # 前缀之后没有编号
    ],
)
def test_parse_arxiv_url_rejects(url: str) -> None:
    """主机不对、路径无前缀、前缀后无编号都判用法错误。"""
    with pytest.raises(PaperArgumentError):
        parse_arxiv_url(url)


def test_link_and_bare_id_resolve_to_the_same_paper() -> None:
    """链接写法与编号写法归结为同一个编号，此后完全同路。"""
    from_link = parse_paper_argument("https://arxiv.org/abs/2002.05202")
    from_id = parse_paper_argument("2002.05202")
    assert from_link.arxiv_id == from_id.arxiv_id == "2002.05202"
    assert from_link.source_dir is None and from_id.source_dir is None


# ------------------------------------------------------------------ 编号合法性


@pytest.mark.parametrize(
    "arxiv_id",
    ["", "   ", "2002.05202 v1", "with\ttab", "../escape", "a/../b", "/absolute", "~home", ".hidden", "back\\slash"],
)
def test_invalid_arxiv_id_is_rejected(arxiv_id: str) -> None:
    """空串、含空白、路径穿越形态一律拒绝——它们会被用作目录名。"""
    with pytest.raises((workdir.WorkdirError, PaperArgumentError)):
        parse_paper_argument(arxiv_id)


@pytest.mark.parametrize(
    ("arxiv_id", "directory"),
    [("2002.05202", "2002.05202"), ("2002.05202v1", "2002.05202v1"), ("hep-th/9901001", "hep-th_9901001")],
)
def test_arxiv_id_to_directory_name(arxiv_id: str, directory: str) -> None:
    """编号里的斜杠换成下划线，目录保持单层；版本号后缀原样保留。"""
    assert workdir.normalize_arxiv_id(arxiv_id) == directory


def test_local_directory_takes_precedence(tmp_path: Path) -> None:
    """三种形态按顺序识别，本地目录优先于编号。"""
    parsed = parse_paper_argument(str(tmp_path))
    assert parsed.source_dir == tmp_path
    assert not parsed.arxiv_id


# ------------------------------------------------------------------ 工作目录解析


def test_workdir_option_wins_over_home(tmp_path: Path) -> None:
    """`--workdir` 指的是论文工作目录本身，优先级高于 `$TONGTU_HOME`。"""
    resolved = workdir.resolve("2002.05202", workdir=tmp_path / "explicit", env={"TONGTU_HOME": str(tmp_path / "home")})
    assert resolved == (tmp_path / "explicit").absolute()


def test_home_env_used_when_no_option(tmp_path: Path) -> None:
    """未给 `--workdir` 时落在 `$TONGTU_HOME/<编号>`。"""
    resolved = workdir.resolve("2002.05202", env={"TONGTU_HOME": str(tmp_path)})
    assert resolved == (tmp_path / "2002.05202").absolute()


def test_default_root_when_env_absent() -> None:
    """两者都没有时用固定默认路径，不依赖操作系统的标准目录 API。"""
    resolved = workdir.resolve("2002.05202", env={})
    assert resolved == (Path("~/.local/share/tongtu").expanduser() / "2002.05202").absolute()


def test_workdir_requires_id_or_option() -> None:
    """既无编号也无 `--workdir` 时报错，不猜一个目录出来。"""
    with pytest.raises(workdir.WorkdirError):
        workdir.resolve()


# ------------------------------------------------------------------ 退出码与选项


@pytest.mark.parametrize("paper", ["", "../escape", "https://example.com/abs/2002.05202"])
def test_stage_rejects_bad_paper_argument(paper: str, tmp_path: Path) -> None:
    """论文参数不合法是用法错误，退 2 而不是 1。

    docs/stages/fetch.md：这些在做任何工作之前就能判定，不进入阶段状态。调用方据此区分
    「参数写错了」与「这篇论文处理失败」。
    """
    result = runner.invoke(app, ["stage", "fetch", paper, "--workdir", str(tmp_path)])
    assert result.exit_code == EXIT_USAGE, f"退 {result.exit_code}，输出：{result.output}"


def test_json_option_is_ignored_with_notice(tmp_path: Path) -> None:
    """`--json` 的事件流 schema 尚未定义：向 stderr 说明并忽略该选项，照常执行。

    docs/CLI.md 明确了这一当前行为。schema 冻结后这条用例要跟着改。
    """
    source = str(PAPERS_DIR / "article")
    completed = subprocess.run(
        [str(TONGTU_BIN), "stage", "fetch", source, "--workdir", str(tmp_path / "wd"), "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "schema" in completed.stderr, f"没有就 --json 给出说明：{completed.stderr!r}"
    assert (tmp_path / "wd" / "build" / "manifests" / "fetch.json").is_file(), "给了 --json 就没照常执行"


def test_stage_reports_missing_upstream(tmp_path: Path) -> None:
    """上游没跑时下游退 1 并说明，而不是抛栈或退 0。"""
    result = runner.invoke(app, ["stage", "flatten", "2002.05202", "--workdir", str(tmp_path)])
    assert result.exit_code == EXIT_FAILURE


# ------------------------------------------------------------------ 本地目录入口的重拷语义


def test_local_entry_recopies_source(tmp_path: Path) -> None:
    """本地目录入口每次重新拷贝：`src/` 里上次执行的残留文件在重跑后不复存在。

    docs/stages/fetch.md 的验收「重拷语义正确」。驱动器把 `src/` 整目录删除后重建，残留
    文件若留下来会混进下游的输入 hash 与主文件候选。
    """
    source = str(PAPERS_DIR / "article")
    paper_workdir = tmp_path / "wd"
    first = subprocess.run(
        [str(TONGTU_BIN), "stage", "fetch", source, "--workdir", str(paper_workdir)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert first.returncode == 0, first.stderr

    stale = paper_workdir / "src" / "stale-from-previous-run.tex"
    stale.write_text("% 上一次执行的残留\n", encoding="utf-8")

    second = subprocess.run(
        [str(TONGTU_BIN), "stage", "fetch", source, "--workdir", str(paper_workdir)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert not stale.exists(), "重跑 fetch 没有清掉 src/ 里的陈旧文件"
    assert (paper_workdir / "src" / "main.tex").is_file(), "重拷之后主文件应当在"
