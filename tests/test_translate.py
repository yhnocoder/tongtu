"""translate 阶段驱动器与 MockAgent（架构 §3 translate 行、关节⑤、§9 末）。

e2e（`tests/test_e2e_identity.py`）只走恒等译文这条happy path——内环重试、回退、缓存
命中这些**只有模型不听话时才发生**的路径在那里永远盖不到。本文件用可编程的假 agent
把它们逐条固定下来：翻译驱动器的正确性判据是「validate 说了算、失败可控地降级」，而不是
「跑通了」。
"""

from __future__ import annotations

from tongtu import validate
from tongtu.agent import SessionOutcome
from tongtu.agent.mock import PSEUDO_PREFIX, MockAgent, PseudoAgent, pseudo_translate
from tongtu.stages.chunk import chunk_masked
from tongtu.stages import translate as tr

MASKED = """\
⟦BLK-0⟧
\\section{First}
\\label{sec:one}

An opening paragraph about ⟦BLK-1⟧ and other placeholder matters.

A second paragraph with inline math $x + y$ and a \\cite{ref} call.

\\section{Second}

The final paragraph of the fixture stream.
"""


def plan():
    return chunk_masked(MASKED, soft_target=40, hard_limit=80)


def agent_that(func):
    """把 `body -> 译文` 的函数包成 `complete(prompt, text, model)` 原语。"""
    calls: list[str] = []

    def complete(prompt: str, text: str, model=None) -> str:
        calls.append(prompt)
        return func(text, len(calls))

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def progress_log():
    events: list[tr.Progress] = []
    return events, events.append


# --------------------------------------------------------------------------- #
# MockAgent
# --------------------------------------------------------------------------- #


def test_mock_complete_is_identity():
    agent = MockAgent()

    assert agent.complete("任意提示词", "\\section{Hi} ⟦BLK-1⟧") == "\\section{Hi} ⟦BLK-1⟧"
    assert agent.completions[0].prompt == "任意提示词"
    assert agent.model == "mock"


def test_mock_transform_enables_a_pseudo_translation_variant():
    """伪翻译（pseudo-translation）变体（架构附录 B 开放问题 2）：

    换个 transform 即可，零 LLM、零随机。
    """
    agent = MockAgent(transform=lambda text: "【译】" + text)
    assert agent.complete("p", "abc") == "【译】abc"


# --------------------------------------------------------------------------- #
# PseudoAgent（中文注入变体：中文路径的覆盖点，见 tests/test_e2e_pseudo.py）
# --------------------------------------------------------------------------- #


def test_pseudo_translation_passes_all_four_validate_layers():
    """前缀句不含 `\\` `{` `}` `$` `⟦` `⟧`，故四层校验逐项不变——变体的立身之本。"""
    translated = pseudo_translate(MASKED)

    assert validate.check(MASKED, translated) == [], "中文注入必须自带 validate 全绿"
    assert PSEUDO_PREFIX in translated
    assert translated.replace(PSEUDO_PREFIX, "") == MASKED, "删掉前缀句即逐字节回到原文"


def test_pseudo_translation_skips_structural_paragraphs():
    """结构行开头的段一律不加前缀：中文落在 `\\documentclass` 前或首个 `\\item` 前是真编译错。"""
    stream = "⟦BLK-0⟧\n\\section{First}\n\n\\begin{itemize}\n\\item one\n\\end{itemize}\n\nProse.\n"

    translated = pseudo_translate(stream)

    paragraphs = translated.split("\n\n")
    assert paragraphs[0] == "⟦BLK-0⟧\n\\section{First}", "前导区块那一段一个字也不许动"
    assert paragraphs[1].startswith("\\begin{itemize}")
    assert paragraphs[2] == PSEUDO_PREFIX + "Prose.\n"


def test_pseudo_translation_is_deterministic_and_leaves_blank_text_alone():
    assert pseudo_translate("") == ""
    assert pseudo_translate("\n\n  \n") == "\n\n  \n"
    assert pseudo_translate("Hi") == pseudo_translate("Hi") == PSEUDO_PREFIX + "Hi"
    assert validate.paragraph_count(pseudo_translate(MASKED)) == validate.paragraph_count(MASKED)


