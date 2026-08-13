"""chunk 阶段：章节树优先分块（架构 §3 chunk 行、决策 12、开放问题 1）。"""

import json
from pathlib import Path

import pytest

from tongtu.stages import chunk as ck

DATA = Path(__file__).parent / "data" / "chunk"

#: golden 用的软/硬参数——fixture 论文只有几百 token，用缩小的参数才能压住结构规则。
GOLDEN_SOFT, GOLDEN_HARD = 120, 240


def load(name: str) -> str:
    return (DATA / f"{name}.masked.tex").read_text(encoding="utf-8")


@pytest.fixture
def small() -> str:
    return load("small_paper")


@pytest.fixture
def long_paper() -> str:
    return load("long_section")


def plan_of(text: str, soft: int = GOLDEN_SOFT, hard: int = GOLDEN_HARD, **kw) -> ck.ChunkPlan:
    return ck.chunk_masked(text, soft_target=soft, hard_limit=hard, **kw)


# --------------------------------------------------------------------------- #
# golden
# --------------------------------------------------------------------------- #


def test_golden_manifest(small):
    golden = json.loads((DATA / "small_paper.chunks.json").read_text(encoding="utf-8"))
    assert plan_of(small).to_manifest() == golden


def test_manifest_is_json_serializable(small):
    manifest = plan_of(small).to_manifest()
    assert json.loads(json.dumps(manifest, ensure_ascii=False)) == manifest
    assert [c["id"] for c in manifest["chunks"]] == ["c000", "c001", "c002", "c003"]
    assert [c["file"] for c in manifest["chunks"]] == ["c000.tex", "c001.tex", "c002.tex", "c003.tex"]


def test_chunk_files_are_writable_units(small):
    plan = plan_of(small)
    files = plan.chunk_files()
    assert list(files) == [c.file for c in plan.chunks]
    for chunk in plan.chunks:
        assert files[chunk.file] == chunk.body + "\n"
        assert not files[chunk.file].startswith("\n")


# --------------------------------------------------------------------------- #
# 恒等性质：无丢段、段落不可拆、环境不切开
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["small_paper", "long_section"])
@pytest.mark.parametrize("soft,hard", [(40, 60), (80, 150), (120, 240), (4000, 8000)])
def test_reassembly_is_exact(name, soft, hard):
    text = load(name)
    plan = plan_of(text, soft, hard)
    assert plan.reassemble() == text


@pytest.mark.parametrize("name", ["small_paper", "long_section"])
@pytest.mark.parametrize("soft,hard", [(40, 60), (80, 150), (120, 240), (4000, 8000)])
def test_chunks_tile_the_paragraph_stream(name, soft, hard):
    """每块 = 一段**连续完整**的段落序列，块与块首尾相接覆盖全部段落。"""
    text = load(name)
    plan = plan_of(text, soft, hard)
    assert plan.chunks[0].para_start == 0
    assert plan.chunks[-1].para_end == len(plan.paragraphs)
    for prev, nxt in zip(plan.chunks, plan.chunks[1:]):
        assert prev.para_end == nxt.para_start
    for chunk in plan.chunks:
        members = plan.paragraphs_of(chunk)
        assert len(members) == chunk.paragraph_count >= 1
        # 块正文恰好是首段起点到末段终点的原文切片——段落没有被切开
        assert chunk.body == text[members[0].start : members[-1].end]
        assert chunk.tokens == sum(p.tokens for p in members)


@pytest.mark.parametrize("soft,hard", [(40, 60), (120, 240)])
def test_environments_are_never_cut(soft, hard):
    """散文环境（itemize 等）内部含空行，也必须整体落在同一块里。"""
    text = load("small_paper")
    plan = plan_of(text, soft, hard)
    itemize = [p for p in plan.paragraphs if "\\begin{itemize}" in p.text]
    assert len(itemize) == 1
    assert "\\end{itemize}" in itemize[0].text
    assert itemize[0].text.count("\n\n") >= 1, "fixture 的 itemize 内部应含空行"
    for chunk in plan.chunks:
        assert chunk.body.count("\\begin{itemize}") == chunk.body.count("\\end{itemize}")


