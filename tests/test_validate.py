"""四层机械校验（架构 §3 translate 出口判据、§12 层 1 文本层门禁）。

每层「通过 + 各种失败形态」，外加边界：空块、纯占位符块、含 ⟦CAP-n⟧ 的块、
中文全角标点不误报。
"""

import pytest

from tongtu import validate as v


def names(errors):
    """失败层名（含重复，按报错顺序），断言用。"""
    return [e.check for e in errors]


# --- 层 1：占位符 multiset ---------------------------------------------------


def test_placeholders_pass_when_reordered():
    orig = "See ⟦BLK-1⟧ and ⟦BLK-2⟧ for details."
    trans = "详见 ⟦BLK-2⟧ 与 ⟦BLK-1⟧。"
    assert v.check(orig, trans) == []


def test_placeholder_missing():
    errors = v.check("A ⟦BLK-1⟧ B ⟦BLK-2⟧", "A ⟦BLK-1⟧ B")
    assert names(errors) == [v.PLACEHOLDERS]
    assert errors[0].missing == ("⟦BLK-2⟧",)
    assert errors[0].extra == ()
    assert "⟦BLK-2⟧" in errors[0].message


def test_placeholder_extra():
    errors = v.check("A ⟦BLK-1⟧", "A ⟦BLK-1⟧ ⟦BLK-1⟧")
    assert names(errors) == [v.PLACEHOLDERS]
    assert errors[0].extra == ("⟦BLK-1⟧",)
    assert errors[0].missing == ()


def test_placeholder_renamed_reports_both_sides():
    errors = v.check("x ⟦BLK-3⟧", "x ⟦BLK-4⟧")
    assert names(errors) == [v.PLACEHOLDERS]
    assert errors[0].missing == ("⟦BLK-3⟧",)
    assert errors[0].extra == ("⟦BLK-4⟧",)


def test_placeholder_multiplicity_matters():
    # multiset 而非集合：重复次数变了也算不一致。
    errors = v.check("⟦BLK-1⟧ ⟦BLK-1⟧", "⟦BLK-1⟧")
    assert names(errors) == [v.PLACEHOLDERS]
    assert errors[0].missing == ("⟦BLK-1⟧",)


def test_placeholder_lowercased_is_not_a_placeholder():
    errors = v.check("x ⟦BLK-1⟧", "x ⟦blk-1⟧")
    checks = names(errors)
    assert v.PLACEHOLDERS in checks
    assert errors[0].missing == ("⟦BLK-1⟧",)
    # 小写形态连定界符自检都过不去（有 ⟦⟧ 却不构成完整占位符）。
    assert any(e.detail == "brackets" for e in errors)


def test_placeholder_debris_caught_although_multiset_matches():
    # ⟦BLK-1⟧⟧ 带着一个完整占位符，multiset 相等，但多余的 ⟧ 会漏进回填后的 TeX。
    errors = v.check("a ⟦BLK-1⟧ b", "甲 ⟦BLK-1⟧⟧ 乙")
    assert names(errors) == [v.PLACEHOLDERS]
    assert errors[0].detail == "brackets"
    assert "译文" in errors[0].message


def test_placeholder_lone_open_bracket_caught():
    errors = v.check("a ⟦BLK-1⟧", "甲 ⟦BLK-1⟧ ⟦")
    assert [e.detail for e in errors] == ["brackets"]


def test_clean_block_has_no_bracket_debris_error():
    assert v.check("⟦BLK-1⟧", "⟦BLK-1⟧") == []


# --- 层 2：控制序列 multiset -------------------------------------------------


def test_control_sequences_pass_when_reordered():
    orig = r"See \ref{fig:a} and \cite{X} in \emph{context}."
    trans = r"在\emph{语境}中参见 \cite{X} 与 \ref{fig:a}。"
    assert v.check(orig, trans) == []


