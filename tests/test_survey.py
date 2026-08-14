"""survey 阶段驱动器：通读输入、防御性解析、降级骨架、接线（架构 §3、决策 11、关节④）。

survey 是流水线**最早**的 LLM 支出，也是唯一一个「失败也要继续」的阶段：brief 是增益不
是门禁。本文件按这两条组织断言——

* **通读输入**要真的省 token 又不丢信息：数学回填、表格/图保持占位符、附录与参考文献剔除；
* **模型那一路怎么坏都不许坏了流水线**：JSON 裹代码块 / 掺解释文字 / 截断 / 纯垃圾，
  依次走「解析 → 重试一次 → 降级为确定性骨架」，产物照样过 schema。

e2e（`tests/test_e2e_identity.py`）跑的是 MockAgent 恒等返回，那正好是「降级骨架」这一
路；成功与重试两路只有可编程的假 agent 能盖到。
"""

from __future__ import annotations

import io
import json

import pytest

from tongtu import glossary as gl
from tongtu.pipeline import Events, Pipeline, manifest_fresh, read_manifest
from tongtu.schema_check import check as schema_check
from tongtu.stages import survey as sv
from tongtu.stages.mask import mask
from tongtu.workdir import Workdir

SOURCE = """\
\\documentclass{article}
\\title{A Study of Placeholders}
\\begin{document}
\\maketitle

\\begin{abstract}
We study placeholders and the discontents thereof.
\\end{abstract}

\\section{Introduction}
\\label{sec:intro}

Placeholders matter because $x$ matters, as \\cite{knuth} shows.

\\begin{equation}
  \\label{eq:loss}
  \\mathcal{L} = \\sum_i x_i
\\end{equation}

\\subsection{Background}

Some background prose about the beam search baseline.

\\begin{figure}
  \\includegraphics{fig.png}
  \\caption{A figure nobody drew.}
\\end{figure}

\\section{Method}

The method paragraph mentions attention twice: attention.

\\appendix
\\section{Extra Derivations}

Appendix prose that must never reach the通读 model.

\\begin{thebibliography}{9}
\\bibitem{knuth} A reference.
\\end{thebibliography}
\\end{document}
"""

DECISION = {
    "paper": {"primary_category": "cs.CL"},
    "sections": [
        {
            "number": "1",
            "title": "Introduction",
            "level": 1,
            "summary": "介绍占位符问题。",
            "children": [{"title": "Background", "level": 2, "summary": "背景。"}],
        },
        {"number": "2", "title": "Method", "level": 1, "summary": "方法。"},
    ],
    "notation": [{"symbol": "\\mathcal{L}", "meaning": "损失函数", "first_seen": "1"}],
    "naming_conventions": [{"name": "the method", "convention": "统一译作「本文方法」"}],
    "style": {"tone": "严谨学术", "audience": "NLP 研究者", "notes": ["we 一律译作「我们」"]},
    "terms": [
        {"term": "attention", "translation": "注意力"},
        {"term": "beam search", "translation": "束搜索", "aliases": ["beam-search"]},
    ],
    "do_not_translate": [{"term": "LLaMA", "note": "模型名"}],
    "bogus_field": "模型多写的字段，清洗时应当丢掉",
}


def masked():
    result = mask(SOURCE)
    assert result.warnings == ()
    return result


def agent_of(*responses: str):
    """可编程的假 agent：按次返回预置输出，用完则重复最后一个。"""

    calls: list[str] = []

    def complete(prompt: str, text: str, model=None) -> str:
        calls.append(prompt)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def fenced(payload: dict) -> str:
    return "好的，结果如下：\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n完毕。"


# --------------------------------------------------------------------------- #
# 通读输入
# --------------------------------------------------------------------------- #


def test_reading_view_restores_math_and_keeps_heavy_blocks_masked():
    result = masked()

    view = sv.reading_view(result.masked, result)

    assert "\\mathcal{L} = \\sum_i x_i" in view, "记号约定住在行间公式里，必须回填"
    assert "⟦BLK-" in view and "includegraphics" not in view, "图是 token 大头，保持占位符"
    assert "A figure nobody drew." in view, "caption 文本仍要能读到（⟦CAP-n⟧ 行）"
    assert "$x$" in view and "\\cite{knuth}" in view


