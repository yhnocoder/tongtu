"""翻译记忆（块级缓存）的存取与失效（架构 §4 的返工触发表、决策 3）。

两层测试，对应两个不同的正确性判据：

1. **存取层**（`tongtu.memory`）：从 `chunks.json` 装载出来的到底是什么、坏文件怎么办、
   `out/` 与 `build/` 谁覆盖谁、按块 id / 术语选出来的 key 对不对；
2. **命中语义层**（`tongtu.stages.translate` + 缓存）：架构 §4 那张返工触发表的三行——
   改一个术语只失效**命中它的块**、bump `style_version` 全失效、换模型全失效。判据是
   **关节⑤被调了几次**，不是「跑通了」：缓存的全部价值就是少调几次模型。
"""

from __future__ import annotations

import json

from tongtu import memory as mem
from tongtu.stages import translate as tr
from tongtu.stages.chunk import chunk_masked
from tongtu.workdir import Workdir

# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

#: 两块：第一块含 tensor，第二块不含——「改一个词只失效命中它的块」正是靠这个可测。
MASKED = """\
⟦BLK-0⟧
\\section{First}

An opening paragraph about tensor decompositions and other matters.

\\section{Second}

A closing paragraph that mentions nothing in particular.
"""


def plan():
    plan = chunk_masked(MASKED, soft_target=20, hard_limit=60)
    assert len(plan) == 2, "夹具指望恰好切出两块（一块命中术语、一块不命中）"
    return plan


def counting_agent():
    """`complete` 恒等返回，并把每次收到的正文记下来（调用次数即「花了多少钱」）。"""
    seen: list[str] = []

    def complete(prompt: str, text: str, model=None) -> str:
        seen.append(text)
        return text

    complete.seen = seen  # type: ignore[attr-defined]
    return complete


def chunks_json(*entries: dict) -> dict:
    return {"contract_version": "0.1", "chunks": list(entries)}


def entry(**fields) -> dict:
    base = {
        "id": "c000",
        "src": "some source",
        "src_hash": "0" * 64,
        "cache_key": "a" * 64,
        "translation": "\n译文\n",
        "status": "translated",
    }
    return {**base, **fields}


