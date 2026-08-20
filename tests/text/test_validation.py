"""四层 validate 与标题树扫描的文本层用例：无外部依赖，用例取自 stages/translate.md 与 survey.md。"""

from __future__ import annotations

import pytest

from tongtu import chunking, validation


def _checks(result: validation.ValidationResult) -> tuple[str, ...]:
    """未通过的层名，按 `CHECK_NAMES` 的顺序。"""
    return tuple(failure.check for failure in result.failures)


#: 一段掩码文本形态的原文：placeholder、控制序列、inline math 与三个含可译文本的段落齐备。
SOURCE = """\
\\section{Introduction}
\\label{sec:intro}

We present a model with $n$ layers, see \\citet{ref} and Figure~\\ref{fig:one}.

⟦BLK-0⟧
⟦CAP-0⟧ A caption line.

\\newpage

The second paragraph uses \\textbf{bold} text.\
"""

#: 与上面结构完全一致的合格译文。
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
    """结构一致的译文四层全绿。"""
    result = validation.validate(SOURCE, TRANSLATION)
    assert result.ok
    assert _checks(result) == ()


def test_missing_placeholder_fails_only_the_placeholder_check() -> None:
    """漏掉一个 placeholder 只落在 placeholders 层，差异说明指名到具体 token。"""
    result = validation.validate(SOURCE, TRANSLATION.replace("⟦BLK-0⟧\n", ""))
    assert _checks(result) == (validation.CHECK_PLACEHOLDERS,)
    assert "⟦BLK-0⟧" in result.failures[0].message


def test_broken_placeholder_fragment_is_caught_by_the_self_check() -> None:
    """multiset 相等但留下哨兵碎片（⟦BLK-0⟧⟧）同样不通过。"""
    result = validation.validate(SOURCE, TRANSLATION.replace("⟦BLK-0⟧", "⟦BLK-0⟧⟧"))
    assert _checks(result) == (validation.CHECK_PLACEHOLDERS,)


def test_star_variant_is_a_distinct_control_sequence() -> None:
    r"""`\section*` 与 `\section` 是两个不同的项，换了即不通过。"""
    result = validation.validate(SOURCE, TRANSLATION.replace("\\section{", "\\section*{"))
    assert validation.CHECK_CONTROL_SEQUENCES in _checks(result)


def test_escaped_brace_is_a_control_sequence_not_a_brace() -> None:
    r"""`\{` 计入控制序列层而不计入括号层，两层的口径由同一次扫描保证一致。"""
    assert validation.validate("a \\{ b", "a \\{ b").ok
    assert validation.CHECK_BRACES_AND_MATH in _checks(validation.validate("a { b", "a \\{ b"))


def test_merged_paragraph_fails_the_paragraph_count_check() -> None:
    """两个含可译文本的段落被并成一段落在 paragraph_count 层。"""
    result = validation.validate(SOURCE, TRANSLATION.replace("\n\n⟦BLK-0⟧", " ⟦BLK-0⟧"))
    assert validation.CHECK_PARAGRAPH_COUNT in _checks(result)


def test_blank_line_around_a_command_only_paragraph_may_be_merged() -> None:
    r"""`\newpage` 一类不含可译文本的段落被并掉不算失败，实测依据见 docs/models.md。"""
    assert validation.validate(SOURCE, TRANSLATION.replace("\n\n第二段", " 第二段")).ok


@pytest.mark.parametrize(
    "paragraph",
    ["⟦BLK-3⟧", "\\maketitle", "\\newpage", "\\begin{abstract}", "\\includegraphics[width=\\linewidth]{f}", "   "],
)
def test_paragraphs_without_translatable_text_are_not_counted(paragraph: str) -> None:
    """只含 placeholder、元信息命令或环境定界符的段落不计入段落数。"""
    assert validation.translatable_paragraphs(paragraph) == 0


def test_paragraph_with_command_argument_is_counted() -> None:
    """命令的参数是可译文本，带参数的段落照常计入。"""
    assert validation.translatable_paragraphs("\\section{Introduction}") == 1


def test_document_headings_depth_is_relative_to_the_shallowest_level() -> None:
    """标题树的 depth 相对全文最浅层级：最浅的记 1，往深一级加一。"""
    masked = "\\section{One}\n\ntext\n\n\\subsection{Two}\n\ntext\n\n\\section*{Three}\n\ntext\n"
    headings = chunking.document_headings(masked)
    assert [(heading.level, heading.argument, heading.depth) for heading in headings] == [
        ("section", "One", 1),
        ("subsection", "Two", 2),
        ("section", "Three", 1),
    ]


def test_document_headings_is_empty_without_any_heading_command() -> None:
    """全文一个标题命令都没有时返回空元组，由 survey 转成 brief 里的 null。"""
    assert chunking.document_headings("just a paragraph\n\nand another\n") == ()


def test_an_unescaped_percent_is_caught_by_the_counting_check() -> None:
    r"""译文多一个未转义的 `%` 会注释掉那一行的剩余部分，计入 braces_and_math 层。"""
    source = "我们取 50 个样本。"
    result = validation.validate(source, "我们取 50% 个样本。")
    assert _checks(result) == (validation.CHECK_BRACES_AND_MATH,)
    assert "%" in result.failures[0].message


def test_an_escaped_percent_stays_in_the_control_sequence_check() -> None:
    r"""`\%` 是控制序列，不进计数层——两层的口径由同一次扫描保证不重不漏。"""
    assert validation.validate("取 50\\% 的样本", "取 50\\% 的样本").ok
    assert _checks(validation.validate("取 50 的样本", "取 50\\% 的样本")) == (validation.CHECK_CONTROL_SEQUENCES,)