def test_reading_view_drops_the_appendix_and_the_bibliography():
    result = masked()

    view = sv.reading_view(result.masked, result)

    assert "Appendix prose" not in view
    assert "Extra Derivations" not in view
    assert "bibitem" not in view
    assert "Method" in view, "正文一节都不许丢"


def test_the_cut_also_finds_a_bare_bibliography_command():
    """没有附录、参考文献走 `\\bibliography{refs}` 的论文同样要剔干净。"""
    src = SOURCE.replace(
        "\\begin{thebibliography}{9}\n\\bibitem{knuth} A reference.\n\\end{thebibliography}\n",
        "\\bibliography{refs}\n",
    ).replace("\\appendix\n\\section{Extra Derivations}\n\nAppendix prose that must never reach the通读 model.\n\n", "")
    result = mask(src)

    view = sv.reading_view(result.masked, result)

    assert "\\bibliography{refs}" not in view
    assert "The method paragraph" in view


def test_appendix_is_cut_from_the_reading_view_but_still_present_in_the_stream():
    """附录不进通读，但**仍正常翻译**（架构决策 11）——掩码流一个字节都没少。"""
    result = masked()

    assert "Appendix prose" in result.masked
    assert sv.cut_offset(result.masked) < len(result.masked)


# --------------------------------------------------------------------------- #
# 原文照录
# --------------------------------------------------------------------------- #


def test_title_and_abstract_are_taken_from_the_source_verbatim():
    result = masked()

    title, abstract = sv.paper_facts(result.masked, result)

    assert title == "A Study of Placeholders"
    assert abstract == "We study placeholders and the discontents thereof."


def test_preamble_abstract_is_read_from_the_cap_slot():
    """有些 documentclass 要求 abstract 写在 `\\begin{document}` 之前（mask 抽成 CAP 槽位）。"""
    src = SOURCE.replace(
        "\\begin{document}\n\\maketitle\n\n\\begin{abstract}\nWe study placeholders and the discontents thereof.\n\\end{abstract}\n",
        "\\begin{abstract}\nPreamble abstract text.\n\\end{abstract}\n\\begin{document}\n\\maketitle\n",
    )
    result = mask(src)

    assert sv.paper_facts(result.masked, result)[1] == "Preamble abstract text."


# --------------------------------------------------------------------------- #
# 防御性解析
# --------------------------------------------------------------------------- #


def test_parse_tolerates_fences_and_surrounding_prose():
    assert sv.parse_json_object(fenced(DECISION))["terms"][0]["term"] == "attention"


def test_parse_skips_braces_that_are_not_the_object():
    text = "视图里有 \\LaTeX{} 这种花括号。\n{\"sections\": [], \"terms\": []}\n"

    assert sv.parse_json_object(text) == {"sections": [], "terms": []}


def test_parse_rejects_truncation():
    with pytest.raises(sv.SurveyParseError, match="截断"):
        sv.parse_json_object('{"sections": [{"title": "Intro", "summ')


def test_parse_rejects_objects_without_any_known_field():
    with pytest.raises(sv.SurveyParseError):
        sv.parse_json_object("\\LaTeX{} 与 {} 都不是决策对象")
    with pytest.raises(sv.SurveyParseError, match="没有返回任何内容"):
        sv.parse_json_object("")


# --------------------------------------------------------------------------- #
# 三条路径：成功 / 重试后成功 / 降级骨架
# --------------------------------------------------------------------------- #