def write(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 存取
# --------------------------------------------------------------------------- #


def test_load_file_strips_the_affixes(tmp_path):
    """记忆里存的是**正文**：首尾空白由 translate 的驱动器保管，不进缓存。"""
    path = tmp_path / "chunks.json"
    write(path, chunks_json(entry(translation="\n\n  译文  \n\n")))

    assert mem.load_file(path) == {"a" * 64: "译文"}


def test_fallback_entries_never_enter_the_memory(tmp_path):
    """回退块存的是原文，命中它等于把一次失败冻结成永久结论。"""
    path = tmp_path / "chunks.json"
    write(
        path,
        chunks_json(
            entry(id="c000", cache_key="a" * 64),
            entry(id="c001", cache_key="b" * 64, status="fallback"),
            entry(id="c002", cache_key="", translation="没有 key 的条目"),
        ),
    )

    assert set(mem.load_file(path)) == {"a" * 64}


def test_a_broken_memory_file_is_not_an_error(tmp_path):
    """记忆坏了最坏只是全部重翻一次（贵，但不损坏）——不许拖垮流水线。"""
    path = tmp_path / "chunks.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    assert mem.read_chunks(path) is None
    assert mem.load_file(path) == {}
    assert mem.load_file(tmp_path / "根本不存在.json") == {}
    assert mem.entries(None) == () and mem.entries({"chunks": "不是数组"}) == ()


def test_load_prefers_the_build_copy(tmp_path):
    """两份都在时以 build 侧为准（它是本轮刚写的），来源记进 `sources` 供人核对。"""
    workdir = Workdir(path=tmp_path / "work", arxiv_id="2401.00001").create()
    out_path, build_path = mem.memory_paths(workdir)
    write(out_path, chunks_json(entry(cache_key="a" * 64, translation="旧译文")))
    write(
        build_path,
        chunks_json(
            entry(cache_key="a" * 64, translation="新译文"),
            entry(id="c001", cache_key="b" * 64, translation="另一块"),
        ),
    )

    memory = mem.load(workdir)

    assert memory["a" * 64] == "新译文" and len(memory) == 2
    assert [p.split("/")[-2] for p in memory.sources] == ["out", "zh-chunks"]


def test_load_recovers_from_out_when_build_is_gone(tmp_path):
    """`build/` 整体删掉不丢昂贵成果（架构 §5/§4 末）——权威记忆随产物包走。"""
    workdir = Workdir(path=tmp_path / "work", arxiv_id="2401.00001").create()
    out_path, _ = mem.memory_paths(workdir)
    write(out_path, chunks_json(entry(cache_key="a" * 64, translation="译文")))

    assert mem.load(workdir)["a" * 64] == "译文"


def test_forget_and_drop_entries(tmp_path):
    """失效 = 删条目：内存里删掉，盘上的权威记忆也要删（否则下一次 run 会把它装回来）。"""
    path = tmp_path / "chunks.json"
    write(
        path,
        chunks_json(entry(id="c000", cache_key="a" * 64), entry(id="c001", cache_key="b" * 64)),
    )
    memory = mem.Memory(entries=dict(mem.load_file(path)))

    assert memory.forget(["a" * 64, "不存在的 key"]) == 1
    assert set(memory) == {"b" * 64}
    assert mem.drop_entries(path, ["a" * 64]) == 1
    assert mem.chunk_ids(mem.read_chunks(path)) == ("c001",)
    assert mem.drop_entries(path, []) == 0


# --------------------------------------------------------------------------- #
# 失效范围的选择
# --------------------------------------------------------------------------- #


def test_keys_for_chunks_reports_unknown_ids():
    record = chunks_json(entry(id="c000", cache_key="a" * 64), entry(id="c001", cache_key="b" * 64))

    keys, missing = mem.keys_for_chunks(record, ["c001", "c099"])

    assert keys == {"b" * 64} and missing == ("c099",)
    assert mem.chunk_ids(record) == ("c000", "c001")


def test_keys_for_term_uses_the_same_hit_rule_as_the_cache_key():
    """命中判定与 cache key 用同一份实现（`glossary.hit_terms`）：大小写不敏感的子串。"""
    record = chunks_json(
        entry(id="c000", cache_key="a" * 64, src="A study of Tensor decompositions."),
        entry(id="c001", cache_key="b" * 64, src="Nothing to see here."),
        entry(
            id="c002",
            cache_key="c" * 64,
            src="源码里没有这个词形（靠 aliases 命中的）",
            terms=[{"term": "tensor", "translation": "张量"}],
        ),
    )

    assert mem.keys_for_term(record, "tensor") == {"a" * 64, "c" * 64}
    assert mem.keys_for_term(record, "TENSOR") == {"a" * 64, "c" * 64}
    assert mem.keys_for_term(record, "没人提过的词") == set()
    assert mem.keys_for_term(record, "   ") == set()


# --------------------------------------------------------------------------- #
# 命中语义（架构 §4 返工触发表）
# --------------------------------------------------------------------------- #


def test_editing_a_term_only_invalidates_the_chunks_that_hit_it():
    """改一个术语条目 → 只有**命中它的块**失效（架构 §4 表第 3 行、决策 5）。"""
    p = plan()
    cache = mem.Memory()
    agent = counting_agent()

    tr.translate(p, complete=agent, cache=cache)
    assert len(agent.seen) == 2 and len(cache) == 2, "首跑全 miss"

    result = tr.translate(p, complete=agent, glossary={"tensor": "张量"}, cache=cache)

    assert len(agent.seen) == 3, "只该重翻命中 tensor 的那一块"
    assert "tensor" in agent.seen[-1]
    assert [c.cached for c in result.chunks] == [False, True]
    assert result.cache_hits == 1 and result.cache_misses == 1
    assert len(cache) == 3, "新 key 是新增而非替换——改回去还能命中老译文"


def test_bumping_the_style_version_invalidates_everything():
    """改文风规则 = 显式的全量重翻（架构 §4 表第 4 行）。"""
    p = plan()
    cache = mem.Memory()
    agent = counting_agent()
    tr.translate(p, complete=agent, cache=cache)

    result = tr.translate(p, complete=agent, style_version="下一版", cache=cache)

    assert len(agent.seen) == 4 and result.cache_hits == 0
    assert result.cache_misses == len(p)


def test_changing_the_brief_invalidates_everything():
    """重跑 survey 且 brief **内容**变了 → 全部块（架构 §4 表第 5 行）。"""
    p = plan()
    cache = mem.Memory()
    agent = counting_agent()
    tr.translate(p, complete=agent, brief_hash="老纲要", cache=cache)

    result = tr.translate(p, complete=agent, brief_hash="新纲要", cache=cache)

    assert len(agent.seen) == 4 and result.cache_hits == 0


def test_a_second_identical_run_costs_nothing():
    p = plan()
    cache = mem.Memory()
    agent = counting_agent()
    tr.translate(p, complete=agent, glossary={"tensor": "张量"}, cache=cache)

    result = tr.translate(p, complete=agent, glossary={"tensor": "张量"}, cache=cache)

    assert len(agent.seen) == 2, "输入一个字没变，一次也不该拉起关节⑤"
    assert result.cache_hits == len(p) and result.stream == MASKED


def test_memory_roundtrips_through_chunks_json(tmp_path):
    """翻一遍 → 落盘 → 装回来 → 再翻一遍：全命中。这是「续跑」这条路径的最小闭环。"""
    p = plan()
    agent = counting_agent()
    first = tr.translate(p, complete=agent, model="m", cache=mem.Memory())
    path = tmp_path / "out" / "chunks.json"
    mem.write_chunks(path, first.to_chunks_json())

    restored = mem.Memory(entries=dict(mem.load_file(path)))
    second = tr.translate(p, complete=agent, model="m", cache=restored)

    assert len(agent.seen) == 2, "从盘上装回来的记忆必须能命中"
    assert second.cache_hits == len(p)
    assert second.stream == first.stream == MASKED, "命中之后拼接仍逐字节等于掩码流"
