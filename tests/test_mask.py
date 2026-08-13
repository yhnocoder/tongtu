"""mask 阶段的文本层测试（架构 §12 层 1，PR 门禁）。

三组：

1. **golden-file**：`tests/data/mask/*.tex` → 掩码流与 blocks.json 的定稿快照。
   刻意更新时跑 `UPDATE_MASK_GOLDEN=1 uv run pytest tests/test_mask.py`，diff 进 PR。
2. **往返恒等性质测试**：全部 fixture（含畸形输入）逐字节 `unmask(mask(x)) == x`。
   这与生产环境每篇论文都会跑的自检是同一条判据（架构 §3.1 第 3 条）。
3. **畸形输入**：未闭合环境、不配平花括号——不崩溃、降级为不掩码、警告说得清楚。
"""

import json
import os
from pathlib import Path

import pytest

from tongtu import CONTRACT_VERSION
from tongtu.stages.mask import (
    CATEGORIES,
    EnvQuery,
    classify_environments,
    enumerate_environments,
    load_environment_table,
    mask,
    parse_environment_declarations,
    roundtrip_check,
    roundtrip_diff,
)
from tongtu.stages.unmask import unmask
from tongtu.texlex import (
    Lexer,
    TexLexError,
    find_balanced,
    find_bracket_arg,
    find_env_end,
    skip_verb,
    strip_comments_inline,
)

DATA = Path(__file__).resolve().parent / "data" / "mask"
GOLDEN = DATA / "golden"

UPDATE = os.environ.get("UPDATE_MASK_GOLDEN") == "1"


def fixtures() -> list[Path]:
    return sorted(DATA.glob("*.tex"))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _golden(path: Path, produced: str) -> None:
    if UPDATE:
        path.write_text(produced, encoding="utf-8")
        return
    assert path.exists(), f"缺 golden 文件 {path.name}；用 UPDATE_MASK_GOLDEN=1 生成"
    assert produced == read(path), f"{path.name} 与 golden 不符"


# ----------------------------------------------------------------- golden-file


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_golden_masked_stream(path):
    result = mask(read(path))
    _golden(GOLDEN / f"{path.stem}.masked.tex", result.masked)


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_golden_blocks_json(path):
    src = read(path)
    result = mask(src)
    data = result.to_blocks_json(
        source_path=f"tests/data/mask/{path.name}", roundtrip_ok=roundtrip_check(src)
    )
    _golden(
        GOLDEN / f"{path.stem}.blocks.json",
        json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
    )


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_blocks_json_shape(path):
    """产出符合 docs/schemas/blocks.schema.json 的骨架约定（字段级冒烟）。"""
    data = mask(read(path)).to_blocks_json(roundtrip_ok=True)
    assert data["contract_version"] == CONTRACT_VERSION
    assert len(data["source"]["sha256"]) == 64
    assert data["blocks"][0]["id"] == "BLK-0"
    assert data["blocks"][0]["category"] == "preamble"
    for index, block in enumerate(data["blocks"]):
        assert block["id"] == f"BLK-{index}"
        assert block["placeholder"] == f"⟦BLK-{index}⟧"
        assert block["category"] in CATEGORIES
        assert block["span"]["start"] <= block["span"]["end"]
        assert block["span"]["line_start"] >= 1
    for index, caption in enumerate(data["captions"]):
        assert caption["id"] == f"CAP-{index}"
        assert caption["placeholder"] == f"⟦CAP-{index}⟧"
        assert set(caption) >= {"text", "stream_text", "block_id", "kind"}
    for env in data["environments"]:
        assert env["classification"] in ("prose", "heavy")
        assert env["decided_by"] in (
            "table",
            "newtheorem",
            "newenvironment",
            "agent",
            "default",
        )


# ------------------------------------------------------------- 往返恒等性质


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_roundtrip_identity(path):
    src = read(path)
    assert roundtrip_diff(src) is None


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_spans_point_at_the_original_bytes(path):
    """块的 span 必须真的指向原文那几个字节（anchors 与调试都依赖它）。"""
    src = read(path)
    for block in mask(src).blocks:
        start, end = block.span
        assert src[start:end].endswith(block.tex[-1:] if block.tex else "")
        if not block.caption_ids:
            assert src[start:end] == block.tex


