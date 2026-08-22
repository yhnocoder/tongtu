from __future__ import annotations

import pytest

from tongtu import validation


def _checks(result: validation.ValidationResult) -> tuple[str, ...]:
    return tuple(failure.check for failure in result.failures)


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


def test_structurally_identical_translation_passes_all_four_checks() -> None:
    result = validation.validate(SOURCE, TRANSLATION)
    assert result.ok
    assert _checks(result) == ()


def test_missing_placeholder_fails_only_the_placeholder_check() -> None:
    result = validation.validate(SOURCE, TRANSLATION.replace("⟦BLK-0⟧\n", ""))
    assert _checks(result) == (validation.CHECK_PLACEHOLDERS,)
    assert "⟦BLK-0⟧" in result.failures[0].message


def test_broken_placeholder_fragment_is_caught_by_the_self_check() -> None:
    result = validation.validate(SOURCE, TRANSLATION.replace("⟦BLK-0⟧", "⟦BLK-0⟧⟧"))
    assert _checks(result) == (validation.CHECK_PLACEHOLDERS,)


def test_star_variant_is_a_distinct_control_sequence() -> None:
    result = validation.validate(SOURCE, TRANSLATION.replace("\\section{", "\\section*{"))
    assert validation.CHECK_CONTROL_SEQUENCES in _checks(result)


def test_escaped_brace_is_a_control_sequence_not_a_brace() -> None:
    assert validation.validate("a \\{ b", "a \\{ b").ok
    assert validation.CHECK_BRACES_AND_MATH in _checks(validation.validate("a { b", "a \\{ b"))


def test_merged_paragraph_fails_the_paragraph_count_check() -> None:
    result = validation.validate(SOURCE, TRANSLATION.replace("\n\n⟦BLK-0⟧", " ⟦BLK-0⟧"))
    assert validation.CHECK_PARAGRAPH_COUNT in _checks(result)


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


def test_an_unescaped_percent_is_caught_by_the_counting_check() -> None:
    source = "我们取 50 个样本。"
    result = validation.validate(source, "我们取 50% 个样本。")
    assert _checks(result) == (validation.CHECK_BRACES_AND_MATH,)
    assert "%" in result.failures[0].message


def test_an_escaped_percent_stays_in_the_control_sequence_check() -> None:
    assert validation.validate("取 50\\% 的样本", "取 50\\% 的样本").ok
    assert _checks(validation.validate("取 50 的样本", "取 50\\% 的样本")) == (validation.CHECK_CONTROL_SEQUENCES,)
