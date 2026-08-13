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
import shutil
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

#: 编排器实际会跑的阶段——M4 起十个全跑，一个不跳。
RUN_STAGES = (
    "fetch", "flatten", "baseline", "mask", "survey", "chunk", "translate", "compile",
    "figures", "export",
)
SKIPPED: tuple[str, ...] = ()

#: 产物包顶层的契约文件（架构 §7 那张表）。`zh.synctex.gz` 不在其列——它要真 xelatex
#: 才有，假编译器路径下缺席是**预期内**的，anchors 因此走页级降级。
CONTRACT_FILES = (
    "zh.tex", "zh.pdf", "blocks.json", "chunks.json", "brief.json", "glossary.json",
    "anchors.json", "report.json", "report.html",
)

#: 过 schema 的那几份（名字 → schema 名）。
CONTRACT_SCHEMAS = {
    "blocks.json": "blocks",
    "chunks.json": "chunks",
    "brief.json": "brief",
    "glossary.json": "glossary",
    "anchors.json": "anchors",
    "report.json": "report",
    "figures/figures.json": "figures",
}

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
# 跑一篇
#
# 假 latexpand / 假 latexmk 在 `tests/conftest.py` 的 `tools` 夹具里（M3 起翻译记忆与
# 六关节的测试也要用同一份假工具链，故抬到 conftest 共用）。
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
    assert [s.stage for s in result.stages] == list(RUN_STAGES)
    assert all(statuses[name] == "ok" for name in RUN_STAGES), statuses

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

    # --- 翻译记忆：首跑必然全 miss（架构 §4）----------------------------
    assert translate_result["cache_hits"] == 0, "第一次跑不可能命中缓存"
    assert translate_result["cache_misses"] == translate_result["chunk_count"]
    assert result.cache_hits == 0 and result.cache_misses == result.chunks_total
    assert translate_result["memory"]["loaded"] == 0
    assert len({c["cache_key"] for c in memory["chunks"]}) == len(memory["chunks"]), (
        "每块一个 cache_key，重复即意味着两块会互相冒充"
    )

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
    assert pdf.is_file()
    assert pdf.read_bytes().startswith(b"%PDF")
    if tools == "real":
        assert pdf.stat().st_size > 1000, "真 PDF 不该这么小"
        assert (paper_dir.build / "baseline" / "flat.pdf").stat().st_size > 1000

    # --- 产物包（零期验收判据：契约齐全 + 全部过 schema）------------------
    out = paper_dir.out
    assert result.pdf == out / "zh.pdf", "交付路径指向产物包，不是 build/"
    assert result.report == out / "report.json"
    for name in CONTRACT_FILES:
        assert (out / name).is_file(), f"产物包缺契约文件 {name}"
    assert (out / "zh.tex").read_text("utf-8") == zh_tex, "顶层 zh.tex 就是编译的那一份"
    assert (out / "zh.pdf").read_bytes() == pdf.read_bytes()
    for name, schema_name in CONTRACT_SCHEMAS.items():
        document = json.loads((out / name).read_text(encoding="utf-8"))
        errors = validate_schema(document, load_schema(schema_name))
        assert errors == [], (name, errors)
        assert document["contract_version"] == CONTRACT_VERSION

    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "ok" and report["compile"]["passed"] is True
    assert report["validation"]["chunks_total"] == result.chunks_total
    assert report["validation"]["mask_roundtrip_ok"] is True
    assert report["compile"]["inject"]["branch"] == "inject"  # 契约新加的 inject 段
    assert [s["name"] for s in report["stages"]] == [n for n in RUN_STAGES if n != "export"]
    assert {a["path"] for a in report["artifacts"]} >= {"zh.tex", "zh.pdf", "anchors.json"}
    assert all(a["schema_valid"] is not False for a in report["artifacts"])

    # --- 自包含 pack：解包即可 latexmk 一条命令 --------------------------
    pack = out / "zh-pack"
    assert (pack / "zh.tex").read_text("utf-8") == zh_tex, "包里那份与顶层逐字节相同"
    assert (pack / "README.md").is_file()
    assert (pack / "fonts").is_dir(), "缺字体的包在别人机器上编出来全是豆腐"
    assert not (pack / "zh.pdf").exists(), "编译产物不进包（顶层已有一份）"
    for asset in (paper_dir.src).iterdir():
        if asset.is_dir() and asset.name != "__MACOSX":
            assert (pack / asset.name).is_dir(), f"包里缺源码资产目录 {asset.name}"

    # --- anchors：假编译器没有 synctex → 页级降级路径 --------------------
    anchors = json.loads((out / "anchors.json").read_text(encoding="utf-8"))
    assert anchors["anchors"], "一篇论文不可能一个锚点都没有"
    assert anchors["coordinate_system"] == {"origin": "top-left", "unit": "pt"}
    if tools == "fake":
        assert not (out / "zh.synctex.gz").exists()
        assert {a["source"] for a in anchors["anchors"]} == {"blocks"}, "没有 synctex 就该降级"
        assert all(a["confidence"] < 0.5 for a in anchors["anchors"])
        assert all(a["rects"] for a in anchors["anchors"])
    elif (out / "zh.synctex.gz").is_file():
        # 真 latexmk 带 `-synctex=1`（compiler.LATEXMK_FLAGS），于是这一路该出精确矩形。
        # 写成条件断言而非硬断言：某些 TeX 发行版会把映射文件清掉，那时降级路径照样成立。
        assert "synctex" in {a["source"] for a in anchors["anchors"]}
    assert any(a["type"] == "section" for a in anchors["anchors"]), "章节锚点是导航的主力"
    assert {a["id"] for a in anchors["anchors"]}.__len__() == len(anchors["anchors"])

    # --- 检验页 ----------------------------------------------------------
    page = (out / "report.html").read_text(encoding="utf-8")
    assert 'src="vendor/pdfjs/pdf.min.js"' in page
    assert 'src="report-data.js"' in page
    assert (out / "vendor" / "pdfjs" / "pdf.min.js").is_file()
    assert (out / "vendor" / "pdfjs" / "pdf.worker.min.js").is_file()
    data = (out / "report-data.js").read_text(encoding="utf-8")
    assert data.startswith("/*") and "window.TONGTU_REPORT = {" in data

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
    assert final["report"] == str(out / "report.json")
    assert final["out_dir"] == str(out)
    assert final["chunks_total"] == translate_result["chunk_count"] > 0
    assert final["fallback_chunks"] == 0
    progress = [e for e in events if e["event"] == "chunk_progress"]
    assert {e["status"] for e in progress} == {"started", "translated"}
    assert len(progress) == 2 * final["chunks_total"]

    # --- 幂等：原样重跑一次，全部命中 manifest --------------------------
    again, again_events = run(paper, workdir)
    assert again.exit_code == 0
    assert {s.stage: s.status for s in again.stages} == {
        name: "cached" for name in RUN_STAGES
    }
    assert again.pdf == out / "zh.pdf" and again.chunks_total == result.chunks_total
    assert [e["event"] for e in again_events if e["event"] == "chunk_progress"] == []
    for event in again_events:
        assert validate_schema(event, schema) == []