@pytest.mark.parametrize(
    "src",
    [
        "",
        "no document at all\n",
        "\\documentclass{a}\n\\begin{document}\\end{document}",
        "\\begin{document}%\n\\end{document}",
        "\\documentclass{a}\\begin{document}$\\begin{pmatrix}1\\end{pmatrix}$\\end{document}",
        "% only a comment\n",
        "%%%%\n%%%%\n",
        "\\documentclass{a}\n\\begin{document}\ntrailing backslash \\",
        "\\documentclass{a}\n\\begin{document}\n\\begin{figure}\\caption{}\\end{figure}",
        "\\documentclass{a}\n\\begin{document}\n\\begin{figure}\\caption{c}\\end{figure} x",
        "\\documentclass{a}\n\\begin{document}\n\\verb",
        "\\documentclass{a}\n\\begin{document}\n\\verb|unterminated\n",
    ],
    ids=range(12),
)
def test_roundtrip_identity_edge_cases(src):
    assert roundtrip_check(src), roundtrip_diff(src)


def test_roundtrip_check_reuses_a_precomputed_result():
    """驱动器复用已算好的 MaskResult：不重算，更不会把关节③再问一遍。"""
    calls = []

    def arbiter(query: EnvQuery):
        calls.append(query.name)
        return "heavy"

    src = "\\documentclass{a}\n\\begin{document}\n\\begin{qqq}x\\end{qqq}\n\\end{document}\n"
    result = mask(src, arbiter=arbiter)
    assert calls == ["qqq"]
    assert roundtrip_check(src, result=result)
    assert calls == ["qqq"]  # 没有第二次提问


def test_roundtrip_survives_windows_and_unicode():
    src = "\\documentclass{a}\r\n\\begin{document}\r\n中文 ünïcode $x$\r\n\\end{document}\r\n"
    assert roundtrip_check(src)


# ------------------------------------------------------------------ 畸形输入


def test_unclosed_environment_degrades_with_warning():
    src = "\\documentclass{a}\n\\begin{document}\n\\begin{figure}\nno end\n\\end{document}\n"
    result = mask(src)
    assert result.warnings and "未闭合环境 figure" in result.warnings[0]
    assert "第 3 行" in result.warnings[0]
    # 不掩码 ≠ 损坏：环境原样留在流里，往返照样恒等
    assert "\\begin{figure}" in result.masked
    assert unmask(result.masked, result) == src


def test_unclosed_environment_does_not_swallow_the_rest():
    src = read(DATA / "malformed.tex")
    result = mask(src)
    masked = result.masked
    assert "\\begin{figure}" in masked  # 未闭合的 figure 留在流里
    assert any("未闭合环境 figure" in w for w in result.warnings)
    assert any("caption 参数不配平" in w for w in result.warnings)
    # 后面的 table 照常掩码，散文照常进流
    assert [b.environment for b in result.blocks if b.environment] == ["table"]
    assert "Prose after the damage still reaches the stream" in masked


def test_unbalanced_caption_braces_keep_the_block_intact():
    src = (
        "\\documentclass{a}\n\\begin{document}\n"
        "\\begin{table}\\caption{oops {unclosed}\\end{table}\ntail\n\\end{document}\n"
    )
    result = mask(src)
    assert result.captions == ()  # 抽不出槽位就整块掩码，不半途而废
    assert any("不配平" in w for w in result.warnings)
    assert unmask(result.masked, result) == src


def test_missing_begin_document_is_reported_not_raised():
    result = mask("fragment with \\begin{equation}x\\end{equation}\n")
    assert any("未找到 \\begin{document}" in w for w in result.warnings)
    assert result.blocks[0].id == "BLK-0" and result.blocks[0].tex == ""


# --------------------------------------------------------------- 掩码流内容


def test_prose_keeps_inline_math_citations_and_wrappers():
    masked = mask(read(DATA / "article_basic.tex")).masked
    assert "$x_i$" in masked
    assert "\\cite{knuth1984}" in masked
    assert "\\begin{itemize}" in masked and "\\item Prose environments" in masked
    assert "50\\% of the pipeline" in masked  # 转义百分号不是注释
    assert "\\begin{equation}" not in masked  # 行间公式整块掩掉


def test_comments_become_blocks_and_runs_are_merged():
    result = mask(read(DATA / "article_basic.tex"))
    comments = [b for b in result.blocks if b.category == "comment"]
    assert len(comments) == 1  # 行尾注释 + 两行整行注释合并成一块
    assert comments[0].tex.startswith("% 行尾注释")
    assert comments[0].tex.count("\n") == 2
    assert "%" not in result.masked.split("⟦BLK-0⟧")[1].replace("\\%", "")


