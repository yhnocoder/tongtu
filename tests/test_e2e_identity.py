"""恒等翻译 e2e：MockAgent 原样返回源文，三篇 fixture 论文全流水线跑到底（架构 §12 层 2）。

这是**零期交付判据的 CI 化身**（PHASE0 §1 第 1 条）：零 LLM 成本覆盖 fetch → flatten →
baseline → mask → chunk → translate → compile 全链路，外加阶段级增量（manifest 命中即
跳过）与 `--json` 事件流的 schema 校验。

两种形态，同一套断言：

* **fake**（本机、PR 可跑）：往 PATH 前面塞假 `latexpand`（真拼 `\\input`）与假 `latexmk`
  （把 tex 原样写进「PDF」），于是**除这两个外部程序外的全部代码路径都是真的**——掩码、
  分块、翻译内环、回填、注入、编译回环、manifest、事件流一个不落；
* **real**（CI 参考镜像）：同样的参数化跑真 latexmk / latexpand，断言真 PDF 非空。
  打 `@pytest.mark.compile`，本机没装即 skip（`uv run pytest -m compile` 是 CI 的编译层 job）。

## 恒等翻译能断言什么

MockAgent 的 `complete` 恒等返回，于是译文掩码流**逐字节等于**原掩码流，回填之后
`build/zh-raw.tex` 必然逐字节等于 `build/flat.tex`——这条等式一旦破了，掩码 / 分块 /
翻译驱动器三者之一就丢了字节，而且当场能指出丢在哪。注入块是唯一允许的差异（它是
compile 有意加的），故 `zh.tex` 去掉 tongtu 标记之间的那一段之后仍要等于 `zh-raw.tex`。

## 零第三方依赖的 schema 校验

仓库运行时与 dev 都不引 jsonschema（架构 §13），校验器是自家的**够用子集**
（`tongtu.schema_check`）。它原本住在本文件里，M3 起 survey 阶段要在运行时校验
`brief.json` / `glossary.json`，于是抽进运行时包——测试与生产用同一把尺子。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from tongtu import CONTRACT_VERSION
from tongtu.pipeline import Pipeline, run_pipeline, run_stage
from tongtu.schema_check import load_schema, validate_schema
from tongtu.stages.inject_cjk import BEGIN_MARK, END_MARK, inject
from tongtu.workdir import Workdir

ROOT = Path(__file__).resolve().parents[1]
PAPERS = ROOT / "tests" / "fixtures" / "papers"
SCHEMAS = ROOT / "docs" / "schemas"

#: 三篇 fixture（PHASE0 §3.7）。
FIXTURES = ("article", "revtex", "conference")

#: 编排器实际会跑的阶段（figures / export 本期占位跳过）。
RUN_STAGES = (
    "fetch", "flatten", "baseline", "mask", "survey", "chunk", "translate", "compile",
)
SKIPPED = ("figures", "export")

HAS_TEX = shutil.which("latexmk") is not None and shutil.which("latexpand") is not None

MODES = [
    pytest.param("fake", id="fake"),
    pytest.param(
        "real",
        id="real",
        marks=[
            pytest.mark.compile,
            pytest.mark.skipif(not HAS_TEX, reason="本机没有 latexmk / latexpand"),
        ],
    ),
]


def test_the_schema_checker_actually_rejects():
    """先验一下校验器本身——不然「全绿」可能只是它什么也没查。"""
    schema = load_schema("events")
    good = {"contract_version": "0.1", "event": "stage_start", "ts": "now", "stage": "mask"}
    assert validate_schema(good, schema) == []
    assert validate_schema({**good, "event": "nope"}, schema)  # 不认识的事件类型
    assert validate_schema({**good, "bogus": 1}, schema)  # additionalProperties: false
    assert validate_schema({k: v for k, v in good.items() if k != "stage"}, schema)  # 缺必填
    assert validate_schema({**good, "contract_version": "x"}, schema)  # pattern


# --------------------------------------------------------------------------- #
# 假 latexpand / 假 latexmk
# --------------------------------------------------------------------------- #

FAKE_LATEXPAND = r'''#!/usr/bin/env python3
"""最小 latexpand 替身：递归拼 \input，按需内联 .bbl，结果回显到 stdout。"""
import re, sys
from pathlib import Path

argv = sys.argv[1:]
main, bbl = None, None
i = 0
while i < len(argv):
    if argv[i] == "--expand-bbl":
        bbl = argv[i + 1]
        i += 2
        continue
    if argv[i].startswith("--"):
        i += 1
        continue
    main = argv[i]
    i += 1

INPUT = re.compile(r"\\(?:input|include)\{([^}]*)\}")


def expand(path: Path) -> str:
    text = path.read_text(encoding="utf-8")

    def sub(match):
        name = match.group(1)
        target = Path(name if name.endswith(".tex") else name + ".tex")
        return expand(target)

    return INPUT.sub(sub, text)


text = expand(Path(main))
if bbl:
    # 替换文本用 lambda 递进去：.bbl 里全是反斜杠，当成 re 的替换模板会被当转义解释
    body = Path(bbl).read_text(encoding="utf-8")
    text = re.sub(r"\\bibliography\{[^}]*\}", lambda m: body, text)
sys.stdout.write(text)
'''

FAKE_LATEXMK = r'''#!/usr/bin/env python3
"""最小 latexmk 替身：把 tex 原样写进「PDF」，日志里没有 ! 错误，退出 0。"""
import sys
from pathlib import Path

tex = Path([a for a in sys.argv[1:] if not a.startswith("-")][-1])
if not tex.is_file():
    sys.stderr.write("fake latexmk: %s not found\n" % tex)
    sys.exit(1)
Path(tex.stem + ".log").write_text(
    "This is fake latexmk\nOutput written on %s.pdf (3 pages).\n" % tex.stem, encoding="utf-8"
)
Path(tex.stem + ".pdf").write_bytes(
    b"%PDF-1.4\n" + tex.read_bytes() + b"\n%%EOF\n"
)
'''


def _install(bindir: Path, name: str, body: str) -> None:
    script = bindir / name
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def tools(request, tmp_path, monkeypatch):
    """fake 模式：把假 latexpand / 假 latexmk 塞到 PATH 最前面；real 模式：什么也不做。"""
    mode = getattr(request, "param", "fake")
    if mode == "fake":
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _install(bindir, "latexpand", FAKE_LATEXPAND)
        _install(bindir, "latexmk", FAKE_LATEXMK)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return mode


# --------------------------------------------------------------------------- #
# 跑一篇
# --------------------------------------------------------------------------- #


def run(paper: str, workdir: Path, *, force: bool = False):
    """跑一篇 fixture，返回 `(PipelineResult, 事件列表)`。工作目录一律在 tmp_path 下。"""
    stream = io.StringIO()
    result = run_pipeline(
        PAPERS / paper,
        workdir=workdir,
        force=force,
        json_events=True,
        out=stream,
    )
    events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    return result, events


def strip_injection(text: str) -> set[str]:
    """去掉 inject_cjk 的注入块，返回「连分隔换行一起去掉」的几种可能。

    注入块夹在 tongtu 标记注释之间，前面还有 inject 加的分隔（块自起一行并与上文空一行，
    1 或 2 个换行，取决于注入点原本是不是行首）。这里把两种都算出来，由调用方断言其一
    等于回填后的原文——比在测试里复刻 `_separator` 的分支更不容易随实现漂移。
    """
    start = text.index(BEGIN_MARK)
    end = text.index(END_MARK) + len(END_MARK)
    if text[end : end + 1] == "\n":
        end += 1
    head, tail = text[:start], text[end:]
    out = {head + tail}
    for sep in ("\n", "\n\n"):
        if head.endswith(sep):
            out.add(head[: -len(sep)] + tail)
    return out


@pytest.mark.parametrize("tools", MODES, indirect=True)
@pytest.mark.parametrize("paper", FIXTURES)
def test_identity_translation_e2e(paper, tools, tmp_path):
    """三篇 fixture 全流水线跑到底：恒等译文出 PDF，字节级可核对。"""
    workdir = tmp_path / "work" / paper
    result, events = run(paper, workdir)

    assert result.exit_code == 0, result.message
    assert result.status == "ok", result.message
    paper_dir = Workdir(path=workdir, arxiv_id=paper)

    # --- 阶段账 ---------------------------------------------------------
    statuses = {s.stage: s.status for s in result.stages}
    assert [s.stage for s in result.stages] == list(RUN_STAGES) + list(SKIPPED)
    assert all(statuses[name] == "ok" for name in RUN_STAGES), statuses
    assert all(statuses[name] == "skipped" for name in SKIPPED), statuses

    # --- 阶段 manifest 落盘（架构 §4）-----------------------------------
    for name in RUN_STAGES:
        path = paper_dir.manifest_path(name)
        assert path.is_file(), f"{name} 没落 manifest"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["stage"] == name
        assert manifest["inputs"], f"{name} 的 manifest 没有输入 hash"
        assert manifest["contract_version"] == CONTRACT_VERSION
        for entry in manifest["outputs"]:
            assert (workdir / entry["path"]).exists(), f"{name} 的输出 {entry['path']} 不见了"
    for name in SKIPPED:
        assert not paper_dir.manifest_path(name).exists(), f"{name} 没实现却落了 manifest"

    # --- 掩码往返自检（架构 §3.1 第 3 条）-------------------------------
    mask_manifest = json.loads(paper_dir.manifest_path("mask").read_text(encoding="utf-8"))
    assert mask_manifest["result"]["roundtrip_ok"] is True
    blocks = json.loads((paper_dir.build / "blocks.json").read_text(encoding="utf-8"))
    assert blocks["roundtrip_ok"] is True
    assert blocks["blocks"], "一篇论文不可能一个掩码块都没有"

    # --- survey 的两份产物（MockAgent 恒等返回 → 降级骨架路径）-----------
    survey_result = json.loads(paper_dir.manifest_path("survey").read_text(encoding="utf-8"))[
        "result"
    ]
    assert survey_result["degraded"] is True, "恒等 mock 返回的不是 JSON，只能走确定性骨架"
    brief = json.loads((paper_dir.build / "brief.json").read_text(encoding="utf-8"))
    decided = json.loads((paper_dir.build / "glossary.json").read_text(encoding="utf-8"))
    assert validate_schema(brief, load_schema("brief")) == []
    assert validate_schema(decided, load_schema("glossary")) == []
    assert brief["abstract"], "摘要是程序从源码照录的，降级也不该丢"
    assert brief["sections"], "章节树是确定性扫出来的，降级也该在"

    # --- 恒等译文过 validate 全绿 ---------------------------------------
    translate_result = json.loads(
        paper_dir.manifest_path("translate").read_text(encoding="utf-8")
    )["result"]
    assert translate_result["fallback"] == 0, translate_result
    assert translate_result.get("failures_by_check", {}) == {}
    assert translate_result["attempts"] == translate_result["chunk_count"], "恒等译文不该重试"
    memory = json.loads((paper_dir.build / "zh-chunks" / "chunks.json").read_text("utf-8"))
    assert {c["status"] for c in memory["chunks"]} == {"translated"}
    assert validate_schema(memory, load_schema("chunks")) == []

    # --- 回填后与原文逐字节等价（恒等翻译的核心断言）--------------------
    flat = (paper_dir.build / "flat.tex").read_text(encoding="utf-8")
    masked = (paper_dir.build / "masked.tex").read_text(encoding="utf-8")
    zh_raw = (paper_dir.build / "zh-raw.tex").read_text(encoding="utf-8")
    assert "".join(c["translation"] for c in memory["chunks"]) == masked
    assert zh_raw == flat, "恒等译文回填后必须逐字节等于 flat.tex"

    # --- zh.tex 只比原文多一个注入块 ------------------------------------
    zh_tex = (paper_dir.build / "zh" / "zh.tex").read_text(encoding="utf-8")
    assert BEGIN_MARK in zh_tex and "xeCJK" in zh_tex
    compile_result = json.loads(paper_dir.manifest_path("compile").read_text("utf-8"))["result"]
    assert compile_result["inject"]["branch"] == "inject"
    assert zh_raw in strip_injection(zh_tex), "zh.tex 去掉注入块后应当逐字节等于原文"
    assert inject(zh_raw).text == zh_tex, "zh.tex 应当恰好是 inject(回填后的原文)"

    # --- PDF ------------------------------------------------------------
    pdf = paper_dir.build / "zh" / "zh.pdf"
    assert pdf.is_file() and result.pdf == pdf
    assert pdf.read_bytes().startswith(b"%PDF")
    if tools == "real":
        assert pdf.stat().st_size > 1000, "真 PDF 不该这么小"
        assert (paper_dir.build / "baseline" / "flat.pdf").stat().st_size > 1000

    # --- 事件流过 schema ------------------------------------------------
    schema = load_schema("events")
    assert events, "--json 一个事件都没发"
    for event in events:
        assert validate_schema(event, schema) == [], (event, validate_schema(event, schema))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "stage_start" and kinds[-1] == "result"
    assert kinds.count("result") == 1
    assert sum(1 for e in events if e["event"] == "stage_start") == len(STAGES_TOTAL)
    assert {e["run_id"] for e in events} == {events[0]["run_id"]}
    assert {e["arxiv_id"] for e in events} == {paper}
    final = events[-1]
    assert final["status"] == "ok" and final["exit_code"] == 0
    assert final["chunks_total"] == translate_result["chunk_count"] > 0
    assert final["fallback_chunks"] == 0
    progress = [e for e in events if e["event"] == "chunk_progress"]
    assert {e["status"] for e in progress} == {"started", "translated"}
    assert len(progress) == 2 * final["chunks_total"]

    # --- 幂等：原样重跑一次，全部命中 manifest --------------------------
    again, again_events = run(paper, workdir)
    assert again.exit_code == 0
    assert {s.stage: s.status for s in again.stages} == {
        **{name: "cached" for name in RUN_STAGES},
        **{name: "skipped" for name in SKIPPED},
    }
    assert again.pdf == pdf and again.chunks_total == result.chunks_total
    assert [e["event"] for e in again_events if e["event"] == "chunk_progress"] == []
    for event in again_events:
        assert validate_schema(event, schema) == []


#: 阶段序里出现在事件流中的全部阶段（含占位跳过的两个）。
STAGES_TOTAL = RUN_STAGES + SKIPPED


def test_force_recomputes_everything(tools, tmp_path):
    """`--force` 无视缓存全量重跑（架构 §6）。fake 模式即可验证，不值得烧真编译。"""
    workdir = tmp_path / "work" / "article"
    first, _ = run("article", workdir)
    assert first.exit_code == 0
    cached, _ = run("article", workdir)
    assert all(s.status in ("cached", "skipped") for s in cached.stages)

    forced, events = run("article", workdir, force=True)

    assert forced.exit_code == 0
    assert {s.stage: s.status for s in forced.stages} == {
        **{name: "ok" for name in RUN_STAGES},
        **{name: "skipped" for name in SKIPPED},
    }
    assert [e for e in events if e["event"] == "chunk_progress"], "重算就该重新逐块翻译"


def test_changing_the_source_invalidates_the_downstream(tools, tmp_path):
    """改源码 → 对应阶段起的下游重算，上游照样命中（架构 §4 的增量模型）。"""
    src = tmp_path / "src" / "article"
    shutil.copytree(PAPERS / "article", src)
    workdir = tmp_path / "work" / "article"

    first, _ = run(str(src), workdir)
    assert first.exit_code == 0

    body = src / "sections" / "intro.tex"
    body.write_text(body.read_text(encoding="utf-8") + "\n\nOne more synthetic paragraph.\n", "utf-8")

    again, _ = run(str(src), workdir)

    statuses = {s.stage: s.status for s in again.stages}
    assert statuses["fetch"] == "ok" and statuses["flatten"] == "ok"
    assert statuses["mask"] == "ok" and statuses["translate"] == "ok"
    assert again.exit_code == 0
    assert "One more synthetic paragraph." in (workdir / "build" / "zh-raw.tex").read_text("utf-8")


def test_pdf_only_terminates_with_a_nonzero_exit(tools, tmp_path):
    """PDF-only 是一条分支而不是崩溃：结构化终止，退出码非 0（架构 §3 fetch 行）。"""
    src = tmp_path / "src" / "shell"
    src.mkdir(parents=True)
    (src / "main.tex").write_text(
        "\\documentclass{article}\n\\usepackage{pdfpages}\n"
        "\\begin{document}\\includepdf[pages=-]{paper.pdf}\\end{document}\n",
        encoding="utf-8",
    )
    (src / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    result, events = run(str(src), tmp_path / "work" / "shell")

    assert result.exit_code == 1 and result.status == "failed"
    assert [s.stage for s in result.stages] == ["fetch"]
    assert "fallback" in result.message
    final = events[-1]
    assert final["event"] == "result" and final["status"] == "failed"
    assert final["exit_code"] == 1 and final["pdf"] is None
    for event in events:
        assert validate_schema(event, load_schema("events")) == []


def test_baseline_failure_stops_before_any_llm_spend(tools, tmp_path, monkeypatch):
    """原文编译不过 → env_failed 终止，translate 一次也没跑（架构 §3 baseline 行）。"""
    from tongtu.compiler import CompileRunResult

    def broken(tex, build_dir):
        return CompileRunResult(
            ok=False, log="! LaTeX Error: File `nope.sty' not found.\nl.3 \\usepackage\n",
            returncode=1, engine="pdflatex",
        )

    workdir = Workdir(path=tmp_path / "work" / "article", arxiv_id="article").create()
    events_out = io.StringIO()
    from tongtu.pipeline import Events

    agent = _CountingAgent()
    pipeline = Pipeline(
        workdir,
        target=str(PAPERS / "article"),
        events=Events(events_out, json_mode=True, arxiv_id="article"),
        compiler=broken,
        agent=agent,
    )
    result = pipeline.run()

    assert result.exit_code == 1 and result.status == "failed"
    assert [s.stage for s in result.stages] == ["fetch", "flatten", "baseline"]
    assert result.stage("baseline").status == "failed"
    assert agent.calls == 0, "编译门控之前不该产生任何 LLM 支出"
    assert not (workdir.build / "masked.tex").exists()


class _CountingAgent:
    """MockAgent 加一个调用计数——用来断言「编译不过的论文不花一分钱」。"""

    model = "counting-mock"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, text: str, model=None) -> str:
        self.calls += 1
        return text

    def as_session_fn(self):
        return lambda request: None


# --------------------------------------------------------------------------- #
# 单阶段入口
# --------------------------------------------------------------------------- #


def test_stage_entrypoint_loads_upstream_and_recomputes_one(tools, tmp_path):
    """`tongtu stage`：上游从盘上装载（cached），目标阶段无视 manifest 必算。"""
    workdir = tmp_path / "work" / "article"
    first, _ = run("article", workdir)
    assert first.exit_code == 0
    before = (workdir / "build" / "masked.tex").stat().st_mtime_ns

    stream = io.StringIO()
    result = run_stage("chunk", str(PAPERS / "article"), workdir=workdir, json_events=True, out=stream)

    statuses = {s.stage: s.status for s in result.stages}
    assert statuses == {
        "fetch": "cached",
        "flatten": "cached",
        "baseline": "cached",
        "mask": "cached",
        "survey": "cached",
        "chunk": "ok",
    }
    assert (workdir / "build" / "masked.tex").stat().st_mtime_ns == before, "上游不该被重写"
    for line in stream.getvalue().splitlines():
        assert validate_schema(json.loads(line), load_schema("events")) == []


def test_stage_entrypoint_can_rerun_survey(tools, tmp_path):
    """`tongtu stage survey`：M3 起是真阶段（曾经的占位跳过），单跑即重算通读。"""
    workdir = tmp_path / "work" / "article"
    assert run("article", workdir)[0].exit_code == 0
    brief = workdir / "build" / "brief.json"
    before = brief.read_text(encoding="utf-8")

    result = run_stage("survey", str(PAPERS / "article"), workdir=workdir, out=io.StringIO())

    assert {s.stage: s.status for s in result.stages}["survey"] == "ok"
    assert json.loads(brief.read_text(encoding="utf-8"))["sections"] == json.loads(before)[
        "sections"
    ], "同一篇论文重跑通读，章节树不该漂"


def test_stage_entrypoint_reports_missing_upstream(tools, tmp_path):
    """上游产物不在 → 结构化报错（说清先跑哪个阶段），不抛栈。"""
    result = run_stage(
        "compile", str(PAPERS / "article"), workdir=tmp_path / "work" / "empty", out=io.StringIO()
    )

    assert result.exit_code == 1
    failed = [s for s in result.stages if s.status == "failed"]
    assert failed and "先跑" in (failed[0].error or "")


def test_cli_run_returns_zero(tools, tmp_path, capsys):
    """CLI 全流程走一遍（`tongtu run <dir> --workdir ... --json`）。"""
    from tongtu.cli import main

    code = main(
        [
            "run",
            str(PAPERS / "revtex"),
            "--workdir",
            str(tmp_path / "work" / "revtex"),
            "--json",
        ]
    )

    assert code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[-1]["event"] == "result" and lines[-1]["exit_code"] == 0