def test_pseudo_agent_is_a_mock_with_a_transform_and_its_own_model():
    agent = PseudoAgent()

    assert isinstance(agent, MockAgent)
    assert agent.model == "pseudo" != MockAgent().model, "两个变体的翻译记忆必须彼此独立"
    assert agent.complete("任意提示词", "Prose.") == PSEUDO_PREFIX + "Prose."
    assert agent.session("修一下").done is True, "session 仍是 no-op：变体只改译文"


def test_mock_session_is_a_noop_and_records(tmp_path):
    agent = MockAgent(transcript_dir=tmp_path / "logs")

    outcome = agent.session("修一下编译错", workdir=tmp_path, model="m", budget=3)

    assert isinstance(outcome, SessionOutcome) and outcome.done is True
    assert outcome.transcript_path is not None and outcome.transcript_path.is_file()
    assert agent.sessions[0].budget == 3
    assert list(tmp_path.iterdir()) == [tmp_path / "logs"], "no-op 会话不许改工作目录"


def test_mock_session_fn_adapts_the_fixup_request(tmp_path):
    """`as_session_fn()` 把编译回环的 `FixupRequest` 拆回两原语的参数。"""
    from tongtu.compiler import FixupRequest
    from tongtu.workdir import Workdir

    agent = MockAgent()
    workdir = Workdir(path=tmp_path / "w", arxiv_id="x")
    request = FixupRequest(
        joint="fixup",
        prompt="编不过，修",
        workdir=workdir,
        build_dir=tmp_path / "w" / "build",
        tex=tmp_path / "w" / "build" / "zh.tex",
    )

    outcome = agent.as_session_fn()(request)

    assert outcome.done is True
    assert agent.sessions[0].prompt == "编不过，修"
    assert agent.sessions[0].workdir == str(workdir.path)


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def test_split_affixes_roundtrips():
    for text in ("\n\nbody\n\n", "body", "\n", "", "  x  "):
        lead, body, trail = tr.split_affixes(text)
        assert lead + body + trail == text
        assert body == body.strip()


def test_cache_key_ignores_whitespace_but_not_the_rest():
    base = tr.cache_key("a  b\n\nc")

    assert base == tr.cache_key(" a b c ")
    assert base != tr.cache_key("a b d")
    assert base != tr.cache_key("a  b\n\nc", model="gpt-x")
    assert base != tr.cache_key("a  b\n\nc", terms=[("tensor", "张量")])
    assert base != tr.cache_key("a  b\n\nc", brief_hash="deadbeef")
    assert base != tr.cache_key("a  b\n\nc", style_version="v2")
    assert len(base) == 64


def test_context_carries_neighbour_source_only():
    """邻域上下文只用**原文**（架构 §3 末：传前块译文会让缓存失效沿块链级联）。"""
    p = plan()
    assert len(p) > 1
    first = tr.assemble_context(p.chunks[0], p, glossary={"placeholder": "占位符"})
    second = tr.assemble_context(p.chunks[1], p, glossary={"placeholder": "占位符"})

    assert first.before == "" and first.after and first.after in MASKED
    assert second.before and second.before in MASKED and second.after == ""
    # 术语按**块内命中**参与快照与 cache key：第一块出现 placeholder，第二块没有
    assert first.terms == (("placeholder", "占位符"),)
    assert second.terms == ()
    assert "上文原文" in tr.build_prompt(second)
    assert first.neighbor_hash != second.neighbor_hash


def test_prompt_feeds_validation_errors_back():
    from tongtu.validate import check

    errors = check("one ⟦BLK-1⟧", "一")
    prompt = tr.build_prompt(tr.Context(), errors)

    assert "没通过机械校验" in prompt
    assert "占位符" in prompt


# --------------------------------------------------------------------------- #
# 块循环
# --------------------------------------------------------------------------- #


def test_identity_translation_is_all_green():
    p = plan()
    events, sink = progress_log()

    result = translate = tr.translate(
        p, complete=MockAgent().complete, model="mock", progress=sink
    )

    assert result.status == tr.OK and result.ok
    assert [c.status for c in result.chunks] == [tr.TRANSLATED] * len(p)
    assert all(c.attempts == 1 for c in result.chunks)
    assert result.stream == MASKED, "恒等译块拼接必须逐字节等于掩码流"
    assert "".join(u.translation for u in translate.units) == MASKED
    assert {e.status for e in events} == {"started", "translated"}
    assert result.failures_by_check == {}