def test_verbatim_bodies_are_opaque():
    result = mask(read(DATA / "verbatim_verb.tex"))
    names = {b.environment for b in result.blocks if b.environment}
    assert names == {"lstlisting", "verbatim", "alltt"}
    # 假环境不进枚举、假注释不被当注释、假 caption 不成槽位
    assert {e.name for e in result.environments} == {"document", "lstlisting", "verbatim", "alltt"}
    assert result.captions == ()
    assert "\\verb|%|" in result.masked
    assert "\\verbatiminput{snippet.txt}" in result.masked


def test_caption_optional_argument_becomes_its_own_slot():
    result = mask(read(DATA / "article_basic.tex"))
    kinds = [(c.kind, c.stream_text) for c in result.captions]
    assert ("caption", "Pipeline overview") in kinds  # 进目录的短标题也要翻
    assert any(text.startswith("The deterministic pipeline") for _, text in kinds)
    long_caption = next(c for c in result.captions if c.stream_text.startswith("The det"))
    assert "\n" in long_caption.text  # 原文保留换行
    assert "\n" not in long_caption.stream_text  # 流里单行


def test_preamble_title_and_abstract_slots():
    result = mask(read(DATA / "preamble_abstract.tex"))
    kinds = [c.kind for c in result.captions]
    assert kinds[:2] == ["title", "abstract"]
    assert result.captions[0].text == "Masking, Unmasking and the Round Trip"
    assert " \\par " in result.captions[1].stream_text  # 段落以 \par 相连
    # 注释掉的 \title 不算数（v2 的正则会抓到它）
    assert "An old title left in a comment" in result.blocks[0].tex
    assert all("old title" not in c.text for c in result.captions)


def test_caption_star_and_captionof_are_slots():
    result = mask(read(DATA / "preamble_abstract.tex"))
    texts = [c.stream_text for c in result.captions]
    assert "Right panel, unnumbered." in texts  # \caption*
    assert "Short table caption" in texts  # \captionof 的可选参数
    assert any(t.startswith("A \\verb|\\captionof|") for t in texts)


def test_caption_line_does_not_split_a_paragraph():
    """v2 在 CAP 行前后各加一个换行，会把段落中间的图变成两段。"""
    src = (
        "\\documentclass{a}\n\\begin{document}\n"
        "Before \\begin{table}\\caption{Cap}\\end{table} after.\n\\end{document}\n"
    )
    masked = mask(src).masked
    assert "\n\n" not in masked.split("Before")[1]


# ------------------------------------------------------------- 枚举与分类


def test_enumeration_needs_no_prior_knowledge():
    src = read(DATA / "theorem_unknown.tex")
    counts = enumerate_environments(src, load_environment_table().verbatim_envs)
    assert counts["minipage"][0] == 2
    assert "cosmoplot" in counts  # 没人认识它，但枚举得到
    assert counts["figure"][0] == 1  # \newenvironment 定义体里的那一个


def test_declarations_override_and_classify():
    src = read(DATA / "theorem_unknown.tex")
    table = load_environment_table()
    decls = parse_environment_declarations(src, table)
    assert decls["lemma"] == ("prose", None, "newtheorem")
    assert decls["remark"] == ("prose", None, "newtheorem")
    assert decls["highlight"] == ("prose", None, "newenvironment")
    assert decls["myfigure"] == ("heavy", "figure", "newenvironment")
    assert decls["fancybox"] == ("heavy", "other", "newenvironment")


def test_unknown_environment_is_masked_conservatively():
    result = mask(read(DATA / "theorem_unknown.tex"))
    unknown = [b for b in result.blocks if b.category == "unknown"]
    assert [b.environment for b in unknown] == ["cosmoplot"]
    info = next(e for e in result.environments if e.name == "cosmoplot")
    assert (info.classification, info.decided_by) == ("heavy", "default")
    assert "An environment nobody has ever heard of" not in result.masked


def test_declared_theorem_bodies_stay_translatable():
    result = mask(read(DATA / "theorem_unknown.tex"))
    assert "\\begin{lemma}[Masking]" in result.masked
    assert "Declared theorem environments are prose" in result.masked
    assert "Starred declarations count too." in result.masked