def test_control_sequence_dropped():
    errors = v.check(r"\emph{important} result", r"重要结果")
    checks = names(errors)
    assert v.CONTROL_SEQUENCES in checks
    ctrl = next(e for e in errors if e.check == v.CONTROL_SEQUENCES)
    assert ctrl.missing == (r"\emph",)
    # 花括号一并丢了，第 3 层同时告警（两层互为双保险，非重复实现）。
    assert v.BRACES_AND_MATH in checks


def test_control_sequence_added():
    errors = v.check("plain text", r"\textbf{加粗}文本")
    ctrl = next(e for e in errors if e.check == v.CONTROL_SEQUENCES)
    assert ctrl.extra == (r"\textbf",)


def test_control_sequence_star_variant_is_distinct():
    errors = v.check(r"\section*{Intro}", r"\section{引言}")
    ctrl = next(e for e in errors if e.check == v.CONTROL_SEQUENCES)
    assert ctrl.missing == (r"\section*",)
    assert ctrl.extra == (r"\section",)


def test_control_symbol_line_break_tracked():
    errors = v.check("a \\\\\nb", "甲\n乙")
    ctrl = next(e for e in errors if e.check == v.CONTROL_SEQUENCES)
    assert ctrl.missing == ("\\\\",)


@pytest.mark.parametrize("symbol", [r"\%", r"\&", r"\_", r"\#", r"\{", r"\$"])
def test_control_symbols_preserved_pass(symbol):
    assert v.check(f"a {symbol} b", f"甲 {symbol} 乙") == []


def test_control_sequence_count_change_reported_with_multiplicity():
    errors = v.check(r"\cite{A} \cite{B}", r"\cite{A}")
    ctrl = next(e for e in errors if e.check == v.CONTROL_SEQUENCES)
    assert ctrl.missing == (r"\cite",)
    errors = v.check(r"\ref{A} \ref{B} \ref{C}", r"\ref{A}")
    ctrl = next(e for e in errors if e.check == v.CONTROL_SEQUENCES)
    assert ctrl.missing == (r"\ref", r"\ref")
    assert "×2" in ctrl.message


def test_control_sequences_helper():
    assert v.control_sequences(r"\alpha\beta\alpha") == {r"\alpha": 2, r"\beta": 1}


# --- 层 3：未转义 { } $ 计数 -------------------------------------------------


def test_braces_and_math_pass():
    orig = r"Let $x \in \mathcal{X}$ be given."
    trans = r"设 $x \in \mathcal{X}$ 给定。"
    assert v.check(orig, trans) == []


def test_unbalanced_closing_brace():
    errors = v.check(r"\emph{a} b", r"\emph{甲 乙")
    braces = [e for e in errors if e.check == v.BRACES_AND_MATH]
    assert [e.detail for e in braces] == ["}"]
    assert (braces[0].orig_count, braces[0].trans_count) == (1, 0)


def test_math_delimiter_dropped():
    errors = v.check("$x$ 与 $y$", "$x$ 与 y")
    dollars = next(e for e in errors if e.detail == "$")
    assert (dollars.orig_count, dollars.trans_count) == (4, 2)
    assert dollars.check == v.BRACES_AND_MATH


def test_display_math_collapsed_to_inline_is_caught_by_count():
    errors = v.check("$$x$$", "$x$")
    assert [e.detail for e in errors] == ["$"]
    assert (errors[0].orig_count, errors[0].trans_count) == (4, 2)


def test_escaped_delimiters_not_counted_as_structure():
    # \{ \} \$ 属于控制序列（层 2），不进层 3 计数，故不会重复告警。
    assert v.unescaped_count(r"\{a\}", "{") == 0
    assert v.unescaped_count(r"\{a\}", "}") == 0
    assert v.unescaped_count(r"50\$", "$") == 0
    assert v.check(r"\$5 \{a\}", r"\$5 \{甲\}") == []


