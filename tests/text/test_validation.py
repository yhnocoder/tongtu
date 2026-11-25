from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tongtu import validation

REPOSITORY = Path(__file__).resolve().parents[2]


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


def test_a_structural_difference_is_reported_for_the_whole_text_and_for_its_paragraph() -> None:
    report = validation.check(EN, ZH.replace("⟦BLK-12⟧\n", ""))
    assert set(categories(report)) == {validation.Category.STRUCTURE}
    messages = [problem.message for problem in report.problems]
    assert messages[0].startswith("whole text, placeholders:")
    assert any(
        message.startswith("paragraph starting at line 4 of the translation, placeholders") for message in messages
    )


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


def test_a_paragraph_of_names_needs_no_translation() -> None:
    names = "Alice Zhang, Bob Li, Carol Wang, Dave Chen\nTsinghua University, Peking University, DeepSeek-AI"
    report = validation.check(f"{names}\n\n{EN}", f"{names}\n\n{ZH}")
    assert report.ok
    assert report.paragraphs_total == 3


def test_heading_words_do_not_count_as_untranslated_text() -> None:
    heading = "\\section{Training and evaluation of the large models in this work}"
    report = validation.check(f"{heading}\n\nWe use it.\n", f"{heading}\n\n我们使用它。\n")
    assert report.ok
    assert report.paragraphs_total == 0


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


def test_a_term_must_keep_the_do_not_translate_word_it_contains() -> None:
    report = validation.check(EN, ZH, {"mixed RL training": "混合强化学习训练"}, ["RL"])
    assert categories(report) == [validation.Category.GLOSSARY]
    assert "'mixed RL training'" in report.problems[0].message
    assert validation.check(EN, ZH, {"mixed RL training": "混合 RL 训练"}, ["RL"]).ok


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


def test_exact_hits_respect_case() -> None:
    assert validation.hits("Adafactor", "the adafactor optimizer") is True
    assert validation.hits("Adafactor", "the adafactor optimizer", exact=True) is False


def write(path: Path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_main_prints_the_count_and_check_passed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert validation.main([write(tmp_path / "en.tex", EN), write(tmp_path / "zh.tex", ZH)]) == 0
    assert capsys.readouterr().out.splitlines() == ["translated paragraphs 3/3", "check passed"]


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