def test_environment_names_may_contain_digits():
    text = "⟦BLK-0⟧\n\n\\begin{algorithm2e}\nStep one.\n\nStep two.\n\\end{algorithm2e}\n\n\\section{After}\nText.\n"
    plan = ck.chunk_masked(text)
    assert len(plan.paragraphs) == 3
    assert "Step two." in plan.paragraphs[1].text
    assert plan.reassemble() == text


def test_split_paragraphs_keeps_environment_whole(small):
    paragraphs = ck.split_paragraphs(small)
    assert [p.index for p in paragraphs] == list(range(len(paragraphs)))
    assert sum("\\item" in p.text for p in paragraphs) == 1


# --------------------------------------------------------------------------- #
# 章节树
# --------------------------------------------------------------------------- #


def test_front_matter_is_the_first_chunk(small):
    plan = plan_of(small)
    first = plan.chunks[0]
    assert first.is_front_matter is True
    assert first.section_path == ()
    assert "⟦BLK-0⟧" in first.body and "⟦CAP-0⟧" in first.body
    assert first.paragraph_count == 1
    assert all(not c.is_front_matter for c in plan.chunks[1:])
    # 首块不与正文章节聚合，哪怕软目标大到装得下全文
    assert ck.chunk_masked(small).chunks[0].para_end == 1


def test_section_paths_and_numbering(small):
    plan = plan_of(small)
    paths = {p.index: p.section_path for p in plan.paragraphs}
    assert paths[0] == ()
    assert paths[1] == ("1",)
    assert paths[4] == ("2", "2.1")
    assert paths[7] == ("2", "2.2")
    assert paths[8] == ("*1",), "\\section* 不编号，按同级出现序记 *n"
    assert paths[10] == ("A",), "\\appendix 之后的节用字母编号"
    assert paths[11] == ("A", "A.1")
    titles = {p.index: p.section_titles for p in plan.paragraphs}
    assert titles[4] == ("Method", "Masking")
    assert titles[10] == ("Proofs",)


def test_chunk_section_path_is_common_prefix(long_paper):
    plan = plan_of(long_paper)
    by_id = {c.id: c for c in plan.chunks}
    assert by_id["c003"].section_path == ("3", "3.1")  # 单个小节
    assert by_id["c002"].section_path == ("3",)  # 节首概述段
    assert by_id["c001"].section_path == ()  # 聚合了 §1 与 §2
    assert by_id["c001"].section_titles == ()


def test_headings_recorded_per_chunk(small):
    plan = plan_of(small)
    assert [h.path for h in plan.chunks[1].headings] == [("1",)]
    assert [h.path for h in plan.chunks[2].headings] == [
        ("2",),
        ("2", "2.1"),
        ("2", "2.2"),
        ("*1",),
    ]
    assert plan.chunks[2].headings[0].command == "section"
    assert plan.chunks[2].headings[3].numbered is False


def test_appendix_is_flagged_and_never_merged_with_the_body(small):
    for plan in (plan_of(small), ck.chunk_masked(small)):
        appendix = [c for c in plan.chunks if c.is_appendix]
        assert appendix, "附录应当成块"
        assert appendix[-1] is plan.chunks[-1]
        body = [c for c in plan.chunks if not c.is_appendix]
        assert all(b.index < appendix[0].index for b in body)
        # \appendix 这一行（glue 段）跟随附录首节
        assert appendix[0].body.startswith("\\appendix")
        assert appendix[0].section_path == ("A",)


def test_appendices_environment_marks_and_letters_the_appendix():
    text = (
        "⟦BLK-0⟧\n\n\\section{Body}\nBody prose.\n\n"
        "\\begin{appendices}\n\n\\section{App}\nAppendix prose.\n\n"
        "\\subsection{Deep}\nMore.\n\n\\end{appendices}\n"
    )
    plan = ck.chunk_masked(text)
    paths = [p.section_path for p in plan.paragraphs]
    assert paths == [(), ("1",), ("1",), ("A",), ("A", "A.1"), ("A", "A.1")]
    assert [p.is_appendix for p in plan.paragraphs] == [False, False, True, True, True, True]
    assert [c.is_appendix for c in plan.chunks] == [False, False, True]
    assert plan.reassemble() == text