def test_escape_chain_resolves_left_to_right():
    # \\ 是换行命令，其后的 { 是真花括号——不能按「前一个字符是反斜杠」判断。
    assert v.unescaped_count(r"\\{", "{") == 1
    assert v.unescaped_count(r"\\\{", "{") == 0
    assert v.unescaped_count("\\", "{") == 0  # 末尾孤立反斜杠不越界


def test_escaped_brace_downgraded_to_real_brace_alarms_twice():
    # 一处缺陷、两层告警：层 2 少了 \{，层 3 多了 {。这是双保险的预期行为。
    errors = v.check(r"a \{ b", r"甲 { 乙")
    assert set(names(errors)) == {v.CONTROL_SEQUENCES, v.BRACES_AND_MATH}


def test_fullwidth_punctuation_does_not_false_alarm():
    orig = "Set $S$ has {a, b}; see \\S1 (100%) — done."
    trans = "集合 $S$ 含 {a、b}；参见 \\S1（100%）——完成。"
    assert v.check(orig, trans) == []


def test_fullwidth_dollar_or_brace_is_a_failure():
    # 全角替身骗不过计数（译文把 $ 打成 ＄）。
    errors = v.check("$x$", "＄x＄")
    assert [e.detail for e in errors] == ["$"]
    assert (errors[0].orig_count, errors[0].trans_count) == (2, 0)


# --- 层 4：段落数 ------------------------------------------------------------


def test_paragraph_count_pass():
    orig = "First para.\n\nSecond para."
    trans = "第一段。\n\n第二段。"
    assert v.check(orig, trans) == []


def test_paragraphs_merged():
    errors = v.check("A.\n\nB.", "甲。乙。")
    assert names(errors) == [v.PARAGRAPH_COUNT]
    assert (errors[0].orig_count, errors[0].trans_count) == (2, 1)


def test_paragraphs_split():
    errors = v.check("A and B.", "甲。\n\n乙。")
    assert names(errors) == [v.PARAGRAPH_COUNT]
    assert (errors[0].orig_count, errors[0].trans_count) == (1, 2)


def test_paragraph_skipped():
    errors = v.check("A.\n\nB.\n\nC.", "甲。\n\n丙。")
    assert names(errors) == [v.PARAGRAPH_COUNT]
    assert (errors[0].orig_count, errors[0].trans_count) == (3, 2)


@pytest.mark.parametrize(
    "text",
    [
        "A.\n\nB.",
        "A.\n\n\n\nB.",
        "A.\n   \nB.",
        "A.\r\n\r\nB.",
        "\n\nA.\n\nB.\n\n",
    ],
)
def test_paragraph_count_normalizes_blank_lines(text):
    assert v.paragraph_count(text) == 2


def test_single_newline_is_not_a_paragraph_break():
    # LaTeX 里单换行只是排版换行，不分段。
    assert v.paragraph_count("A.\nB.") == 1
    assert v.check("A.\nB.", "甲。\n乙。") == []


# --- 边界 -------------------------------------------------------------------


def test_empty_block_passes():
    assert v.check("", "") == []
    assert v.paragraph_count("") == 0


def test_whitespace_only_block_passes():
    assert v.check("   \n\n  ", "\n") == []


def test_empty_translation_of_nonempty_block_fails():
    errors = v.check("Some prose with ⟦BLK-1⟧.", "")
    assert set(names(errors)) == {v.PLACEHOLDERS, v.PARAGRAPH_COUNT}


def test_placeholder_only_block():
    assert v.check("⟦BLK-7⟧", "⟦BLK-7⟧") == []
    errors = v.check("⟦BLK-7⟧", "")
    assert set(names(errors)) == {v.PLACEHOLDERS, v.PARAGRAPH_COUNT}


def test_caption_slot_line_translates_cleanly():
    # ⟦CAP-n⟧ 槽位所在的整行由 mask 抽出，译文只改文字、槽位与命令照抄。
    orig = "Figure ⟦CAP-3⟧ shows the pipeline; see also ⟦BLK-2⟧.\n\n\\caption{⟦CAP-4⟧}"
    trans = "图 ⟦CAP-3⟧ 展示了流水线；另见 ⟦BLK-2⟧。\n\n\\caption{⟦CAP-4⟧}"
    assert v.check(orig, trans) == []