def test_survey_success_path():
    result = masked()

    outcome = sv.survey(result.masked, result, complete=agent_of(fenced(DECISION)), model="fake-1")

    assert outcome.status == sv.OK and not outcome.degraded
    assert outcome.attempts == 1
    brief = outcome.brief
    assert brief["abstract"] == "We study placeholders and the discontents thereof."
    assert brief["paper"]["title"] == "A Study of Placeholders"
    assert brief["paper"]["primary_category"] == "cs.CL"
    assert [s["title"] for s in brief["sections"]] == ["Introduction", "Method"]
    assert brief["sections"][0]["children"][0]["summary"] == "背景。"
    assert brief["notation"][0]["symbol"] == "\\mathcal{L}"
    assert brief["style"]["tone"] == "严谨学术"
    assert "bogus_field" not in brief, "模型多写的字段要在组装时丢掉（schema 不认）"
    assert brief["generated_by"]["model_id"] == "fake-1"
    assert schema_check(brief, "brief") == []
    assert schema_check(outcome.glossary.to_json(), "glossary") == []
    assert outcome.terms_added == 2 and outcome.do_not_translate_added == 1
    assert outcome.glossary.term("beam search").source == "agent"


def test_survey_retries_once_and_feeds_the_error_back():
    result = masked()
    complete = agent_of("我先解释一下我的思路，然后……（没有 JSON）", fenced(DECISION))

    outcome = sv.survey(result.masked, result, complete=complete)

    assert outcome.status == sv.OK
    assert outcome.attempts == 2
    assert len(complete.calls) == 2
    assert "上一次的输出没能解析成 JSON" in complete.calls[1], "错误要喂回去"
    assert complete.calls[0] in complete.calls[1], "重试的提示词仍以规则本体开头"
    assert outcome.warnings, "重试过就该留痕"


def test_survey_degrades_to_a_deterministic_skeleton():
    result = masked()
    complete = agent_of("完全不是 JSON")

    outcome = sv.survey(result.masked, result, complete=complete, model="fake-1")

    assert outcome.status == sv.DEGRADED and outcome.degraded
    assert outcome.ok, "survey 失败不阻塞流水线——brief 是增益不是门禁"
    assert outcome.attempts == 2, "至多重试一次（全文 token 很贵）"
    assert outcome.brief["abstract"], "摘要是程序照录的，降级也不该丢"
    assert [s["title"] for s in outcome.brief["sections"]] == [
        "Introduction",
        "Method",
        "Extra Derivations",
    ]
    assert outcome.brief["sections"][0]["children"][0]["title"] == "Background"
    assert outcome.brief["sections"][2]["is_appendix"] is True
    assert all(s["summary"] == "" for s in outcome.brief["sections"])
    assert "notation" not in outcome.brief
    assert outcome.terms_added == 0 and outcome.glossary.terms == ()
    assert schema_check(outcome.brief, "brief") == []
    assert outcome.message and outcome.warnings


def test_truncated_json_twice_degrades():
    result = masked()

    outcome = sv.survey(result.masked, result, complete=agent_of('{"sections": [{"title": "In'))

    assert outcome.status == sv.DEGRADED
    assert any("截断" in w for w in outcome.warnings)


def test_a_missing_agent_degrades_without_raising():
    result = masked()

    outcome = sv.survey(result.masked, result, complete=None)

    assert outcome.status == sv.DEGRADED and outcome.attempts == 0
    assert schema_check(outcome.brief, "brief") == []


def test_an_exploding_agent_is_retried_then_degrades():
    result = masked()

    def boom(prompt, text, model=None):
        raise RuntimeError("运行时挂了")

    outcome = sv.survey(result.masked, result, complete=boom)

    assert outcome.status == sv.DEGRADED
    assert any("运行时挂了" in w for w in outcome.warnings)


def test_mock_agent_identity_lands_on_the_skeleton_path():
    """e2e 用的就是它：恒等返回不是 JSON → 解析失败 → 确定性骨架。"""
    from tongtu.agent.mock import MockAgent

    result = masked()

    outcome = sv.survey(result.masked, result, complete=MockAgent().complete)

    assert outcome.status == sv.DEGRADED
    assert outcome.brief["sections"], "骨架仍带真实章节树"


# --------------------------------------------------------------------------- #
# 决策表与输入表分离
# --------------------------------------------------------------------------- #