def test_appendix_macro_may_share_a_paragraph_with_its_first_section():
    text = "⟦BLK-0⟧\n\n\\section{Body}\nA.\n\n\\appendix\n\\section{App}\nB.\n"
    plan = ck.chunk_masked(text)
    assert plan.paragraphs[2].heading is not None
    assert plan.paragraphs[2].section_path == ("A",)
    assert plan.chunks[-1].is_appendix is True


def test_headings_inside_environments_are_not_section_boundaries():
    text = "⟦BLK-0⟧\n\n\\begin{quote}\n\\section{Nope}\n\nStill quoted.\n\\end{quote}\n\n\\section{Real}\nText.\n"
    plan = ck.chunk_masked(text)
    assert [p.section_path for p in plan.paragraphs] == [(), (), ("1",)]
    assert plan.paragraphs[1].text.endswith("\\end{quote}")
    assert plan.reassemble() == text


def test_crlf_source_still_splits_into_paragraphs():
    text = "⟦BLK-0⟧\r\n\r\n\\section{One}\r\nAlpha.\r\n\r\nBeta.\r\n"
    plan = ck.chunk_masked(text)
    assert len(plan.paragraphs) == 3
    assert plan.reassemble() == text


def test_document_without_headings_is_one_front_matter_run():
    text = "⟦BLK-0⟧\n\nFirst prose paragraph.\n\nSecond prose paragraph.\n"
    plan = ck.chunk_masked(text)
    assert len(plan.paragraphs) == 3
    assert len(plan.chunks) == 1
    assert plan.chunks[0].is_front_matter is True
    assert plan.reassemble() == text


# --------------------------------------------------------------------------- #
# 聚合 / 下分 / 尾块
# --------------------------------------------------------------------------- #


def test_small_sections_aggregate_up_to_the_soft_target(long_paper):
    plan = plan_of(long_paper)
    aggregated = plan.chunks[1]
    assert [h.path for h in aggregated.headings] == [("1",), ("2",)]
    assert aggregated.tokens <= GOLDEN_SOFT
    # 软目标收紧到装不下两节 → 两节各自成块
    tight = plan_of(long_paper, 60, 240)
    assert [h.path for h in tight.chunks[1].headings] == [("1",)]
    assert [h.path for h in tight.chunks[2].headings] == [("2",)]


def test_oversized_section_splits_at_subsection_boundaries(long_paper):
    plan = plan_of(long_paper)
    parts = [c for c in plan.chunks if c.part_count > 1]
    assert [c.id for c in parts] == ["c002", "c003", "c004", "c005"]
    assert [c.part for c in parts] == [1, 2, 3, 4]
    assert all(c.tokens <= GOLDEN_HARD for c in parts)
    assert parts[0].section_path == ("3",)
    for part in parts[1:]:
        assert part.headings[0].command == "subsection"
        assert part.section_path[0] == "3"
    # 下分后的分片不再与后续章节聚合
    assert parts[-1].para_end == plan.chunks[6].para_start


def test_oversized_section_without_subsections_falls_back_to_paragraphs(long_paper):
    plan = plan_of(long_paper, 80, 150)
    experiments = [c for c in plan.chunks if c.section_path == ("4",)]
    assert len(experiments) > 1, "无小节的超大节应按段落边界下分"
    assert all(c.headings == () for c in experiments[1:]), "分片不是从标题起头"
    assert all(c.tokens <= 150 for c in experiments)
    assert experiments[0].part_count == len(experiments)


def test_single_oversized_paragraph_is_never_split(long_paper):
    plan = plan_of(long_paper, 80, 150)
    notation = [c for c in plan.chunks if c.section_path == ("5",)]
    assert len(notation) == 1
    assert notation[0].paragraph_count == 1
    assert notation[0].tokens > 150, "段落不可拆——超过硬上限也整块保留"


def test_tail_chunk_is_merged_backwards(long_paper):
    plan = plan_of(long_paper)
    tail = plan.chunks[-1]
    assert [h.path for h in tail.headings] == [("5",), ("6",)]
    assert tail.tokens <= GOLDEN_HARD
    # 合并后会超硬上限时不并
    strict = plan_of(long_paper, 80, 150)
    assert [h.path for h in strict.chunks[-1].headings] == [("6",)]
    assert strict.chunks[-1].tokens < strict.tail_min


