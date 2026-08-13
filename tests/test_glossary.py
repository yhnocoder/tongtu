"""术语表三层合并与块内命中（架构 §8、§4，决策 5）。

这层测试盯的是三件事，它们各自都能悄无声息地毁掉译文一致性：

1. **合并语义**——谁覆盖谁。三层输入表（全局 XDG → 论文目录 → `--glossary`）后者覆盖
   前者，`do_not_translate` 取并集，`style` 逐字段覆盖；agent 决策只能新增，用户条目优先。
2. **XDG 尊重**——全局层的位置由 `$XDG_CONFIG_HOME` 决定，不是硬编码的 `~/.config`。
3. **命中与排序**——`relevant_terms` 直接进块级缓存 key（架构 §4），命中集合不稳定就等于
   缓存失效随机化，故排序必须是确定的，且必须与 translate 用的那份实现**逐字节一致**。
"""

from __future__ import annotations

import json

import pytest

from tongtu import glossary as gl
from tongtu.schema_check import check as schema_check
from tongtu.stages import translate as tr
from tongtu.stages.chunk import chunk_masked


def write(path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def table(terms=(), do_not_translate=(), style=None) -> dict:
    data: dict = {"contract_version": "0.1"}
    if terms:
        data["terms"] = list(terms)
    if do_not_translate:
        data["do_not_translate"] = list(do_not_translate)
    if style is not None:
        data["style"] = style
    return data


# --------------------------------------------------------------------------- #
# 层的定位
# --------------------------------------------------------------------------- #


def test_global_layer_respects_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    assert gl.global_path() == tmp_path / "cfg" / "tongtu" / "glossary.json"


def test_global_layer_falls_back_to_dot_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert gl.global_path().parts[-3:] == (".config", "tongtu", "glossary.json")


def test_paper_layer_sits_next_to_the_four_areas(tmp_path):
    """论文层刻意不在 `src/` 里——那是只读的 e-print 树，混进去会污染 fetch 的树 hash。"""
    from tongtu.workdir import Workdir

    workdir = Workdir(path=tmp_path / "2401.00001", arxiv_id="2401.00001")

    assert gl.paper_path(workdir) == workdir.path / "glossary.json"
    assert gl.paper_path(workdir).parent == workdir.path  # 与 src/ build/ out/ logs/ 同级


def test_layers_are_discovered_in_priority_order(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    write(gl.global_path(), table(terms=[{"term": "model", "translation": "模型"}]))
    workdir = tmp_path / "paper"
    write(workdir / "glossary.json", table(terms=[{"term": "loss", "translation": "损失"}]))
    cli = write(tmp_path / "cli.json", table(terms=[{"term": "batch", "translation": "批次"}]))

    layers = gl.load_layers(workdir=workdir, cli=[cli])

    assert [layer.layer for layer in layers] == ["global", "paper", "cli"]
    assert [layer.glossary.entry_count for layer in layers] == [1, 1, 1]


def test_a_missing_cli_table_is_an_error_but_a_missing_global_one_is_not(tmp_path):
    """`--glossary` 指了不存在的文件是用法错误；用户没写全局表则完全正常。"""
    assert gl.load_layers(workdir=tmp_path / "paper") == ()

    with pytest.raises(gl.GlossaryError, match="不存在"):
        gl.load_layers(cli=[tmp_path / "nope.json"])


def test_a_malformed_table_is_rejected_with_the_path(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")

    with pytest.raises(gl.GlossaryError, match="不是合法 JSON"):
        gl.load_file(bad)

    off_schema = write(tmp_path / "off.json", {"terms": [{"term": "x", "translation": 1}]})
    with pytest.raises(gl.GlossaryError, match="不合 schema"):
        gl.load_file(off_schema)


# --------------------------------------------------------------------------- #
# 合并语义
# --------------------------------------------------------------------------- #


def merged(tmp_path, monkeypatch, *, global_=None, paper=None, cli=()):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    if global_ is not None:
        write(gl.global_path(), global_)
    workdir = tmp_path / "paper"
    if paper is not None:
        write(workdir / "glossary.json", paper)
    paths = []
    for i, payload in enumerate(cli):
        paths.append(write(tmp_path / f"cli{i}.json", payload))
    return gl.load_glossary(workdir=workdir, cli=paths)


def test_later_layers_override_the_same_term(tmp_path, monkeypatch):
    result = merged(
        tmp_path,
        monkeypatch,
        global_=table(terms=[{"term": "attention", "translation": "注意"}]),
        paper=table(terms=[{"term": "attention", "translation": "注意力"}]),
        cli=[table(terms=[{"term": "attention", "translation": "注意力机制"}])],
    )

    assert [(t.term, t.translation) for t in result.terms] == [("attention", "注意力机制")]
    assert result.term("attention").source == "cli"


def test_multiple_cli_tables_are_applied_left_to_right(tmp_path, monkeypatch):
    result = merged(
        tmp_path,
        monkeypatch,
        cli=[
            table(terms=[{"term": "head", "translation": "头部"}]),
            table(terms=[{"term": "head", "translation": "注意力头"}]),
        ],
    )

    assert result.term("head").translation == "注意力头"


def test_do_not_translate_is_a_union(tmp_path, monkeypatch):
    result = merged(
        tmp_path,
        monkeypatch,
        global_=table(do_not_translate=[{"term": "PyTorch"}]),
        paper=table(do_not_translate=[{"term": "LLaMA", "note": "模型名"}]),
        cli=[table(do_not_translate=[{"term": "PyTorch", "note": "库名"}])],
    )

    assert [d.term for d in result.do_not_translate] == ["LLaMA", "PyTorch"]
    assert result.do_not_translate[1].note == "库名"  # 同名后者胜，带上后者的备注


def test_style_is_overridden_field_by_field(tmp_path, monkeypatch):
    """后者只覆盖它写了的字段——不然「改一条规则」会连带清空 tone。"""
    result = merged(
        tmp_path,
        monkeypatch,
        global_=table(style={"style_version": "1", "tone": "学术书面语", "rules": ["规则甲"]}),
        paper=table(style={"style_version": "2", "rules": ["规则乙"]}),
    )

    assert result.style.style_version == "2"
    assert result.style.tone == "学术书面语"
    assert result.style.rules == ("规则乙",)


def test_a_layer_without_a_style_section_does_not_reset_style_version(tmp_path, monkeypatch):
    """「没写 style_version」不等于「写了默认值」——否则加一层术语就把文风版本冲掉了。"""
    result = merged(
        tmp_path,
        monkeypatch,
        global_=table(style={"style_version": "7", "tone": "学术书面语"}),
        paper=table(terms=[{"term": "x", "translation": "叉"}]),
        cli=[table(terms=[{"term": "y", "translation": "歪"}])],
    )

    assert result.style_version == "7"
    assert result.style.tone == "学术书面语"


def test_style_version_falls_back_to_the_prompt_asset_version(tmp_path, monkeypatch):
    """谁都没写 style_version 时，生效的是 prompt 资产自带的文风规则版本。"""
    from tongtu import prompts

    result = merged(tmp_path, monkeypatch, paper=table(terms=[{"term": "x", "translation": "叉"}]))

    assert result.style_version == prompts.STYLE_VERSION == gl.DEFAULT_STYLE_VERSION


def test_merged_from_records_every_layer(tmp_path, monkeypatch):
    result = merged(
        tmp_path,
        monkeypatch,
        global_=table(terms=[{"term": "a", "translation": "甲"}]),
        cli=[table(terms=[{"term": "b", "translation": "乙"}])],
    )

    assert [layer.layer for layer in result.merged_from] == ["global", "cli"]
    assert schema_check(result.to_json(), "glossary") == []


# --------------------------------------------------------------------------- #
# 用户条目优先于 agent 决策
# --------------------------------------------------------------------------- #


def test_agent_may_add_but_never_overwrite(tmp_path, monkeypatch):
    base = merged(
        tmp_path,
        monkeypatch,
        paper=table(
            terms=[{"term": "attention", "translation": "注意力"}],
            do_not_translate=[{"term": "LLaMA"}],
        ),
    )

    decided = gl.with_agent_decisions(
        base,
        terms=[
            {"term": "attention", "translation": "关注"},  # 用户已写死 → 丢弃
            {"term": "beam search", "translation": "束搜索", "aliases": ["beam-search"]},
        ],
        do_not_translate=[{"term": "LLaMA"}, {"term": "BLEU"}],
    )

    assert decided.term("attention").translation == "注意力"
    assert decided.term("attention").source == "paper"
    assert decided.term("beam search").source == "agent"
    assert decided.term("beam search").decided_at  # agent 决策带时间戳
    assert [d.term for d in decided.do_not_translate] == ["BLEU", "LLaMA"]
    assert decided.do_not_translate[0].source == "agent"
    assert schema_check(decided.to_json(), "glossary") == []


def test_agent_junk_is_dropped_not_crashed(tmp_path, monkeypatch):
    base = gl.empty()

    decided = gl.with_agent_decisions(
        base,
        terms=[{"term": "", "translation": "空"}, {"term": "ok"}, "不是对象", {"nope": 1}],
        do_not_translate=["裸字符串也收", {"term": ""}],
    )

    assert decided.terms == ()
    assert [d.term for d in decided.do_not_translate] == ["裸字符串也收"]


# --------------------------------------------------------------------------- #
# 命中、排序与 hash
# --------------------------------------------------------------------------- #


GLOSSARY = gl.Glossary(
    terms=(
        gl.Term(term="attention", translation="注意力", aliases=("attentions",)),
        gl.Term(term="beam search", translation="束搜索"),
        gl.Term(term="zebra", translation="斑马"),
    ),
    do_not_translate=(gl.NoTranslate(term="LLaMA"),),
)

CHUNK = "We apply Attention over the beam search output of LLaMA, twice."


def test_relevant_terms_returns_the_hit_subset_sorted():
    hits = gl.relevant_terms(CHUNK, GLOSSARY)

    assert hits == (
        ("LLaMA", "LLaMA"),  # 不译 = 译法即原词
        ("attention", "注意力"),
        ("beam search", "束搜索"),
    )
    assert hits == tuple(sorted(hits)), "命中集合进缓存 key，排序必须确定"
    assert "zebra" not in dict(hits), "没出现的词不该进上下文"


def test_aliases_are_hit_too():
    assert ("attentions", "注意力") in gl.relevant_terms("many attentions here", GLOSSARY)


def test_hits_are_case_insensitive_and_stable_across_calls():
    once = gl.relevant_terms(CHUNK.upper(), GLOSSARY)

    assert once == gl.relevant_terms(CHUNK.upper(), GLOSSARY)
    assert dict(once)["attention"] == "注意力"


def test_relevant_terms_is_the_same_implementation_translate_uses():
    """两处各写一遍必然漂，而漂了就意味着缓存 key 与提示词不是一回事。"""
    plan = chunk_masked("⟦BLK-0⟧\n\n\\section{S}\n\n" + CHUNK + "\n")
    body_chunk = plan.chunks[-1]

    context = tr.assemble_context(body_chunk, plan, glossary=gl.term_map(GLOSSARY))

    assert context.terms == gl.relevant_terms(body_chunk.body, GLOSSARY)


def test_empty_glossary_yields_no_terms():
    assert gl.relevant_terms(CHUNK, None) == ()
    assert gl.relevant_terms(CHUNK, gl.empty()) == ()


def test_content_hash_ignores_provenance_but_not_decisions(tmp_path, monkeypatch):
    """survey 重跑出同样的决策，不该把全部块的翻译一起失效掉（架构 §4）。"""
    base = merged(tmp_path, monkeypatch, paper=table(terms=[{"term": "a", "translation": "甲"}]))
    same_content_other_provenance = gl.Glossary(
        terms=(gl.Term(term="a", translation="甲", source="agent", decided_at="2026-01-01T00:00:00Z"),),
        style=base.style,
        merged_from=(),
    )
    changed = gl.Glossary(terms=(gl.Term(term="a", translation="乙"),), style=base.style)

    assert gl.content_hash(base) == gl.content_hash(same_content_other_provenance)
    assert gl.content_hash(base) != gl.content_hash(changed)


def test_style_version_bump_changes_the_content_hash():
    a = gl.Glossary(style=gl.Style(style_version="1"))
    b = gl.Glossary(style=gl.Style(style_version="2"))

    assert gl.content_hash(a) != gl.content_hash(b)


def test_a_decision_table_survives_a_round_trip_through_disk(tmp_path):
    decided = gl.with_agent_decisions(
        gl.Glossary(style=gl.Style(style_version="7", tone="学术")),
        terms=[{"term": "flow", "translation": "流"}],
    )
    path = write(tmp_path / "glossary.json", decided.to_json())

    again = gl.Glossary.from_json(json.loads(path.read_text(encoding="utf-8")))

    assert gl.content_hash(again) == gl.content_hash(decided)
    assert again.style_version == "7"
    assert again.term("flow").source == "agent"