def test_user_entries_beat_agent_decisions_end_to_end():
    result = masked()
    user = gl.Glossary(
        terms=(gl.Term(term="attention", translation="注意力机制", source="paper"),),
        style=gl.Style(style_version="9"),
    )

    outcome = sv.survey(
        result.masked, result, complete=agent_of(fenced(DECISION)), glossary=user
    )

    assert outcome.glossary.term("attention").translation == "注意力机制"
    assert outcome.glossary.term("attention").source == "paper"
    assert outcome.glossary.term("beam search").source == "agent"
    assert outcome.glossary.style_version == "9", "文风版本号来自用户表，agent 碰不得"


# --------------------------------------------------------------------------- #
# brief 的两个下游用途
# --------------------------------------------------------------------------- #


def test_brief_hash_ignores_the_generation_timestamp():
    """否则 survey 一重跑就全量重翻（架构 §4：触发条件是 brief **内容**变化）。"""
    result = masked()
    a = sv.survey(result.masked, result, complete=agent_of(fenced(DECISION)))
    b = sv.survey(result.masked, result, complete=agent_of(fenced(DECISION)))

    assert a.brief["generated_by"]["generated_at"] <= b.brief["generated_by"]["generated_at"]
    assert a.brief_hash == b.brief_hash
    assert a.brief_hash != sv.brief_hash({**a.brief, "abstract": "改了摘要"})


def test_render_brief_is_what_translate_injects():
    result = masked()
    outcome = sv.survey(result.masked, result, complete=agent_of(fenced(DECISION)))

    text = outcome.brief_text

    assert "A Study of Placeholders" in text
    assert "介绍占位符问题。" in text and "背景。" in text
    assert "\\mathcal{L} = 损失函数" in text
    assert "严谨学术" in text


# --------------------------------------------------------------------------- #
# 接线：阶段 manifest、缓存跳过、下游参数
# --------------------------------------------------------------------------- #


def seeded(tmp_path, *, agent=None, glossary=()):
    """把 mask 的产物直接摆进工作目录，跳过 fetch/flatten/baseline（它们要 TeX 工具）。"""
    workdir = Workdir(path=tmp_path / "work", arxiv_id="2401.00001").create()
    result = masked()
    (workdir.build / "flat.tex").write_text(SOURCE, encoding="utf-8")
    (workdir.build / "masked.tex").write_text(result.masked, encoding="utf-8")
    (workdir.build / "blocks.json").write_text(
        json.dumps(result.to_blocks_json(roundtrip_ok=True), ensure_ascii=False), encoding="utf-8"
    )
    pipeline = Pipeline(
        workdir,
        events=Events(io.StringIO(), json_mode=True, arxiv_id="2401.00001"),
        agent=agent,
        glossary=glossary,
    )
    assert pipeline.run_one("mask", mode="load").status == "cached"
    return pipeline


class _JsonAgent:
    """按次返回预置输出的 agent 运行时（`complete` 原语）。"""

    model = "fake-json"

    def __init__(self, *responses: str) -> None:
        self.responses = responses
        self.calls = 0

    def complete(self, prompt: str, text: str, model=None) -> str:
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


def test_survey_stage_writes_both_products_and_a_manifest(tmp_path):
    pipeline = seeded(tmp_path, agent=_JsonAgent(fenced(DECISION)))

    outcome = pipeline.run_one("survey")

    assert outcome.status == "ok"
    assert outcome.detail["degraded"] is False and outcome.detail["terms_added"] == 2
    brief = json.loads(pipeline.brief_path.read_text(encoding="utf-8"))
    decided = json.loads(pipeline.glossary_path.read_text(encoding="utf-8"))
    assert schema_check(brief, "brief") == [] and schema_check(decided, "glossary") == []
    assert brief["paper"]["arxiv_id"] == "2401.00001"

    manifest = read_manifest(pipeline.workdir, "survey")
    assert manifest["inputs"]["masked"] and manifest["inputs"]["blocks"]
    assert manifest["inputs"]["prompt_version"] == sv.prompt_version()
    assert manifest["inputs"]["prompt"], "prompt 资产的内容 hash 也要进 manifest"
    assert {e["path"] for e in manifest["outputs"]} == {"build/brief.json", "build/glossary.json"}


