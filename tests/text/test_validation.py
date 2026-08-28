from __future__ import annotations

from pathlib import Path

import pytest

from tongtu import validation


def checks(source: str, translation: str) -> tuple[str, ...]:
    return tuple(failure.check for failure in validation.validate(source, translation).failures)


def message(source: str, translation: str, check: str) -> str:
    found = [failure.message for failure in validation.validate(source, translation).failures if failure.check == check]
    assert found, f"{check} did not fail"
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
    assert message(SOURCE, translation, validation.CHECK_PLACEHOLDERS) == "translation is missing ⟦BLK-0⟧"


def test_extra_placeholder_is_reported_as_multiplied_out() -> None:
    translation = TRANSLATION.replace("⟦CAP-0⟧", "⟦CAP-0⟧ ⟦BLK-9⟧")
    assert message(SOURCE, translation, validation.CHECK_PLACEHOLDERS) == "translation has extra ⟦BLK-9⟧"


def test_broken_placeholder_fragment_is_caught_by_the_self_check() -> None:
    translation = TRANSLATION.replace("⟦BLK-0⟧", "⟦BLK-0⟧⟧")
    assert checks(SOURCE, translation) == (validation.CHECK_PLACEHOLDERS,)
    assert "fragments" in message(SOURCE, translation, validation.CHECK_PLACEHOLDERS)


def test_identical_control_sequences_pass() -> None:
    assert validation.validate("a \\emph{x} b", "甲 \\emph{乙} 丙").ok


def test_star_variant_is_a_distinct_control_sequence() -> None:
    translation = TRANSLATION.replace("\\section{", "\\section*{")
    assert validation.CHECK_CONTROL_SEQUENCES in checks(SOURCE, translation)
    text = message(SOURCE, translation, validation.CHECK_CONTROL_SEQUENCES)
    assert text == (
        "\\section appears 1 times in source, 0 in translation"
        ' (paragraph 1 beginning "\\section{Introduction} \\label{sec:intro}" has 1 in source, 0 in translation); '
        "\\section* appears 0 times in source, 1 in translation"
        ' (paragraph 1 beginning "\\section{Introduction} \\label{sec:intro}" has 0 in source, 1 in translation)'
    )


def test_control_sequence_difference_reads_as_counts_on_both_sides() -> None:
    text = message("\\cite{a} \\cite{b} x", "\\cite{a} 甲", validation.CHECK_CONTROL_SEQUENCES)
    assert text == (
        "\\cite appears 2 times in source, 1 in translation"
        ' (paragraph 1 beginning "\\cite{a} \\cite{b} x" has 2 in source, 1 in translation)'
    )


def test_control_sequence_difference_is_located_in_the_differing_paragraph() -> None:
    source = "\\emph{a} one\n\n\\emph{b} two\n\n\\emph{c} three"
    translation = "\\emph{甲} 一\n\n乙 二\n\n\\emph{丙} 三"
    assert message(source, translation, validation.CHECK_CONTROL_SEQUENCES) == (
        "\\emph appears 3 times in source, 2 in translation"
        ' (paragraph 2 beginning "\\emph{b} two" has 1 in source, 0 in translation)'
    )


def test_control_sequence_differences_in_several_paragraphs_are_listed_in_order() -> None:
    source = "a \\& b\n\nc\n\nd \\& e\n\ne \\& f"
    translation = "甲 和 乙\n\n丙\n\n丁 \\& 戊\n\n戊 \\& \\& \\& 己"
    assert message(source, translation, validation.CHECK_CONTROL_SEQUENCES) == (
        "\\& appears 3 times in source, 4 in translation"
        ' (paragraph 1 beginning "a \\& b" has 1 in source, 0 in translation; '
        'paragraph 4 beginning "e \\& f" has 1 in source, 3 in translation)'
    )


