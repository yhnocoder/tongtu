"""`ci/llm_summary.py` 的机械测试：两条取数路径各跑一遍合成输入。

汇总脚本本身不在 tongtu 包里（它是 CI 胶水，见脚本头），但它有分支、有降级，**曾经**是
写在 workflow YAML 里的一段内联 Python——那种形态改一行就得推一次 CI 才知道对不对。抽成
文件之后就该像别的代码一样被钉住：

1. **report.json 路径**：数字照抄产物包里的权威统计；
2. **事件流降级路径**：没有 report.json 时从 `--json` 事件里数出回退与重试，数不出来的
   字段写 `—` 而不是 0，且表格下方要点名哪几篇是降级来的。
"""

import importlib.util
import json
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "ci" / "llm_summary.py"


def _load():
    """按路径装载脚本——`ci/` 不是包，装不进 `import` 的常规路径。"""
    spec = importlib.util.spec_from_file_location("llm_summary", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 要能从 sys.modules 里回查本模块
    spec.loader.exec_module(module)
    return module


llm_summary = _load()


def write(path: pathlib.Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")


def events_of(*records) -> str:
    """事件流文件的内容：JSON 行之间掺几行人类可读输出（真实 `--json` 输出就是这样）。"""
    lines = ["通途 v0.0.1：开跑"]
    lines.extend(json.dumps(r, ensure_ascii=False) for r in records)
    lines.append("跑完了")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 路径一：产物包里的 report.json
# --------------------------------------------------------------------------- #


def test_report_json_is_the_authority(tmp_path):
    qdir, home = tmp_path / "q", tmp_path / "home"
    write(
        qdir / "2401.00001.report.json",
        {
            "status": "ok",
            "validation": {"chunks_total": 42, "fallback": 2, "retries": 5},
            "compile": {"passed": True, "warnings": [{"count": 3}, {}]},
            "agent_interventions": [{"joint": "fixup"}, {"joint": "translate"}],
        },
    )
    write(qdir / "2401.00001.exit", "0\n")
    write(qdir / "2401.00001.events.jsonl", events_of({"event": "result", "duration_ms": 91_000}))

    row = llm_summary.collect("2401.00001", qdir, home)

    assert (row.status, row.chunks, row.fallback, row.retries) == ("ok", 42, 2, 5)
    assert row.interventions == 2
    assert row.warnings == 4, "缺 count 的警告条目按 1 计"
    assert row.compiled is True and row.exit_code == "0" and row.has_report

    line = row.to_markdown()
    assert line.startswith("| `2401.00001` | ok | 0 | 42 | 2 | 5 | 2 | 4 | 通过 | 91s |")


def test_report_json_under_tongtu_home_is_the_fallback_location(tmp_path):
    """存档步骤没来得及拷贝时，工作目录里的那份仍算数（但不算「有 report」）。"""
    qdir, home = tmp_path / "q", tmp_path / "home"
    write(home / "math_0601001" / "out" / "report.json", {"status": "failed"})
    qdir.mkdir(parents=True)

    row = llm_summary.collect("math/0601001", qdir, home)

    assert row.status == "failed", "旧式 arXiv id 的 `/` 要换成 `_` 才找得到目录"
    assert row.has_report is False


# --------------------------------------------------------------------------- #
# 路径二：`--json` 事件流降级
# --------------------------------------------------------------------------- #


def test_event_stream_fallback_counts_what_it_can(tmp_path):
    qdir, home = tmp_path / "q", tmp_path / "home"
    write(
        qdir / "2402.00002.events.jsonl",
        events_of(
            {"event": "chunk_progress", "status": "translated"},
            {"event": "chunk_progress", "status": "retry"},
            {"event": "chunk_progress", "status": "fallback"},
            {"event": "chunk_progress", "status": "fallback"},
            "{ 这行不是 JSON",
            {"event": "result", "status": "ok_with_fallback", "chunks_total": 4, "duration_ms": 1500},
        ),
    )
    write(qdir / "2402.00002.exit", "1\n")

    row = llm_summary.collect("2402.00002", qdir, home)

    assert row.status == "ok_with_fallback" and row.chunks == 4
    assert (row.fallback, row.retries) == (2, 1)
    assert row.interventions is None and row.warnings is None and row.compiled is None
    assert row.exit_code == "1" and row.has_report is False
    assert "| — | — | — | 2s |" in row.to_markdown(), "算不出的字段写 — 而不是 0"


def test_a_paper_with_nothing_at_all_still_gets_a_row(tmp_path):
    row = llm_summary.collect("2403.00003", tmp_path / "q", tmp_path / "home")

    assert row.to_markdown() == "| `2403.00003` | — | — | — | — | — | — | — | — | — |"


# --------------------------------------------------------------------------- #
# 整张表与命令行
# --------------------------------------------------------------------------- #


def test_render_names_the_degraded_papers():
    rows = [
        llm_summary.Row(paper="a", has_report=True),
        llm_summary.Row(paper="b", has_report=False),
    ]

    text = llm_summary.render(rows, agent="codex", model="", image="texlive/texlive:latest")

    assert "`codex`" in text and "(运行时默认)" in text
    assert text.count("\n| `") == 2
    assert "`b`" in text.rsplit(">", 1)[-1] and "`a`" not in text.rsplit(">", 1)[-1]


@pytest.mark.parametrize(
    "given, expected",
    [
        (["2401.1", "2402.2"], ["2401.1", "2402.2"]),
        (["2401.1 2402.2,2403.3"], ["2401.1", "2402.2", "2403.3"]),
        ([], []),
    ],
)
def test_paper_lists_accept_spaces_and_commas(given, expected, monkeypatch):
    monkeypatch.delenv("PAPERS", raising=False)

    assert llm_summary.parse_papers(given or None) == expected


def test_cli_writes_a_table_to_a_file(tmp_path, monkeypatch, capsys):
    qdir = tmp_path / "q"
    write(qdir / "2401.00001.report.json", {"status": "ok", "compile": {"passed": False}})
    out = tmp_path / "summary.md"

    code = llm_summary.main(
        [
            "--papers", "2401.00001",
            "--quality-dir", str(qdir),
            "--tongtu-home", str(tmp_path / "home"),
            "--agent", "mock",
            "--output", str(out),
        ]
    )

    assert code == 0 and capsys.readouterr().out == ""
    text = out.read_text(encoding="utf-8")
    assert text.startswith("## LLM 质量层试跑")
    assert "| `2401.00001` | ok |" in text and "未通过" in text


def test_cli_reads_the_workflow_env_by_default(tmp_path, monkeypatch, capsys):
    """workflow 里不重复传参：默认值就是它已经导出的那几个环境变量。"""
    qdir = tmp_path / "q"
    write(qdir / "2401.00001.exit", "0")
    monkeypatch.setenv("PAPERS", "2401.00001")
    monkeypatch.setenv("QUALITY_DIR", str(qdir))
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT", "codex")
    monkeypatch.setenv("MODEL", "some-model")
    monkeypatch.setenv("IMAGE", "ghcr.io/x/y:latest")

    assert llm_summary.main([]) == 0

    text = capsys.readouterr().out
    assert "`codex`" in text and "`some-model`" in text and "`ghcr.io/x/y:latest`" in text
    assert "| `2401.00001` | — | 0 |" in text


def test_the_script_runs_standalone():
    """`--help` 得能在裸 python 下跑起来（不 import tongtu，不依赖项目环境）。"""
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False
    )

    assert proc.returncode == 0 and "--quality-dir" in proc.stdout