def test_survey_is_skipped_when_nothing_changed(tmp_path):
    agent = _JsonAgent(fenced(DECISION))
    pipeline = seeded(tmp_path, agent=agent)
    assert pipeline.run_one("survey").status == "ok"
    assert agent.calls == 1

    again = seeded(tmp_path, agent=agent)
    outcome = again.run_one("survey")

    assert outcome.status == "cached"
    assert agent.calls == 1, "缓存命中不该再花一次全文 token"
    assert again.brief["abstract"] and again.decisions.term("attention")
    # 「算出来的」与「从盘上装回来的」必须给出同样的下游输入，否则 translate 会在
    # 「跑过 survey」与「跳过 survey」之间反复失效——缓存自己把自己搅黄。
    assert sv.brief_hash(again.brief) == sv.brief_hash(pipeline.brief)
    assert gl.content_hash(again.decisions) == gl.content_hash(pipeline.decisions)
    assert again.decisions.style_version == pipeline.decisions.style_version


def test_editing_a_glossary_layer_invalidates_survey(tmp_path):
    table = tmp_path / "cli.json"
    table.write_text(json.dumps({"terms": [{"term": "x", "translation": "叉"}]}), encoding="utf-8")
    pipeline = seeded(tmp_path, agent=_JsonAgent(fenced(DECISION)), glossary=[table])
    assert pipeline.run_one("survey").status == "ok"
    inputs = read_manifest(pipeline.workdir, "survey")["inputs"]

    table.write_text(json.dumps({"terms": [{"term": "x", "translation": "叉子"}]}), encoding="utf-8")
    changed = seeded(tmp_path, agent=_JsonAgent(fenced(DECISION)), glossary=[table])

    assert not manifest_fresh(changed.workdir, "survey", changed._survey_inputs())
    assert changed.run_one("survey").status == "ok"
    assert read_manifest(changed.workdir, "survey")["inputs"] != inputs


def test_a_broken_glossary_fails_the_stage_with_the_path(tmp_path):
    table = tmp_path / "cli.json"
    table.write_text("{ nope", encoding="utf-8")
    pipeline = seeded(tmp_path, agent=_JsonAgent(fenced(DECISION)), glossary=[table])

    outcome = pipeline.run_one("survey")

    assert outcome.status == "failed" and "cli.json" in outcome.error


def test_brief_and_terms_reach_the_translate_stage(tmp_path):
    """survey 的两份产物是 translate 的上下文与缓存 key 的一部分（架构 §3/§4）。"""
    from tongtu.agent.mock import MockAgent

    class _Agent(MockAgent):
        """survey 返回决策 JSON，translate 恒等返回——一个 agent 兼顾两个关节。"""

        def complete(self, prompt: str, text: str, model=None) -> str:
            self.completions.append((prompt, text))
            return fenced(DECISION) if "通读" in prompt else text

    agent = _Agent()
    pipeline = seeded(tmp_path, agent=agent)
    assert pipeline.run_one("survey").status == "ok"
    assert pipeline.run_one("chunk").status == "ok"

    before = pipeline._translate_inputs()
    assert pipeline.run_one("translate").status == "ok"

    prompts_seen = [p for p, _ in agent.completions[1:]]
    assert any("A Study of Placeholders" in p for p in prompts_seen), "brief 要进逐块提示词"
    assert any("attention → 注意力" in p for p in prompts_seen), "命中术语要进逐块提示词"
    memory = json.loads(
        (pipeline.zh_chunks_dir / "chunks.json").read_text(encoding="utf-8")
    )
    assert memory["brief_hash"] == sv.brief_hash(pipeline.brief)
    assert memory["style_version"] == pipeline.decisions.style_version
    hit = [c for c in memory["chunks"] if c.get("terms")]
    assert hit and {t["term"] for c in hit for t in c["terms"]} <= {
        "attention",
        "beam search",
        "beam-search",
        "LLaMA",
    }

    assert before["brief"] == sv.brief_hash(pipeline.brief)
    assert before["glossary"] == gl.content_hash(pipeline.decisions)