def test_paragraph_preview_is_flattened_and_cut_short() -> None:
    source = "\\emph{x}\n" + "word " * 20
    text = message(source, "甲", validation.CHECK_CONTROL_SEQUENCES)
    flattened = "\\emph{x} " + "word " * 20
    assert f'beginning "{flattened[: validation.PARAGRAPH_PREVIEW_LENGTH]}…"' in text


def test_differences_are_not_located_when_paragraph_counts_differ() -> None:
    text = message("\\emph{a}\n\nb", "甲 乙", validation.CHECK_CONTROL_SEQUENCES)
    assert text == "\\emph appears 1 times in source, 0 in translation"


def test_escaped_brace_is_a_control_sequence_not_a_brace() -> None:
    assert validation.validate("a \\{ b", "a \\{ b").ok
    assert checks("a { b", "a \\{ b") == (validation.CHECK_CONTROL_SEQUENCES,)


def test_balanced_braces_with_a_different_count_pass() -> None:
    assert validation.validate("a {b} c", "甲 {乙} 丙 {丁}").ok


def test_a_closing_brace_before_any_opening_one_fails() -> None:
    assert checks("a", "{a}}{") == (validation.CHECK_BRACES_AND_MATH,)
    assert message("a", "{a}}{", validation.CHECK_BRACES_AND_MATH) == "{ } unbalanced at character 3"


def test_an_unclosed_brace_at_the_end_fails() -> None:
    assert message("a", "{a", validation.CHECK_BRACES_AND_MATH) == "{ } unbalanced at character 0"


def test_an_unbalanced_source_does_not_force_a_failure() -> None:
    assert validation.validate("{a", "{甲").ok


def test_an_odd_number_of_dollars_fails() -> None:
    assert (
        message("a $x$ b", "甲 $x$ 乙 $", validation.CHECK_BRACES_AND_MATH)
        == "$ count in translation is 3, an odd number, so they cannot pair up"
    )


def test_fewer_dollars_than_the_source_fails() -> None:
    assert (
        message("$x$ and $y$", "$x$ 与 y", validation.CHECK_BRACES_AND_MATH)
        == "$ count differs: 4 in source, 2 in translation"
        ' (paragraph 1 beginning "$x$ and $y$" has 4 in source, 2 in translation)'
    )


def test_dollar_difference_is_located_in_the_differing_paragraph() -> None:
    assert (
        message("$x$\n\n$y$ and $z$", "$x$\n\n$y$ 与 z", validation.CHECK_BRACES_AND_MATH)
        == "$ count differs: 6 in source, 4 in translation"
        ' (paragraph 2 beginning "$y$ and $z$" has 4 in source, 2 in translation)'
    )


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
    assert text.startswith("paragraphs with translatable text: 4 in source, 3 in translation")


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


def test_main_prints_every_layer_and_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "c000.tex"
    translation = tmp_path / "zh.tex"
    source.write_text("We use $x$ here.\n", encoding="utf-8")
    translation.write_text("我们这里用 $x$。\n\n", encoding="utf-8")
    assert validation.main([str(source), str(translation)]) == 0
    output = capsys.readouterr().out
    assert all(f"[pass] {layer}" in output for layer in validation.CHECK_NAMES)


def test_main_reports_the_failing_layer(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "c000.tex"
    translation = tmp_path / "zh.tex"
    source.write_text("We use $x$ here.\n", encoding="utf-8")
    translation.write_text("我们这里用 x。\n", encoding="utf-8")
    assert validation.main([str(source), str(translation)]) == 1
    output = capsys.readouterr().out
    assert "[fail] braces_and_math" in output
    assert "[pass] placeholders" in output


def test_main_without_two_arguments_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert validation.main([]) == 1
    assert validation.USAGE in capsys.readouterr().out


def test_main_reports_an_unreadable_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert validation.main([str(tmp_path / "absent.tex"), str(tmp_path / "zh.tex")]) == 1
    assert "cannot read file" in capsys.readouterr().out