def test_arbiter_hook_is_the_agent_joint():
    """关节③的 hook：M3 接 agent，本期只验证签名与两种裁决都生效。"""
    asked = []

    def arbiter(query: EnvQuery):
        asked.append(query)
        return "prose" if query.name == "chatty" else "heavy"

    src = (
        "\\documentclass{a}\n\\begin{document}\n"
        "\\begin{chatty}talk\\end{chatty}\n\\begin{opaque}data\\end{opaque}\n\\end{document}\n"
    )
    result = mask(src, arbiter=arbiter)
    assert {q.name for q in asked} == {"chatty", "opaque"}
    assert asked[0].count == 1 and asked[0].sample.startswith("\\begin{")
    assert "talk" in result.masked and "data" not in result.masked
    decided = {e.name: (e.classification, e.decided_by, e.category) for e in result.environments}
    assert decided["chatty"] == ("prose", "agent", None)
    assert decided["opaque"] == ("heavy", "agent", "other")
    assert roundtrip_check(src, arbiter=arbiter)


def test_classify_environments_defaults_to_no_arbiter():
    """分类函数的裁决回调是可选的，默认 None → 未知环境保守整块掩码。"""
    counts = {"figure": (2, 0), "lemma": (1, 10), "mystery": (1, 20)}
    decided = classify_environments(counts, declarations={"lemma": ("prose", None, "newtheorem")})
    assert decided["figure"].classification == "heavy"
    assert decided["figure"].count == 2
    assert decided["lemma"].decided_by == "newtheorem"
    assert (decided["mystery"].classification, decided["mystery"].category) == (
        "heavy",
        "unknown",
    )


def test_arbiter_failure_falls_back_to_conservative_default():
    def arbiter(query: EnvQuery):
        raise RuntimeError("agent 掉线")

    src = "\\documentclass{a}\n\\begin{document}\n\\begin{zzz}x\\end{zzz}\n\\end{document}\n"
    result = mask(src, arbiter=arbiter)
    info = next(e for e in result.environments if e.name == "zzz")
    assert (info.classification, info.decided_by) == ("heavy", "default")


def test_environment_table_is_wellformed():
    table = load_environment_table()
    assert table.version >= 1
    for name, rule in table.rules.items():
        assert rule.classification in ("prose", "heavy"), name
        if rule.classification == "heavy":
            assert rule.category in CATEGORIES, name
        else:
            assert rule.category is None and not rule.verbatim, name
    assert table.lookup("figure*").category == "figure"  # 星号变体继承
    assert "lstlisting" in table.verbatim_envs and "verbatim*" in table.verbatim_envs


# ----------------------------------------------------------------- 词法原语


def test_skip_verb_requires_a_non_letter_delimiter():
    assert skip_verb("\\verb|x|", 0) == 8
    assert skip_verb("\\verb*+a b+ rest", 0) == 11
    assert skip_verb("\\verbatiminput{f}", 0) is None  # v2 会把 a 当定界符
    assert skip_verb("\\verb|unterminated\nnext", 0) is None


def test_find_balanced_ignores_comments_and_verb():
    assert find_balanced("{a % }\n b}", 0) == 9
    assert find_balanced("{\\verb|}|}", 0) == 9
    assert find_balanced("{\\{ }", 0) == 4
    with pytest.raises(TexLexError):
        find_balanced("{unclosed", 0)


def test_find_bracket_arg_handles_nesting():
    assert find_bracket_arg("[a[b]c]", 0) == 6
    assert find_bracket_arg("[{a]b}]", 0) == 6


def test_find_env_end_counts_nesting_and_skips_verbatim():
    src = "\\begin{a}\\begin{a}x\\end{a}\\end{a}"
    assert find_env_end(src, 0, "a") == len(src)
    src = "\\begin{f}\\begin{verbatim}\\end{f}\\end{verbatim}\\end{f}"
    assert find_env_end(src, 0, "f", {"verbatim"}) == len(src)
    with pytest.raises(TexLexError):
        find_env_end("\\begin{a}x", 0, "a")


def test_lexer_reports_control_words_whole():
    lexer = Lexer("\\captionsetup{a}\\caption{b}")
    kinds = [(t.kind, "\\captionsetup{a}\\caption{b}"[t.start : t.end]) for t in lexer]
    assert ("control", "\\captionsetup") in kinds
    assert ("control", "\\caption") in kinds


def test_strip_comments_inline_collapses_whitespace():
    assert strip_comments_inline("a % note\n  b\n") == "a b"
    assert strip_comments_inline("100\\% sure") == "100\\% sure"