#: 阶段序里出现在事件流中的全部阶段。
STAGES_TOTAL = RUN_STAGES


def test_force_recomputes_everything(tools, tmp_path):
    """`--force` 无视缓存全量重跑（架构 §6）。fake 模式即可验证，不值得烧真编译。"""
    workdir = tmp_path / "work" / "article"
    first, _ = run("article", workdir)
    assert first.exit_code == 0
    cached, _ = run("article", workdir)
    assert all(s.status in ("cached", "skipped") for s in cached.stages)

    forced, events = run("article", workdir, force=True)

    assert forced.exit_code == 0
    assert {s.stage: s.status for s in forced.stages} == {name: "ok" for name in RUN_STAGES}
    assert [e for e in events if e["event"] == "chunk_progress"], "重算就该重新逐块翻译"
    # `--force` 连块级缓存一起无视：全量重翻，一条都不许命中（架构 §6）
    assert forced.cache_hits == 0 and forced.cache_misses == forced.chunks_total
    progress = [e for e in events if e["event"] == "chunk_progress"]
    assert "cached" not in {e["status"] for e in progress}

    # 再跑一次、只把 translate 的 manifest 抹掉：这次该全 hit（记忆还在盘上）
    (workdir / "build" / "manifests" / "translate.json").unlink()
    again, again_events = run("article", workdir)

    assert again.exit_code == 0
    assert {s.stage: s.status for s in again.stages}["translate"] == "ok", "manifest 没了就得重算"
    assert again.cache_hits == again.chunks_total and again.cache_misses == 0
    assert {
        e["status"] for e in again_events if e["event"] == "chunk_progress"
    } == {"cached"}, "全部块命中翻译记忆，一次也不该拉起关节⑤"


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
