"""伪翻译（pseudo-translation）e2e：PseudoAgent 给每段前缀一句固定中文，
三篇 fixture 全流水线跑到底。

恒等翻译 e2e（`tests/test_e2e_identity.py`）的译文**不含一个中文字**，于是 xeCJK 断行、
CJK 字体、`\\XeTeXlinebreaklocale` 这条路它一寸也盖不到（架构 §12 层 2 的注、附录 B 开放
问题 2）。本文件补上那一段覆盖，也是开放问题 2 的落点：**伪翻译变体**（该词出自架构附录 B）而不是
另造一篇中文 fixture——同一批 fixture、同一条流水线、同一套断言，只把 agent 换成
:class:`~tongtu.agent.mock.PseudoAgent`，仍旧零 LLM、零随机。

两种形态与恒等 e2e 同参数化：

* **fake**：假 latexmk 把 tex 原样写进「PDF」，于是能断言中文一路走到编译输入，且全流水线
  出包、产物 schema 全过；
* **real**（`@pytest.mark.compile`，CI texlive 镜像）：**这才是中文路径的真正覆盖点**——
  真 xelatex 拿含中文的 `zh.tex` 编出非空 PDF，且日志里没有一个「有字无形」的
  `There is no 以 in font …`（豆腐即字体链断了，见 `inject_cjk.XECJK_BODY`）。

## 这个变体凭什么不是「乱塞字符」

前缀句只由汉字与全角句号组成，不含 `\\` `{` `}` `$` `⟦` `⟧`，且只插在**散文段**的段首
（结构行开头的段一律跳过），于是：

1. `validate` 四层（占位符 / 控制序列 / 括号与行内数学 / 段落数）逐项不变——译文全绿、
   零重试，翻译内环里跑出的失败一定是流水线自己的 bug；
2. 删掉全部前缀句即逐字节回到 `flat.tex`——恒等 e2e 那条字节级等式的中文注入版本；
3. 中文绝不落到 `\\documentclass` 之前或 `\\begin{itemize}` 与首个 `\\item` 之间，故
   real 形态里编译**该过**，编不过就是真出了问题。
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest
from test_e2e_identity import CONTRACT_FILES, CONTRACT_SCHEMAS, FIXTURES, MODES, PAPERS, RUN_STAGES

from tongtu import CONTRACT_VERSION
from tongtu.agent import get_agent
from tongtu.agent.mock import PSEUDO_PREFIX, PseudoAgent
from tongtu.pipeline import run_pipeline
from tongtu.schema_check import load_schema, validate_schema
from tongtu.stages.compile import LOG_NAME
from tongtu.stages.inject_cjk import BEGIN_MARK, inject
from tongtu.workdir import Workdir

#: 中日韩统一表意文字（含扩展 A）——「译文里到底有没有中文」的机械判据。
CJK_RE = re.compile(r"[㐀-䶿一-鿿]")


def run(paper: str, workdir: Path):
    """跑一篇 fixture，agent 换成中文注入变体。返回 `(PipelineResult, 事件列表)`。"""
    stream = io.StringIO()
    result = run_pipeline(
        PAPERS / paper,
        workdir=workdir,
        json_events=True,
        out=stream,
        agent=get_agent("pseudo"),
    )
    events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    return result, events


@pytest.mark.parametrize("tools", MODES, indirect=True)
@pytest.mark.parametrize("paper", FIXTURES)
def test_pseudo_translation_e2e(paper, tools, tmp_path):
    """含中文的译文全流水线跑到底：validate 全绿、出包、（real 形态）真 xelatex 出 PDF。"""
    workdir = tmp_path / "work" / paper
    result, events = run(paper, workdir)

    assert result.exit_code == 0, result.message
    assert result.status == "ok", result.message
    paper_dir = Workdir(path=workdir, arxiv_id=paper)
    statuses = {s.stage: s.status for s in result.stages}
    assert statuses == {name: "ok" for name in RUN_STAGES}

    # --- 译文里真的有中文 ------------------------------------------------
    memory = json.loads((paper_dir.build / "zh-chunks" / "chunks.json").read_text("utf-8"))
    assert {c["status"] for c in memory["chunks"]} == {"translated"}
    with_cjk = [c for c in memory["chunks"] if CJK_RE.search(c["translation"])]
    assert len(with_cjk) >= 2, "至少两块该带上中文，不然这个变体等于没跑"

    # --- 中文没把 validate 四层碰坏（这个变体成立的前提）------------------
    translate_manifest = json.loads(paper_dir.manifest_path("translate").read_text(encoding="utf-8"))
    translate_result = translate_manifest["result"]
    assert translate_result["fallback"] == 0, translate_result
    assert translate_result.get("failures_by_check", {}) == {}
    assert translate_result["attempts"] == translate_result["chunk_count"], "四层全绿就不该重试"
    assert translate_manifest["inputs"]["model"] == PseudoAgent().model != "mock", (
        "中文注入与恒等 mock 的翻译记忆必须彼此独立，否则两个变体会命中对方的缓存"
    )

    # --- survey 照旧走确定性降级骨架（变体只改译文，不改分支）------------
    survey_result = json.loads(paper_dir.manifest_path("survey").read_text("utf-8"))["result"]
    assert survey_result["degraded"] is True, "本变体返回的同样不是 JSON，仍该走骨架"

    # --- 字节级：本变体只是往散文段里插了句中文 --------------------------
    flat = (paper_dir.build / "flat.tex").read_text(encoding="utf-8")
    zh_raw = (paper_dir.build / "zh-raw.tex").read_text(encoding="utf-8")
    assert zh_raw.count(PSEUDO_PREFIX) >= 5, "三篇 fixture 每篇都不止五个散文段"
    assert zh_raw.replace(PSEUDO_PREFIX, "") == flat, (
        "删掉全部前缀句必须逐字节回到 flat.tex——多出或少掉任何字节都是流水线丢了东西"
    )

    # --- zh.tex：中文 + xeCJK 配置块 -------------------------------------
    zh_tex = (paper_dir.build / "zh" / "zh.tex").read_text(encoding="utf-8")
    assert CJK_RE.search(zh_tex), "编译输入里没有中文，这个变体就白跑了"
    assert BEGIN_MARK in zh_tex and "xeCJK" in zh_tex
    assert inject(zh_raw).text == zh_tex

    # --- PDF -------------------------------------------------------------
    pdf = paper_dir.build / "zh" / "zh.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")
    if tools == "fake":
        # 假 latexmk 把 tex 原样写进「PDF」：中文确实是被喂给编译器的那份。
        assert PSEUDO_PREFIX.encode("utf-8") in pdf.read_bytes()
    else:
        assert pdf.stat().st_size > 1000, "真 PDF 不该这么小"
        log_path = paper_dir.logs / LOG_NAME
        assert log_path.is_file(), "真编译该把 latexmk 日志归档进 logs/"
        log = log_path.read_text(encoding="utf-8", errors="replace")
        for char in PSEUDO_PREFIX:
            assert f"There is no {char} in font" not in log, f"xelatex 排不出「{char}」——字体链断了，PDF 里是豆腐"

    # --- 产物包（与恒等 e2e 同一份契约）----------------------------------
    out = paper_dir.out
    for name in CONTRACT_FILES:
        assert (out / name).is_file(), f"产物包缺契约文件 {name}"
    for name, schema_name in CONTRACT_SCHEMAS.items():
        document = json.loads((out / name).read_text(encoding="utf-8"))
        assert validate_schema(document, load_schema(schema_name)) == [], name
        assert document["contract_version"] == CONTRACT_VERSION
    assert CJK_RE.search((out / "zh.tex").read_text(encoding="utf-8"))

    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "ok" and report["compile"]["passed"] is True
    assert report["validation"].get("failures_by_check", {}) == {}

    # --- 事件流仍过 schema ------------------------------------------------
    schema = load_schema("events")
    assert events and events[-1]["event"] == "result" and events[-1]["exit_code"] == 0
    for event in events:
        assert validate_schema(event, schema) == [], event
    assert events[-1]["fallback_chunks"] == 0