def test_caption_slot_dropped_is_caught():
    errors = v.check("Figure ⟦CAP-3⟧ shows it.", "图展示了它。")
    assert names(errors) == [v.PLACEHOLDERS]
    assert errors[0].missing == ("⟦CAP-3⟧",)


def test_golden_realistic_block_passes():
    orig = (
        "We prove Theorem~\\ref{thm:main} in Section~\\ref{sec:proof}, following "
        "the strategy of \\citet{Foo2020}. The key estimate is $\\|x\\|_2 \\le "
        "\\varepsilon$, which holds for all $x \\in \\mathcal{X}$.\n\n"
        "⟦BLK-12⟧\n\n"
        "The remaining case ($n = 0$) is handled in Appendix~\\ref{app:edge}."
    )
    trans = (
        "我们在第~\\ref{sec:proof}~节证明定理~\\ref{thm:main}，所用策略取自"
        "\\citet{Foo2020}。关键估计是 $\\|x\\|_2 \\le \\varepsilon$，它对所有 "
        "$x \\in \\mathcal{X}$ 成立。\n\n"
        "⟦BLK-12⟧\n\n"
        "余下情形（$n = 0$）在附录~\\ref{app:edge}~中处理。"
    )
    assert v.check(orig, trans) == []


def test_golden_realistic_block_with_damage():
    orig = "The bound $\\varepsilon$ holds; see ⟦BLK-3⟧.\n\nProof in \\ref{sec:x}."
    trans = "界 $\\varepsilon 成立；见 ⟦BLK-4⟧。证明见 \\ref{sec:x}。"
    assert set(names(errors := v.check(orig, trans))) == {
        v.PLACEHOLDERS,
        v.BRACES_AND_MATH,
        v.PARAGRAPH_COUNT,
    }
    assert names(errors) == sorted(names(errors), key=v.CHECKS.index)


# --- 驱动侧接口 --------------------------------------------------------------


def test_error_is_json_serializable():
    import json

    errors = v.check("a ⟦BLK-1⟧", "甲")
    payload = [e.to_dict() for e in errors]
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
    assert payload[0]["check"] == v.PLACEHOLDERS
    assert payload[0]["missing"] == ["⟦BLK-1⟧"]
    assert "extra" not in payload[0]  # 空字段省略


def test_error_is_hashable_and_stringifies_to_message():
    errors = v.check("A.\n\nB.", "甲。")
    assert str(errors[0]) == errors[0].message
    assert len({errors[0], errors[0]}) == 1


def test_failed_checks_dedupes_and_orders():
    errors = v.check(r"\emph{a}$x$", "甲")
    assert v.failed_checks(errors) == (v.CONTROL_SEQUENCES, v.BRACES_AND_MATH)


def test_summarize_counts_layers_once_per_block():
    # `{` 与 `}` 同时对不上只算这一层栽了一次（report.json 统计的是块数）。
    errors = v.check(r"\emph{a}", r"\emph 甲")
    assert [e.detail for e in errors] == ["{", "}"]
    assert v.summarize(errors) == {v.BRACES_AND_MATH: 1}
    assert v.summarize([]) == {}


def test_summarize_keys_match_report_schema():
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "docs/schemas/report.schema.json").read_text(encoding="utf-8")
    )
    documented = schema["properties"]["validation"]["properties"]["failures_by_check"]["properties"]
    assert set(v.CHECKS) == set(documented)


def test_format_errors_numbers_lines_for_agent_prompt():
    errors = v.check("A ⟦BLK-1⟧.\n\nB.", "甲。")
    rendered = v.format_errors(errors)
    assert rendered.splitlines()[0].startswith("1. ")
    assert len(rendered.splitlines()) == len(errors)
    assert v.format_errors([]) == ""
