from __future__ import annotations

import pytest

from tongtu import validation


def checks(source: str, translation: str) -> tuple[str, ...]:
    return tuple(failure.check for failure in validation.validate(source, translation).failures)


def message(source: str, translation: str, check: str) -> str:
    found = [failure.message for failure in validation.validate(source, translation).failures if failure.check == check]
    assert found, f"{check} 没有失败"
    return found[0]


SOURCE = """\
\\section{Introduction}
\\label{sec:intro}

We present a model with $n$ layers, see \\citet{ref} and Figure~\\ref{fig:one}.

⟦BLK-0⟧
⟦CAP-0⟧ A caption line.

\\newpage

The second paragraph uses \\textbf{bold} text.\
"""

TRANSLATION = """\
\\section{引言}
\\label{sec:intro}

我们提出一个有 $n$ 层的模型，见 \\citet{ref} 与图~\\ref{fig:one}。

⟦BLK-0⟧
⟦CAP-0⟧ 一行图表标题。

\\newpage

第二段用了 \\textbf{粗体} 文本。\
"""


def test_check_names_are_the_four_layers() -> None:
    assert validation.CHECK_NAMES == (
        "placeholders",
        "control_sequences",
        "braces_and_math",
        "paragraph_count",
    )


def test_structurally_identical_translation_passes_all_four_checks() -> None:
    assert validation.validate(SOURCE, TRANSLATION).ok
    assert checks(SOURCE, TRANSLATION) == ()


def test_missing_placeholder_fails_only_the_placeholder_check() -> None:
    translation = TRANSLATION.replace("⟦BLK-0⟧\n", "")
    assert checks(SOURCE, translation) == (validation.CHECK_PLACEHOLDERS,)
    assert message(SOURCE, translation, validation.CHECK_PLACEHOLDERS) == "译文缺少 ⟦BLK-0⟧"


def test_extra_placeholder_is_reported_as_multiplied_out() -> None:
    translation = TRANSLATION.replace("⟦CAP-0⟧", "⟦CAP-0⟧ ⟦BLK-9⟧")
    assert message(SOURCE, translation, validation.CHECK_PLACEHOLDERS) == "多出 ⟦BLK-9⟧"


def test_broken_placeholder_fragment_is_caught_by_the_self_check() -> None:
    translation = TRANSLATION.replace("⟦BLK-0⟧", "⟦BLK-0⟧⟧")
    assert checks(SOURCE, translation) == (validation.CHECK_PLACEHOLDERS,)
    assert "碎片" in message(SOURCE, translation, validation.CHECK_PLACEHOLDERS)


def test_identical_control_sequences_pass() -> None:
    assert validation.validate("a \\emph{x} b", "甲 \\emph{乙} 丙").ok


def test_star_variant_is_a_distinct_control_sequence() -> None:
    translation = TRANSLATION.replace("\\section{", "\\section*{")
    assert validation.CHECK_CONTROL_SEQUENCES in checks(SOURCE, translation)
    text = message(SOURCE, translation, validation.CHECK_CONTROL_SEQUENCES)
    assert text == "\\section 原文 1 次、译文 0 次；\\section* 原文 0 次、译文 1 次"


def test_control_sequence_difference_reads_as_counts_on_both_sides() -> None:
    text = message("\\cite{a} \\cite{b} x", "\\cite{a} 甲", validation.CHECK_CONTROL_SEQUENCES)
    assert text == "\\cite 原文 2 次、译文 1 次"


def test_escaped_brace_is_a_control_sequence_not_a_brace() -> None:
    assert validation.validate("a \\{ b", "a \\{ b").ok
    assert checks("a { b", "a \\{ b") == (validation.CHECK_CONTROL_SEQUENCES,)


def test_balanced_braces_with_a_different_count_pass() -> None:
    assert validation.validate("a {b} c", "甲 {乙} 丙 {丁}").ok


def test_a_closing_brace_before_any_opening_one_fails() -> None:
    assert checks("a", "{a}}{") == (validation.CHECK_BRACES_AND_MATH,)
    assert message("a", "{a}}{", validation.CHECK_BRACES_AND_MATH) == "{ } 在第 3 字符处不平衡"


def test_an_unclosed_brace_at_the_end_fails() -> None:
    assert message("a", "{a", validation.CHECK_BRACES_AND_MATH) == "{ } 在第 0 字符处不平衡"


def test_an_unbalanced_source_does_not_force_a_failure() -> None:
    assert validation.validate("{a", "{甲").ok


def test_an_odd_number_of_dollars_fails() -> None:
    assert message("a $x$ b", "甲 $x$ 乙 $", validation.CHECK_BRACES_AND_MATH) == "$ 译文 3 个，是奇数，没有成对"


def test_fewer_dollars_than_the_source_fails() -> None:
    assert message("$x$ and $y$", "$x$ 与 y", validation.CHECK_BRACES_AND_MATH) == "$ 原文 4 个、译文 2 个"


def test_more_dollars_than_the_source_pass_when_even() -> None:
    assert validation.validate("$x$", "$x$ 与 $y$").ok


def test_an_unescaped_percent_beyond_the_source_fails() -> None:
    assert checks("我们取 50 个样本。", "我们取 50% 个样本。") == (validation.CHECK_BRACES_AND_MATH,)
    assert "%" in message("我们取 50 个样本。", "我们取 50% 个样本。", validation.CHECK_BRACES_AND_MATH)


def test_an_escaped_percent_stays_in_the_control_sequence_check() -> None:
    assert validation.validate("取 50\\% 的样本", "取 50\\% 的样本").ok
    assert checks("取 50 的样本", "取 50\\% 的样本") == (validation.CHECK_CONTROL_SEQUENCES,)


def test_merged_paragraph_fails_the_paragraph_count_check() -> None:
    translation = TRANSLATION.replace("\n\n⟦BLK-0⟧", " ⟦BLK-0⟧")
    assert validation.CHECK_PARAGRAPH_COUNT in checks(SOURCE, translation)
    text = message(SOURCE, translation, validation.CHECK_PARAGRAPH_COUNT)
    assert text.startswith("含可译文本的段落数：原文 4 段、译文 3 段")


def test_blank_line_around_a_command_only_paragraph_may_be_merged() -> None:
    assert validation.validate(SOURCE, TRANSLATION.replace("\n\n第二段", " 第二段")).ok


@pytest.mark.parametrize(
    "paragraph",
    ["⟦BLK-3⟧", "\\maketitle", "\\newpage", "\\begin{abstract}", "\\includegraphics[width=\\linewidth]{f}", "   "],
)
def test_paragraphs_without_translatable_text_are_not_counted(paragraph: str) -> None:
    assert validation.translatable_paragraphs(paragraph) == 0


def test_paragraph_with_command_argument_is_counted() -> None:
    assert validation.translatable_paragraphs("\\section{Introduction}") == 1


def test_blank_lines_after_a_heading_do_not_change_the_paragraph_count() -> None:
    source = "\\subsection{Model \\dsviii{} as a judge}\n\n\nSome text here.\n"
    translation = "\\subsection{以 \\dsviii{} 为评审的模型}\n一些文本。\n"
    assert validation.validate(source, translation).ok


def test_a_heading_moved_off_the_prose_line_does_not_change_the_paragraph_count() -> None:
    source = "Prose ends here. \\section{Architecture}\n⟦BLK-0⟧ tail text.\n"
    translation = "正文到此结束。\n\n\\section{Architecture}\n⟦BLK-0⟧ 尾部文本。\n"
    assert validation.validate(source, translation).ok