def test_tail_min_parameter_controls_the_merge(long_paper):
    assert [h.path for h in plan_of(long_paper, tail_min=0).chunks[-1].headings] == [("6",)]
    assert plan_of(long_paper, tail_min=0).tail_min == 0
    assert plan_of(long_paper).tail_min == GOLDEN_SOFT // 4


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #


def test_defaults_are_the_architecture_starting_values():
    assert (ck.SOFT_TARGET_TOKENS, ck.HARD_LIMIT_TOKENS) == (4000, 8000)
    plan = ck.chunk_masked(load("long_section"))
    assert (plan.soft_target, plan.hard_limit) == (4000, 8000)
    # fixture 全文远小于软目标：首块 + 正文一块
    assert len(plan.chunks) == 2


@pytest.mark.parametrize("name", ["small_paper", "long_section"])
def test_smaller_limits_never_produce_fewer_chunks(name):
    text = load(name)
    counts = [len(plan_of(text, soft, hard)) for soft, hard in [(4000, 8000), (120, 240), (80, 150), (40, 60)]]
    assert counts == sorted(counts), counts
    assert counts[0] < counts[-1]


def test_hard_limit_bounds_every_multi_paragraph_chunk(long_paper):
    plan = plan_of(long_paper, 80, 150)
    for chunk in plan.chunks:
        if chunk.paragraph_count > 1:
            assert chunk.tokens <= 150


@pytest.mark.parametrize(
    "kwargs",
    [
        {"soft_target": 0},
        {"soft_target": -1},
        {"soft_target": 100, "hard_limit": 50},
        {"tail_min": -1},
    ],
)
def test_illegal_parameters_rejected(small, kwargs):
    with pytest.raises(ck.ChunkError):
        ck.chunk_masked(small, **kwargs)


def test_blank_input_yields_no_chunks():
    for text in ("", "   \n\n  \n"):
        plan = ck.chunk_masked(text)
        assert plan.chunks == () and plan.paragraphs == ()
        assert plan.reassemble() == ""


# --------------------------------------------------------------------------- #
# 邻域与 token 估算
# --------------------------------------------------------------------------- #


def test_neighbour_indices_chain_the_chunks(long_paper):
    plan = plan_of(long_paper)
    assert plan.chunks[0].prev_tail_para is None
    assert plan.chunks[-1].next_head_para is None
    for prev, nxt in zip(plan.chunks, plan.chunks[1:]):
        assert prev.next_head_para == nxt.para_start
        assert nxt.prev_tail_para == prev.para_end - 1
        assert plan.paragraphs[nxt.prev_tail_para].text == plan.paragraphs_of(prev)[-1].text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", 0),
        ("word", 1),
        ("abcdefgh", 2),
        ("⟦BLK-0⟧", 3),  # 占位符 3 token
        ("⟦CAP-12⟧", 3),
        ("\\alpha", 2),  # 控制序列 2 token
        ("\\{", 2),
        ("中文测试", 4),
        ("one two ten", 3),
        ("one two three", 4),  # three 有 5 字符 → ceil(5/4) = 2
    ],
)
def test_token_estimate_rules(text, expected):
    assert ck.estimate_tokens(text) == expected


def test_token_estimate_is_additive_over_paragraphs(small):
    plan = plan_of(small)
    assert sum(c.tokens for c in plan.chunks) == sum(p.tokens for p in plan.paragraphs)


def test_appendices_macro_marks_appendix():
    """IEEEtran 的 `\\appendices` 同样标志附录（回归：曾只认 `\\appendix`）。"""
    masked = (
        "\\documentclass{article}\n\\begin{document}\n"
        "\\section{Body}\ntext here\n\n"
        "\\appendices\n\n"
        "\\section{Extra}\nappendix text\n\\end{document}"
    )
    from tongtu.stages.chunk import chunk_masked

    plan = chunk_masked(masked, soft_target=10, hard_limit=100)
    flags = [c.is_appendix for c in plan.chunks]
    assert flags[-1] is True, "\\appendices 之后的块应标 is_appendix"
    assert plan.chunks[-1].section_path == ("A",)
    assert not any(c.is_appendix for c in plan.chunks[:-1])