def test_block_affixes_survive_a_sloppy_agent():
    """块首尾的空白由驱动器保管——模型怎么折腾正文都不该动块的形状。"""
    p = plan()
    result = tr.translate(p, complete=agent_that(lambda body, n: body))

    assert result.stream == MASKED
    for chunk, item in zip(p.chunks, result.chunks):
        assert item.source == chunk.text


def test_validate_failure_retries_then_succeeds():
    def flaky(body: str, attempt: int) -> str:
        return body if attempt % 2 == 0 else body + "\n\n多出来的一段。"

    events, sink = progress_log()
    result = tr.translate(plan(), complete=agent_that(flaky), progress=sink)

    assert result.status == tr.OK
    assert all(c.attempts == 2 for c in result.chunks)
    retries = [e for e in events if e.status == "retry"]
    assert retries and retries[0].attempt == 2
    assert "段落数" in (retries[0].reason or "")


def test_retries_are_capped_then_the_chunk_falls_back():
    events, sink = progress_log()
    result = tr.translate(
        plan(),
        complete=agent_that(lambda body, n: "彻底不守规矩的译文"),
        max_retries=2,
        progress=sink,
    )

    assert result.status == tr.OK_WITH_FALLBACK and result.ok
    assert [c.status for c in result.chunks] == [tr.FALLBACK] * len(result.chunks)
    assert all(c.attempts == 3 for c in result.chunks), "1 次首翻 + 2 次重试"
    assert result.stream == MASKED, "回退原文后拼接仍等于掩码流"
    assert result.failures_by_check, "回退要留下失败统计（进 report）"
    assert all(c.fallback_reason == tr.REASON_VALIDATE for c in result.fallbacks)
    assert {e.status for e in events} >= {"retry", "fallback"}


def test_zero_retries_means_one_shot():
    result = tr.translate(
        plan(), complete=agent_that(lambda body, n: "不合规"), max_retries=0
    )
    assert all(c.attempts == 1 for c in result.chunks)
    assert result.status == tr.OK_WITH_FALLBACK


def test_agent_exception_is_retried_then_recorded():
    def boom(body: str, attempt: int) -> str:
        raise RuntimeError("运行时挂了")

    result = tr.translate(plan(), complete=agent_that(boom), max_retries=1)

    assert result.status == tr.OK_WITH_FALLBACK
    assert all(c.fallback_reason == tr.REASON_AGENT for c in result.fallbacks)
    assert result.stream == MASKED


def test_empty_translation_counts_as_a_failure():
    result = tr.translate(plan(), complete=agent_that(lambda body, n: "   "), max_retries=0)
    assert all(c.status == tr.FALLBACK for c in result.chunks)


def test_cache_hit_skips_the_agent():
    p = plan()
    cache: dict[str, str] = {}

    first = tr.translate(p, complete=MockAgent().complete, cache=cache)
    assert len(cache) == len(p), "翻译成功的块要进缓存"

    def forbidden(prompt, text, model=None):  # pragma: no cover - 命中缓存就不该被调用
        raise AssertionError("缓存命中时不该拉起关节⑤")

    events, sink = progress_log()
    second = tr.translate(p, complete=forbidden, cache=cache, progress=sink)

    assert second.stream == first.stream == MASKED
    assert second.cache_hits == len(p)
    assert {e.status for e in events} == {"cached"}
    assert {c["status"] for c in second.to_chunks_json()["chunks"]} == {"translated"}


def test_cache_is_keyed_on_the_model():
    p = plan()
    cache: dict[str, str] = {}
    tr.translate(p, complete=MockAgent().complete, model="a", cache=cache)
    tr.translate(p, complete=MockAgent().complete, model="b", cache=cache)
    assert len(cache) == 2 * len(p), "换模型必须整体失效（架构 §4）"


def test_empty_plan_is_a_structured_failure():
    result = tr.translate([], complete=MockAgent().complete)
    assert result.status == tr.FAILED and not result.ok and result.message


def test_chunks_json_shape():
    p = plan()
    result = tr.translate(p, complete=MockAgent().complete, model="mock")
    data = result.to_chunks_json()

    assert data["contract_version"] and data["model_id"] == "mock"
    entry = data["chunks"][0]
    assert entry["id"] == p.chunks[0].id
    assert len(entry["src_hash"]) == len(entry["cache_key"]) == 64
    assert entry["status"] == "translated"
    assert entry["paragraph_count"] == p.chunks[0].paragraph_count
