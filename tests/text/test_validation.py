from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tongtu import validation

REPOSITORY = Path(__file__).resolve().parents[2]


PARAGRAPH_FAILURE_RE = re.compile(r"paragraph starting at line ([0-9]+) of the translation, ([a-z_]+): (.*)")


def located(source: str, translation: str) -> list[tuple[int, str, str]]:
    found = []
    for problem in validation.check(source, translation).problems:
        match = PARAGRAPH_FAILURE_RE.fullmatch(problem.message)
        if problem.category is validation.Category.STRUCTURE and match:
            found.append((int(match.group(1)), match.group(2), match.group(3)))
    return found


def structure(source: str, translation: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for _line, name, text in located(source, translation):
        found.setdefault(name, text)
    return found


def checks(source: str, translation: str) -> tuple[str, ...]:
    return tuple(structure(source, translation))


def message(source: str, translation: str, check: str) -> str:
    found = structure(source, translation)
    assert check in found, f"{check} did not fail"
    return found[check]


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
\\section{Introduction}
\\label{sec:intro}

我们提出一个有 $n$ 层的模型，见 \\citet{ref} 与图~\\ref{fig:one}。

⟦BLK-0⟧
⟦CAP-0⟧ 一行图表标题。

\\newpage

第二段用了 \\textbf{粗体} 文本。\
"""


def test_structurally_identical_translation_passes_every_check() -> None:
    assert validation.check(SOURCE, TRANSLATION).ok


def test_missing_placeholder_fails_only_the_placeholder_check() -> None:
    translation = TRANSLATION.replace("⟦BLK-0⟧", "图")
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
    assert validation.check("a \\emph{x} b", "甲 \\emph{乙} 丙").ok


def test_star_variant_is_a_distinct_control_sequence() -> None:
    translation = TRANSLATION.replace("\\section{", "\\section*{")
    assert validation.CHECK_CONTROL_SEQUENCES in checks(SOURCE, translation)
    text = message(SOURCE, translation, validation.CHECK_CONTROL_SEQUENCES)
    assert text == (
        "\\section appears 1 times in source, 0 in translation; \\section* appears 0 times in source, 1 in translation"
    )


def test_control_sequence_difference_reads_as_counts_on_both_sides() -> None:
    text = message("\\cite{a} \\cite{b} x", "\\cite{a} 甲", validation.CHECK_CONTROL_SEQUENCES)
    assert text == "\\cite appears 2 times in source, 1 in translation"


def test_control_sequence_difference_is_reported_in_the_differing_paragraph() -> None:
    source = "\\emph{a} one\n\n\\emph{b} two\n\n\\emph{c} three"
    translation = "\\emph{甲} 一\n\n乙 二\n\n\\emph{丙} 三"
    assert located(source, translation) == [
        (3, validation.CHECK_CONTROL_SEQUENCES, "\\emph appears 1 times in source, 0 in translation")
    ]


def test_control_sequence_differences_in_several_paragraphs_are_reported_in_order() -> None:
    source = "a \\& b\n\nc\n\nd \\& e\n\ne \\& f"
    translation = "甲 和 乙\n\n丙\n\n丁 \\& 戊\n\n戊 \\& \\& \\& 己"
    assert located(source, translation) == [
        (1, validation.CHECK_CONTROL_SEQUENCES, "\\& appears 1 times in source, 0 in translation"),
        (7, validation.CHECK_CONTROL_SEQUENCES, "\\& appears 1 times in source, 3 in translation"),
    ]


def test_a_control_sequence_moved_to_another_paragraph_fails_both_paragraphs() -> None:
    source = "\\emph{a} one\n\ntwo"
    translation = "一\n\n\\emph{甲} 二"
    assert [(line, name) for line, name, _text in located(source, translation)] == [
        (1, validation.CHECK_CONTROL_SEQUENCES),
        (3, validation.CHECK_CONTROL_SEQUENCES),
    ]


def test_placeholders_swapped_between_paragraphs_fail_both_paragraphs() -> None:
    source = "⟦BLK-0⟧\nfirst\n\n⟦BLK-1⟧\nsecond"
    translation = "⟦BLK-1⟧\n一\n\n⟦BLK-0⟧\n二"
    assert located(source, translation) == [
        (1, validation.CHECK_PLACEHOLDERS, "translation is missing ⟦BLK-0⟧; translation has extra ⟦BLK-1⟧"),
        (4, validation.CHECK_PLACEHOLDERS, "translation is missing ⟦BLK-1⟧; translation has extra ⟦BLK-0⟧"),
    ]


def test_escaped_brace_is_a_control_sequence_not_a_brace() -> None:
    assert validation.check("a \\{ b", "a \\{ b").ok
    assert checks("a { b", "a \\{ b") == (validation.CHECK_CONTROL_SEQUENCES,)


def test_balanced_braces_with_a_different_count_pass() -> None:
    assert validation.check("a {b} c", "甲 {乙} 丙 {丁}").ok


def test_a_closing_brace_before_any_opening_one_fails() -> None:
    assert checks("a", "{a}}{") == (validation.CHECK_BRACES_AND_MATH,)
    assert message("a", "{a}}{", validation.CHECK_BRACES_AND_MATH) == "{ } unbalanced at character 3"


def test_an_unclosed_brace_at_the_end_fails() -> None:
    assert message("a", "{a", validation.CHECK_BRACES_AND_MATH) == "{ } unbalanced at character 0"


def test_an_unbalanced_source_does_not_force_a_failure() -> None:
    assert validation.check("{a", "{甲").ok


def test_an_odd_number_of_dollars_fails() -> None:
    assert (
        message("a $x$ b", "甲 $x$ 乙 $", validation.CHECK_BRACES_AND_MATH)
        == "$ count in translation is 3, an odd number, so they cannot pair up"
    )


def test_fewer_dollars_than_the_source_fails() -> None:
    assert (
        message("$x$ and $y$", "$x$ 与 y", validation.CHECK_BRACES_AND_MATH)
        == "$ count differs: 4 in source, 2 in translation"
    )


def test_dollar_difference_is_reported_in_the_differing_paragraph() -> None:
    assert located("$x$\n\n$y$ and $z$", "$x$\n\n$y$ 与 z") == [
        (3, validation.CHECK_BRACES_AND_MATH, "$ count differs: 4 in source, 2 in translation")
    ]


def test_an_unescaped_percent_beyond_the_source_fails() -> None:
    assert checks("我们取 50 个样本。", "我们取 50% 个样本。") == (validation.CHECK_BRACES_AND_MATH,)
    assert "%" in message("我们取 50 个样本。", "我们取 50% 个样本。", validation.CHECK_BRACES_AND_MATH)


def test_an_escaped_percent_stays_in_the_control_sequence_check() -> None:
    assert validation.check("取 50\\% 的样本", "取 50\\% 的样本").ok
    assert checks("取 50 的样本", "取 50\\% 的样本") == (validation.CHECK_CONTROL_SEQUENCES,)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("⟦BLK-3⟧\n\n\\maketitle\n\n\\includegraphics[width=\\linewidth]{f}\n\n   ", 0),
        ("\\section{Training and evaluation of the large models in this work}", 0),
        ("We train the model on a large corpus of text.\n\nShort one.", 1),
    ],
)
def test_paragraphs_to_translate_counts_paragraphs_with_enough_english_words(text: str, expected: int) -> None:
    assert validation.paragraphs_to_translate(text) == expected


EN = """\
\\section{Training Setup}
In recent years, large language models have been undergoing rapid iteration~\\citep{gpt4o}.

⟦BLK-12⟧
⟦CAP-3⟧ Overview of the pipeline that we use for all of the experiments.
$F$ denotes a forward chunk and $B$ denotes a backward chunk of the model.

\\paragraph{Optimizer} We use Adafactor with a dropout rate of $0.1$ for the whole run.
"""

ZH = """\
\\section{Training Setup}
近年来，大语言模型快速迭代~\\citep{gpt4o}。

⟦BLK-12⟧
⟦CAP-3⟧ 全部实验所用流程的概览。
$F$ 表示模型的一个前向块，$B$ 表示一个反向块。

\\paragraph{优化器} 整个训练中我们使用 Adafactor，dropout 率为 $0.1$。
"""


def categories(report: validation.Report) -> list[validation.Category]:
    return [problem.category for problem in report.problems]


def test_a_faithful_translation_passes_every_check() -> None:
    report = validation.check(EN, ZH)
    assert report.ok
    assert report.problems == ()
    assert (report.paragraphs_total, report.paragraphs_translated) == (3, 3)


def test_a_different_paragraph_count_is_the_only_problem_reported() -> None:
    translation = ZH.replace("\n\n⟦BLK-12⟧", "\n⟦BLK-12⟧").replace("$B$", "B")
    report = validation.check(EN, translation)
    assert categories(report) == [validation.Category.STRUCTURE]
    assert "paragraph counts differ: 3 in the source, 2 in the translation" in report.problems[0].message
    assert (report.paragraphs_total, report.paragraphs_translated) == (3, 0)


def test_a_structural_difference_is_reported_for_its_paragraph_only() -> None:
    report = validation.check(EN, ZH.replace("⟦BLK-12⟧", "图"))
    assert categories(report) == [validation.Category.STRUCTURE]
    assert report.problems[0].message.startswith("paragraph starting at line 4 of the translation, placeholders")


@pytest.mark.parametrize("boundary", ["\n \n", "\n\t \n", "\n\n \n", "\n \n\n"])
def test_a_line_of_only_whitespace_is_a_paragraph_boundary(boundary: str) -> None:
    source = EN.replace("\n\n⟦BLK-12⟧", f"{boundary}⟦BLK-12⟧")
    report = validation.check(source, ZH)
    assert report.ok
    assert len(validation.paragraphs(source)) == len(validation.paragraphs(ZH)) == 3


def caption_line_problems(translation: str) -> list[str]:
    return [
        problem.message
        for problem in validation.check(EN, translation).problems
        if validation.CHECK_CAPTION_LINES in problem.message
    ]


def test_a_caption_line_that_starts_with_its_token_passes() -> None:
    assert caption_line_problems(ZH) == []


def test_text_before_a_caption_token_is_a_structure_problem() -> None:
    translation = ZH.replace("⟦CAP-3⟧ 全部", "图中 ⟦CAP-3⟧ 全部")
    report = validation.check(EN, translation)
    assert validation.Category.STRUCTURE in categories(report)
    assert caption_line_problems(translation) == [
        "line 5 of the translation, caption_lines: ⟦CAP-3⟧ must start its line; compile replaces the whole line "
        "with the caption, so the text before it would be lost"
    ]


def test_a_placeholder_moved_onto_a_caption_line_is_a_structure_problem() -> None:
    translation = ZH.replace("⟦BLK-12⟧\n⟦CAP-3⟧ 全部实验所用流程的概览。", "⟦CAP-3⟧ 全部实验所用流程的概览。⟦BLK-12⟧")
    messages = [message for message in caption_line_problems(translation) if "also holds" in message]
    assert messages == [
        "line 4 of the translation, caption_lines: the line of ⟦CAP-3⟧ also holds ⟦BLK-12⟧; "
        "a caption line may hold no placeholder other than those on its source line"
    ]


def test_a_placeholder_of_the_source_caption_line_may_stay_on_it() -> None:
    source = EN.replace("⟦BLK-12⟧\n⟦CAP-3⟧", "⟦CAP-3⟧ ⟦BLK-12⟧")
    translation = ZH.replace("⟦BLK-12⟧\n⟦CAP-3⟧ 全部实验所用流程的概览。", "⟦CAP-3⟧ 全部实验所用流程的概览。⟦BLK-12⟧")
    assert caption_line_problems_of(source, translation) == []


def caption_line_problems_of(source: str, translation: str) -> list[str]:
    return [
        problem.message
        for problem in validation.check(source, translation).problems
        if validation.CHECK_CAPTION_LINES in problem.message
    ]


CAPTION_PARAGRAPHS: list[tuple[str, str, str]] = [
    (
        "⟦BLK-1⟧\n⟦CAP-0⟧ Overview of the whole training pipeline.\n\n⟦BLK-3⟧",
        "⟦BLK-1⟧\n⟦CAP-0⟧ 整个训练流程的概览。\n\n⟦BLK-3⟧",
        "⟦BLK-1⟧\n⟦CAP-0⟧ 整个训练流程\n的概览。\n\n⟦BLK-3⟧",
    ),
    (
        "⟦BLK-1⟧\n⟦CAP-0⟧ Overview of the whole training pipeline.\n⟦BLK-2⟧\nWe train the model on a large corpus of text.",
        "⟦BLK-1⟧\n⟦CAP-0⟧ 整个训练流程的概览。\n⟦BLK-2⟧\n我们在大规模文本语料上训练模型。",
        "⟦BLK-1⟧\n⟦CAP-0⟧ 整个训练流程\n的概览。\n⟦BLK-2⟧\n我们在大规模文本语料上训练模型。",
    ),
    (
        "⟦BLK-1⟧\n⟦CAP-0⟧ Overview of the whole training pipeline.\nWe train the model on a large corpus of text.",
        "⟦BLK-1⟧\n⟦CAP-0⟧ 整个训练流程的概览。\n我们在大规模文本语料上训练模型。",
        "⟦BLK-1⟧\n⟦CAP-0⟧ 整个训练流程\n的概览。\n我们在大规模文本语料上训练模型。",
    ),
    (
        "⟦BLK-1⟧\n⟦CAP-0⟧ Overview of the whole training pipeline.\n⟦BLK-2⟧\n⟦CAP-1⟧ Results on the held out benchmarks.",
        "⟦BLK-1⟧\n⟦CAP-0⟧ 整个训练流程的概览。\n⟦BLK-2⟧\n⟦CAP-1⟧ 在留出基准上的结果。",
        "⟦BLK-1⟧\n⟦CAP-0⟧ 整个训练流程的概览。\n⟦BLK-2⟧\n⟦CAP-1⟧ 在留出基准\n上的结果。",
    ),
]


def first_paragraph_lines(text: str) -> int:
    return text.split("\n\n")[0].count("\n") + 1


@pytest.mark.parametrize(("source", "correct", "split"), CAPTION_PARAGRAPHS)
def test_a_caption_split_over_two_lines_is_a_structure_problem(source: str, correct: str, split: str) -> None:
    assert validation.check(source, correct).ok
    report = validation.check(source, split)
    assert [problem.category for problem in report.problems] == [validation.Category.STRUCTURE]
    message = report.problems[0].message
    assert message.startswith("paragraph starting at line 1 of the translation, caption_lines: ")
    expected = f"which has {first_paragraph_lines(source)} lines, the translation has {first_paragraph_lines(split)}"
    assert message.endswith(expected)


def test_line_counts_of_paragraphs_without_captions_are_free() -> None:
    source = "We train the model on a large corpus of text.\nThen we evaluate it on many benchmarks."
    assert validation.check(source, "我们在大规模文本语料上训练模型，然后在许多基准上评测。").ok


def test_the_paragraph_line_counts_lines_of_the_translation() -> None:
    translation = ZH.replace("$F$ 表示", "$G$ 表示").replace("\n\n⟦BLK-12⟧", "\n\n\n⟦BLK-12⟧")
    report = validation.check(EN, translation)
    assert categories(report) == [validation.Category.FORMULA]
    assert "paragraph starting at line 5 of the translation" in report.problems[0].message


def test_a_changed_inline_formula_is_reported() -> None:
    report = validation.check(EN, ZH.replace("$0.1$", "$0.2$"))
    assert categories(report) == [validation.Category.FORMULA]
    assert "['$0.1$']" in report.problems[0].message
    assert "['$0.2$']" in report.problems[0].message


def test_inline_formulas_may_change_order() -> None:
    translation = ZH.replace(
        "$F$ 表示模型的一个前向块，$B$ 表示一个反向块。", "$B$ 表示一个反向块，$F$ 表示模型的一个前向块。"
    )
    assert validation.check(EN, translation).ok


@pytest.mark.parametrize(
    "heading",
    ["\\section{Training Setup}", "\\subsection*{Training Setup}", "\\subsubsection[Short]{Training Setup}"],
)
def test_a_translated_heading_is_reported(heading: str) -> None:
    source = EN.replace("\\section{Training Setup}", heading)
    assert validation.check(source, ZH.replace("\\section{Training Setup}", heading)).ok
    translated = heading.replace("Training Setup", "训练设置")
    report = validation.check(source, ZH.replace("\\section{Training Setup}", translated))
    assert categories(report) == [validation.Category.HEADING]


def test_an_english_paragraph_is_reported_as_untranslated() -> None:
    translation = ZH.replace(
        "\\paragraph{优化器} 整个训练中我们使用 Adafactor，dropout 率为 $0.1$。", EN.splitlines()[-1]
    )
    report = validation.check(EN, translation)
    assert categories(report) == [validation.Category.UNTRANSLATED]
    assert report.problems[0].message.startswith("paragraph starting at line 8 of the translation is still English")
    assert (report.paragraphs_total, report.paragraphs_translated) == (3, 2)


ENGLISH_LAST = EN.splitlines()[-1]

KEEP_REASON = "模型的原始输出，作为实验数据展示"


def kept_last_paragraph(marker: str = f"% tongtu: keep-english: {KEEP_REASON}") -> str:
    return ZH.replace(
        "\\paragraph{优化器} 整个训练中我们使用 Adafactor，dropout 率为 $0.1$。", f"{marker}\n{ENGLISH_LAST}"
    )


def test_a_marked_english_paragraph_is_kept_instead_of_untranslated() -> None:
    report = validation.check(EN, kept_last_paragraph())
    assert report.ok
    assert report.kept == (
        validation.KeptParagraph(
            line=8, preview=ENGLISH_LAST[: validation.PARAGRAPH_PREVIEW_LENGTH], reason=KEEP_REASON
        ),
    )
    assert (report.paragraphs_total, report.paragraphs_translated) == (3, 2)


def test_the_marker_may_sit_inside_the_paragraph_with_leading_whitespace() -> None:
    translation = ZH.replace(
        "\\paragraph{优化器} 整个训练中我们使用 Adafactor，dropout 率为 $0.1$。",
        f"\\paragraph{{Optimizer}} We use Adafactor\n  % tongtu: keep-english: {KEEP_REASON}\n"
        "with a dropout rate of $0.1$ for the whole run.",
    )
    report = validation.check(EN, translation)
    assert report.ok
    assert [(kept.line, kept.reason) for kept in report.kept] == [(8, KEEP_REASON)]
    assert "%" not in report.kept[0].preview


def test_a_marker_without_a_reason_is_a_structure_problem() -> None:
    report = validation.check(EN, kept_last_paragraph("% tongtu: keep-english:   "))
    assert categories(report) == [validation.Category.STRUCTURE]
    assert report.problems[0].message == (
        "paragraph starting at line 8 of the translation has a keep-english marker without a reason"
    )
    assert report.kept == ()


def test_the_marker_line_does_not_count_as_an_extra_percent() -> None:
    assert "%" not in EN
    assert located(EN, kept_last_paragraph()) == []


def test_a_marked_paragraph_still_gets_the_formula_and_heading_checks() -> None:
    formula = validation.check(EN, kept_last_paragraph().replace("$0.1$", "$0.2$"))
    assert categories(formula) == [validation.Category.FORMULA]
    heading = kept_last_paragraph().replace("\\paragraph{Optimizer}", "\\section{Optimizer}")
    assert validation.Category.HEADING in categories(validation.check(EN, heading))


def test_a_marked_paragraph_that_needs_no_translation_is_accepted_and_kept() -> None:
    names = "Alice Zhang, Bob Li, Carol Wang"
    marked = f"% tongtu: keep-english: 作者姓名\n{names}"
    report = validation.check(f"{names}\n\n{EN}", f"{marked}\n\n{ZH}")
    assert report.ok
    assert [(kept.line, kept.preview, kept.reason) for kept in report.kept] == [(1, names, "作者姓名")]
    assert (report.paragraphs_total, report.paragraphs_translated) == (3, 3)


def test_a_paragraph_of_names_needs_no_translation() -> None:
    names = "Alice Zhang, Bob Li, Carol Wang, Dave Chen\nTsinghua University, Peking University, DeepSeek-AI"
    report = validation.check(f"{names}\n\n{EN}", f"{names}\n\n{ZH}")
    assert report.ok
    assert report.paragraphs_total == 3


@pytest.mark.parametrize(
    "paragraph",
    [
        "\\url{https://github.com/example/the-large-language-model-training-code}",
        "\\cref{fig:the-overview-of-our-large-language-model-pipeline}",
        "\\citealp{smith-and-jones-large-language-model-scaling-laws}",
    ],
)
def test_a_paragraph_of_only_a_reference_command_is_not_untranslated(paragraph: str) -> None:
    report = validation.check(f"{paragraph}\n\n{EN}", f"{paragraph}\n\n{ZH}")
    assert report.ok
    assert report.paragraphs_total == 3


def test_heading_words_do_not_count_as_untranslated_text() -> None:
    heading = "\\section{Training and evaluation of the large models in this work}"
    report = validation.check(f"{heading}\n\nWe use it.\n", f"{heading}\n\n我们使用它。\n")
    assert report.ok
    assert report.paragraphs_total == 0


APPENDIX_DEFINITIONS = r"""\clearpage
\appendix
\definecolor{roleR}{HTML}{2A78D6}\definecolor{roleD}{HTML}{EB6834}\definecolor{roleQ}{HTML}{1BAF7A}
\definecolor{roleA}{HTML}{EDA100}\definecolor{roleE}{HTML}{C9C8C2}
\definecolor{roleRt}{HTML}{1F5FB0}\definecolor{roleDt}{HTML}{B4420F}\definecolor{roleQt}{HTML}{0E7A54}
\definecolor{roleAt}{HTML}{8A5A00}\definecolor{roleEt}{HTML}{5F5E5A}
\newcommand{\chip}[3]{{\setlength{\fboxsep}{1.2pt}\colorbox{#1}{\textcolor{#2}{\textsf{\bfseries\scriptsize #3}}}}}
\newcommand{\spR}[1]{\chip{roleR}{white}{R}\,\textcolor{roleRt}{#1}}
\newcommand{\spD}[1]{\chip{roleD}{white}{D}\,\textcolor{roleDt}{#1}}
\newcommand{\spQ}[1]{\chip{roleQ}{black}{Q}\,\textcolor{roleQt}{#1}}
\newcommand{\spA}[1]{\chip{roleA}{black}{A}\,\textcolor{roleAt}{#1}}
\newcommand{\spE}[1]{\chip{roleE}{black}{env}\,\textcolor{roleEt}{\emph{#1}}}"""


def test_a_paragraph_of_macro_definitions_needs_no_translation() -> None:
    report = validation.check(APPENDIX_DEFINITIONS, APPENDIX_DEFINITIONS)
    assert report.ok
    assert report.paragraphs_total == 0


APPENDIX_CONTENTS = r"""\appendix
\crefalias{section}{appendix}
\crefalias{subsection}{subappendix}
\clearpage
\section*{Appendix Table of Contents}
\startcontents[sections]
\printcontents[sections]{l}{1}{\setcounter{tocdepth}{2}}"""


def test_a_paragraph_of_cleveref_and_titletoc_settings_needs_no_translation() -> None:
    report = validation.check(APPENDIX_CONTENTS, APPENDIX_CONTENTS)
    assert report.ok
    assert report.paragraphs_total == 0


@pytest.mark.parametrize(
    "definition",
    [
        r"\def\foo#1{\textbf{some words here} and more words there}",
        r"\gdef\foo[#1]#2{some words here and more words there}",
        r"\newenvironment{boxed}[1][plain]{\begin{center}some words here}{and more words there\end{center}}",
        r"\renewenvironment*{boxed}{some {nested words {here}} and}{more words there}",
        r"\providecommand*\foo[2][x]{some {nested {words}} here \{ and more words there}",
        r"\DeclareRobustCommand{\foo}{some words here and more words there}",
        r"\DeclareMathOperator*{\argmax}{some words here and more words there}",
        r"\newcolumntype{L}[1]{>{\raggedright some words here}p{#1} more words there}",
    ],
)
def test_macro_definitions_do_not_count_as_english_words(definition: str) -> None:
    assert validation.paragraphs_to_translate(definition) == 0


def test_english_text_next_to_a_macro_definition_is_still_untranslated() -> None:
    paragraph = (
        r"\newcommand{\foo}[1]{\textbf{some words here}} We train the model on a large corpus of text for many steps."
    )
    report = validation.check(f"{paragraph}\n\n{EN}", f"{paragraph}\n\n{ZH}")
    assert categories(report) == [validation.Category.UNTRANSLATED]
    assert (report.paragraphs_total, report.paragraphs_translated) == (4, 3)


def test_a_def_without_a_body_on_its_line_keeps_the_next_line_counted() -> None:
    paragraph = "\\def\\foo\nWe train the model on a large corpus of text for \\textbf{many} steps."
    report = validation.check(f"{paragraph}\n\n{EN}", f"{paragraph}\n\n{ZH}")
    assert categories(report) == [validation.Category.UNTRANSLATED]
    assert (report.paragraphs_total, report.paragraphs_translated) == (4, 3)


def test_a_term_must_appear_in_its_translation() -> None:
    terms = {"large language model": "大语言模型", "forward chunk": "前向块"}
    assert validation.check(EN, ZH, terms).ok
    report = validation.check(EN, ZH.replace("大语言模型", "大模型"), terms)
    assert categories(report) == [validation.Category.GLOSSARY]
    assert "'large language model'" in report.problems[0].message
    assert "line 1 of the translation" in report.problems[0].message


def test_glossary_ignores_reference_arguments_headings_and_formulas() -> None:
    terms = {"gpt4o": "某模型", "training setup": "训练设置", "F": "前向"}
    assert validation.check(EN, ZH, terms).ok


def test_glossary_skips_paragraphs_without_chinese() -> None:
    names = "Large Language Model Group, Example University"
    report = validation.check(f"{names}\n\n{EN}", f"{names}\n\n{ZH}", {"large language model": "大语言模型"})
    assert report.ok


def test_a_do_not_translate_word_must_be_kept_with_its_case() -> None:
    assert validation.check(EN, ZH, do_not_translate=["Adafactor"]).ok
    report = validation.check(EN, ZH.replace("Adafactor", "adafactor"), do_not_translate=["Adafactor"])
    assert categories(report) == [validation.Category.GLOSSARY]
    assert "'Adafactor'" in report.problems[0].message


KEPT_IN_COMPOUNDS: list[tuple[str, str, bool]] = [
    ("rank", "We use a low-rank update for every layer of the large model.", False),
    ("rank", "We use a rank-based update for every layer of the large model.", False),
    ("rank", "We use a rank-1 update for every layer of the large model.", False),
    ("rank", "Each PP rank holds two layers of the large model in memory.", True),
    ("RL", "We compare RL-based methods on every one of the benchmarks.", False),
    ("RL", "We compare RL training methods on every one of the benchmarks.", True),
    ("KV-cache", "We compress the KV-cache of every layer of the large model.", True),
]


@pytest.mark.parametrize(("word", "source", "required"), KEPT_IN_COMPOUNDS)
def test_a_do_not_translate_word_inside_a_hyphenated_compound_need_not_be_kept(
    word: str, source: str, required: bool
) -> None:
    report = validation.check(source, "我们对大模型的每一层使用同一种方法。", do_not_translate=[word])
    assert report.ok is not required
    assert categories(report) == ([validation.Category.GLOSSARY] if required else [])


def test_the_translation_side_still_finds_a_kept_word_inside_a_compound() -> None:
    source = "Each PP rank holds two layers of the large model in memory."
    assert validation.check(source, "每个 PP rank-0 进程保存大模型的两层。", do_not_translate=["rank"]).ok


def test_a_term_must_keep_the_do_not_translate_word_it_contains() -> None:
    report = validation.check(EN, ZH, {"mixed RL training": "混合强化学习训练"}, ["RL"])
    assert categories(report) == [validation.Category.GLOSSARY]
    assert "'mixed RL training'" in report.problems[0].message
    assert validation.check(EN, ZH, {"mixed RL training": "混合 RL 训练"}, ["RL"]).ok


@pytest.mark.parametrize(
    ("word", "translation", "kept", "conflict"),
    [
        ("low-rank", "低秩", "rank", False),
        ("low-rank adaptation", "低秩适配", "rank", False),
        ("PP rank", "PP 秩", "rank", True),
        ("mixed RL training", "混合强化学习训练", "RL", True),
    ],
)
def test_a_kept_word_inside_a_hyphenated_compound_of_a_term_need_not_be_kept(
    word: str, translation: str, kept: str, conflict: bool
) -> None:
    conflicts = validation.kept_word_conflicts({word: translation}, [kept])
    assert conflicts == ([(word, translation, kept)] if conflict else [])


@pytest.mark.parametrize(
    ("word", "text", "expected"),
    [
        ("LLM", "Recent LLMs scale well", True),
        ("LLM", "served with vLLM", False),
        ("LLM", "an llm here", False),
        ("large language model", "Large  Language\nModels are", True),
        ("patch", "image patches", True),
        ("token", "tokenizer", False),
    ],
)
def test_hits_matches_whole_words_with_plurals(word: str, text: str, expected: bool) -> None:
    assert validation.hits(word, text) is expected


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("cache", "caches", True),
        ("batch", "batches", True),
        ("loss", "losses", True),
        ("class", "classes", True),
        ("box", "boxes", True),
        ("KV cache", "kv-caches", True),
        ("Long  Context", "long-contexts", True),
        ("plan", "plane", False),
        ("cache", "cached", False),
        ("batches", "batch", True),
    ],
)
def test_same_term_ignores_case_hyphens_and_a_plural_ending(first: str, second: str, expected: bool) -> None:
    assert validation.same_term(first, second) is expected
    assert validation.same_term(second, first) is expected


def test_exact_hits_respect_case() -> None:
    assert validation.hits("Adafactor", "the adafactor optimizer") is True
    assert validation.hits("Adafactor", "the adafactor optimizer", exact=True) is False


def write(path: Path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_main_prints_the_count_and_check_passed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert validation.main([write(tmp_path / "en.tex", EN), write(tmp_path / "zh.tex", ZH)]) == 0
    assert capsys.readouterr().out.splitlines() == ["translated paragraphs 3/3", "check passed"]


def test_main_lists_the_paragraphs_kept_in_english(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert validation.main([write(tmp_path / "en.tex", EN), write(tmp_path / "zh.tex", kept_last_paragraph())]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "translated paragraphs 2/3, kept in English 1",
        f"kept in English: paragraph starting at line 8: {KEEP_REASON}",
        "check passed",
    ]


def test_main_lists_each_problem_and_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    translation = ZH.replace("$0.1$", "$0.2$").replace("\\section{Training Setup}", "\\section{训练设置}")
    assert validation.main([write(tmp_path / "en.tex", EN), write(tmp_path / "zh.tex", translation)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "translated paragraphs 3/3"
    assert lines[1].startswith("[heading] ")
    assert lines[2].startswith("[formula] ")
    assert lines[-1] == "2 problems"


def test_main_layers_glossaries_with_the_later_file_winning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = write(tmp_path / "en.tex", EN)
    translation = write(tmp_path / "zh.tex", ZH)
    first = write(tmp_path / "first.json", '{"terms": {"large language models": "巨型模型"}}')
    second = write(tmp_path / "second.json", '{"terms": {"Large Language Model": "大语言模型"}, "style": "x"}')
    assert validation.main([source, translation, "--glossary", first]) == 1
    assert "[glossary] " in capsys.readouterr().out
    assert validation.main([source, translation, "--glossary", first, "--glossary", second]) == 0
    assert validation.main([source, translation, "--glossary", second, "--glossary", first]) == 1


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"terms": ["a"]}',
        '{"terms": {"a": ""}}',
        '{"do_not_translate": [""]}',
        '{"unknown": 1}',
        '{"terms": {"LLM": "x"}, "do_not_translate": ["LLMs"]}',
    ],
)
def test_main_rejects_an_unreadable_glossary(tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str) -> None:
    glossary = write(tmp_path / "g.json", content)
    arguments = [write(tmp_path / "en.tex", EN), write(tmp_path / "zh.tex", ZH), "--glossary", glossary]
    assert validation.main(arguments) == 1
    output = capsys.readouterr().out
    assert glossary in output
    assert "check passed" not in output


def test_main_reports_a_missing_glossary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    arguments = [write(tmp_path / "en.tex", EN), write(tmp_path / "zh.tex", ZH), "--glossary", str(tmp_path / "no")]
    assert validation.main(arguments) == 1
    assert "cannot read glossary" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [[], ["a"], ["a", "b", "--glossary"], ["a", "b", "c"]])
def test_main_with_wrong_arguments_prints_usage(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert validation.main(arguments) == 1
    assert validation.USAGE in capsys.readouterr().out


def test_main_reports_an_unreadable_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert validation.main([str(tmp_path / "absent.tex"), str(tmp_path / "zh.tex")]) == 1
    assert "cannot read file" in capsys.readouterr().out


def session_directory(tmp_path: Path) -> Path:
    scripts = tmp_path / ".skills" / "translate"
    scripts.mkdir(parents=True)
    shutil.copy(REPOSITORY / "skill" / "translate" / "check.py", scripts / "check.py")
    shutil.copy(REPOSITORY / "tongtu" / "validation.py", scripts / "validate.py")
    (tmp_path / "en.tex").write_text(EN, encoding="utf-8")
    (tmp_path / "zh.tex").write_text(ZH, encoding="utf-8")
    return tmp_path


def run_check(session: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-I", "-S", ".skills/translate/check.py"]
    return subprocess.run(command, cwd=session, capture_output=True, text=True, encoding="utf-8", check=False)


def test_the_copied_check_script_runs_without_third_party_packages(tmp_path: Path) -> None:
    session = session_directory(tmp_path)
    result = run_check(session)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "check passed"


def test_the_copied_check_script_uses_both_glossaries(tmp_path: Path) -> None:
    session = session_directory(tmp_path)
    (session / "glossary.json").write_text('{"terms": {"large language model": "巨型模型"}}', encoding="utf-8")
    result = run_check(session)
    assert result.returncode == 1
    assert "[glossary] " in result.stdout
    fixed = '{"terms": {"large language models": "大语言模型"}, "do_not_translate": ["Adafactor"]}'
    (session / "glossary.fixed.json").write_text(fixed, encoding="utf-8")
    result = run_check(session)
    assert result.returncode == 0, result.stdout


ENDS_WITH_PLACEHOLDER = (
    "We train the language model on long sequences of text with a large batch.\n\n⟦BLK-1⟧\n⟦BLK-2⟧",
    "我们用大批量在长文本序列上训练语言模型。\n\n⟦BLK-1⟧\n⟦BLK-2⟧",
)

ENDS_WITH_CAPTION = (
    "We train the language model on long sequences of text with a large batch.\n\n"
    "⟦BLK-1⟧\n⟦CAP-1⟧ Loss curves of the language model on long sequences of text.",
    "我们用大批量在长文本序列上训练语言模型。\n\n⟦BLK-1⟧\n⟦CAP-1⟧ 语言模型在长文本序列上的损失曲线。",
)

ENDS_WITH_A_SPLIT_CAPTION = (
    ENDS_WITH_CAPTION[0],
    "我们用大批量在长文本序列上训练语言模型。\n\n⟦BLK-1⟧\n⟦CAP-1⟧ 语言模型在长文本序列上的\n损失曲线。",
)


@pytest.mark.parametrize(
    ("source", "translation", "problems", "paragraphs"),
    [(*ENDS_WITH_PLACEHOLDER, 0, 1), (*ENDS_WITH_CAPTION, 0, 2), (*ENDS_WITH_A_SPLIT_CAPTION, 1, 2)],
    ids=["placeholder", "caption", "split_caption"],
)
def test_a_final_newline_on_either_side_does_not_change_the_report(
    source: str, translation: str, problems: int, paragraphs: int
) -> None:
    reports = {
        validation.check(source + source_end, translation + translation_end)
        for source_end in ("", "\n")
        for translation_end in ("", "\n")
    }
    assert len(reports) == 1
    report = reports.pop()
    assert len(report.problems) == problems
    assert (report.paragraphs_total, report.paragraphs_translated) == (paragraphs, paragraphs)
