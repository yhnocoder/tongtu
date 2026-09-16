from __future__ import annotations

import pytest

from tongtu import conditionals, masking

ENVIRONMENTS_TABLE = masking.parse_environment_table(masking.ENVIRONMENTS_TABLE_PATH.read_text(encoding="utf-8"))


def strip(text: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    return conditionals.strip_dead_branches(text, ENVIRONMENTS_TABLE, warnings), warnings


SWITCH_TRUE = "\\newif\\ifarxiv\n\\arxivtrue\n"

SWITCH_FALSE = "\\newif\\ifarxiv\n\\arxivfalse\n"

BRANCHES = "\\ifarxiv\nA\n\\else\nB\n\\fi\nC\n"


def test_strip_true_keeps_first_branch() -> None:
    output, warnings = strip(SWITCH_TRUE + BRANCHES)
    assert output == SWITCH_TRUE + "A\nC\n"
    assert warnings == ["constant switch \\ifarxiv is true: removed 1 dead branches, kept 0"]


def test_strip_false_keeps_else_branch() -> None:
    output, warnings = strip(SWITCH_FALSE + BRANCHES)
    assert output == SWITCH_FALSE + "B\nC\n"
    assert warnings == ["constant switch \\ifarxiv is false: removed 1 dead branches, kept 0"]


def test_strip_without_else() -> None:
    output, _ = strip(SWITCH_TRUE + "\\ifarxiv\nA\n\\fi\nC\n")
    assert output == SWITCH_TRUE + "A\nC\n"
    output, _ = strip(SWITCH_FALSE + "\\ifarxiv\nA\n\\fi\nC\n")
    assert output == SWITCH_FALSE + "C\n"


def test_strip_iffalse_literal_removes_whole_block() -> None:
    output, warnings = strip("X\n\\iffalse\nA\n\\fi\nC\n")
    assert output == "X\nC\n"
    assert warnings == ["constant switch \\iffalse is false: removed 1 dead branches, kept 0"]


def test_strip_unassigned_switch_is_false() -> None:
    output, _ = strip("\\newif\\ifarxiv\n" + BRANCHES)
    assert output == "\\newif\\ifarxiv\nB\nC\n"


@pytest.mark.parametrize(
    "preamble",
    [
        "\\newif\\ifarxiv\n\\arxivtrue\n\\arxivfalse\n",
        "\\newif\\ifarxiv\n\\begin{document}\n\\arxivtrue\n",
        "\\newif\\ifarxiv\n\\AtBeginDocument{\\arxivtrue}\n",
    ],
)
def test_strip_leaves_non_constant_switches(preamble: str) -> None:
    output, warnings = strip(preamble + BRANCHES)
    assert output == preamble + BRANCHES
    assert warnings == []


def test_strip_leaves_switch_assigned_inside_another_conditional() -> None:
    source = "\\newif\\ifarxiv \\ifdefined\\ARXIV\\arxivtrue\\fi \\ifarxiv A\\else B\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == []


def test_strip_skips_known_non_conditionals() -> None:
    output, warnings = strip(SWITCH_TRUE + "\\ifarxiv $a \\iff b$ \\else B \\fi")
    assert output == SWITCH_TRUE + "$a \\iff b$ "
    assert warnings == ["constant switch \\ifarxiv is true: removed 1 dead branches, kept 0"]


def test_strip_keeps_at_names_whole() -> None:
    source = "\\makeatletter\\newif\\ifhl@active\\newcommand\\on{\\global\\hl@activetrue}\\ifhl@active A\\else B\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == []


def test_strip_abandons_on_unknown_at_conditional() -> None:
    source = "\\newif\\ifarxiv\\arxivtrue\\ifarxiv\\ifhl@active X\\fi\\else B\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == [
        "\\ifarxiv at line 1 is kept: \\ifhl@active at line 1 is not a known conditional",
        "constant switch \\ifarxiv is true: removed 0 dead branches, kept 1",
    ]


def test_strip_global_assignment() -> None:
    output, _ = strip("\\newif\\ifarxiv\n\\global\\arxivtrue\n" + BRANCHES)
    assert output == "\\newif\\ifarxiv\n\\global\\arxivtrue\nA\nC\n"


def test_strip_unless_negates() -> None:
    output, _ = strip(SWITCH_TRUE + "\\unless\\ifarxiv\nA\n\\else\nB\n\\fi\nC\n")
    assert output == SWITCH_TRUE + "B\nC\n"
    output, _ = strip("\\unless \\iftrue A\\else B\\fi")
    assert output == "B"


def test_strip_reads_at_prefixed_names_whole() -> None:
    output, warnings = strip("\\makeatletter\\newif\\if@foo\\@footrue \\if@foo A\\else B\\fi")
    assert output == "\\makeatletter\\newif\\if@foo\\@footrue A"
    assert warnings == ["constant switch \\if@foo is true: removed 1 dead branches, kept 0"]
    output, _ = strip("\\makeatletter\\newif\\if@foo\\@footrue\\if@foo A\\else B\\fi")
    assert output == "\\makeatletter\\newif\\if@foo\\@footrue A"


def test_strip_leaves_switch_assigned_inside_begingroup() -> None:
    source = "\\newif\\ifarxiv\\begingroup\\arxivtrue\\endgroup\\ifarxiv A\\else B\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == []


def test_strip_balances_nested_primitive() -> None:
    body = "\\ifarxiv\n\\ifx\\a\\b X\\else Y\\fi\n\\else\nB\n\\fi\nC\n"
    output, _ = strip(SWITCH_TRUE + body)
    assert output == SWITCH_TRUE + "\\ifx\\a\\b X\\else Y\\fi\nC\n"


def test_strip_abandons_unknown_conditional_macro() -> None:
    body = "\\ifarxiv\n\\iftodonotes{x}\n\\else\nB\n\\fi\n" + BRANCHES
    output, warnings = strip(SWITCH_TRUE + body)
    assert output == SWITCH_TRUE + "\\ifarxiv\n\\iftodonotes{x}\n\\else\nB\n\\fi\nA\nC\n"
    assert warnings == [
        "\\ifarxiv at line 3 is kept: \\iftodonotes at line 4 is not a known conditional",
        "constant switch \\ifarxiv is true: removed 1 dead branches, kept 1",
    ]


def test_strip_does_not_confuse_fi_with_figref() -> None:
    output, warnings = strip(SWITCH_TRUE + "\\ifarxiv\n\\figref{a}\n\\else\nB\n\\fi\nC\n")
    assert output == SWITCH_TRUE + "\\figref{a}\nC\n"
    assert warnings == ["constant switch \\ifarxiv is true: removed 1 dead branches, kept 0"]


def test_strip_does_not_confuse_ifx_with_ifxyz() -> None:
    body = "\\ifarxiv\n\\ifxyz X\\fi\n\\else\nB\n\\fi\nC\n"
    output, warnings = strip(SWITCH_TRUE + body)
    assert output == SWITCH_TRUE + body
    assert warnings == [
        "\\ifarxiv at line 3 is kept: \\ifxyz at line 4 is not a known conditional",
        "constant switch \\ifarxiv is true: removed 0 dead branches, kept 1",
    ]


def test_strip_inside_macro_body() -> None:
    output, _ = strip(SWITCH_TRUE + "\\newcommand{\\confonly}[1]{\\ifarxiv\\else#1\\fi}\n")
    assert output == SWITCH_TRUE + "\\newcommand{\\confonly}[1]{}\n"


def test_strip_does_not_add_blank_lines() -> None:
    output, _ = strip(SWITCH_TRUE + "\\section{X}\n\\ifarxiv\n\\begin{table}\n\\end{table}\n\\fi\n")
    assert output == SWITCH_TRUE + "\\section{X}\n\\begin{table}\n\\end{table}\n"


@pytest.mark.parametrize(("preamble", "star"), [(SWITCH_TRUE, ""), (SWITCH_FALSE, "*")])
def test_strip_keeps_environments_balanced(preamble: str, star: str) -> None:
    body = (
        "\\ifarxiv \\begin{table} \\else \\begin{table*} \\fi body \\ifarxiv \\end{table} \\else \\end{table*} \\fi\n"
    )
    output, _ = strip(preamble + body)
    assert output == preamble + f"\\begin{{table{star}}} body \\end{{table{star}}} "


def test_strip_skips_comments_and_verb() -> None:
    body = "\\verb|\\ifarxiv| %\n\\ifarxiv\nA\\verb+\\fi+\n\\else\nB\n\\fi\nC\n"
    output, _ = strip(SWITCH_TRUE + body)
    assert output == SWITCH_TRUE + "\\verb|\\ifarxiv| %\nA\\verb+\\fi+\nC\n"


@pytest.mark.parametrize(
    "source",
    [
        "\\begin{verbatim}\\iffalse x\\fi\\end{verbatim}\n",
        SWITCH_TRUE + "\\begin{lstlisting}\\ifarxiv A\\else B\\fi\\end{lstlisting}\n",
        SWITCH_TRUE + "\\begin{lstlisting*}\\ifarxiv A\\else B\\fi\\end{lstlisting*}\n",
    ],
)
def test_strip_skips_code_environments(source: str) -> None:
    output, warnings = strip(source)
    assert output == source
    assert warnings == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("\\foo\\iftrue world\\fi", "\\foo world"),
        ("ab\\iftrue cd\\fi", "abcd"),
        ("\\foo\\iftrue{x}\\fi", "\\foo{x}"),
        ("\\foo@bar\\iftrue world\\fi", "\\foo@bar world"),
        ("\\\\foo\\iftrue bar\\fi", "\\\\foobar"),
        ("\\foo\\iftrue @bar\\fi", "\\foo @bar"),
    ],
)
def test_strip_separates_control_word_from_following_letters(source: str, expected: str) -> None:
    output, _ = strip(source)
    assert output == expected


def test_strip_before_assignment_is_false() -> None:
    output, warnings = strip("\\newif\\ifarxiv \\ifarxiv A\\else B\\fi \\arxivtrue \\ifarxiv C\\else D\\fi")
    assert output == "\\newif\\ifarxiv B\\arxivtrue C"
    assert warnings == ["constant switch \\ifarxiv is true: removed 2 dead branches, kept 0"]


def test_strip_keeps_macro_body_written_before_assignment() -> None:
    source = "\\newif\\ifarxiv \\newcommand{\\chosen}{\\ifarxiv T\\else F\\fi} \\arxivtrue \\chosen"
    output, warnings = strip(source)
    assert output == source
    assert warnings == [
        "\\ifarxiv at line 1 is kept: precedes assignment inside a group",
        "constant switch \\ifarxiv is true: removed 0 dead branches, kept 1",
    ]


def test_strip_leaves_switch_assigned_by_let() -> None:
    source = "\\newif\\ifarxiv\\let\\ifarxiv\\iftrue \\ifarxiv A\\else B\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == [
        "\\iftrue at line 1 is kept: operand of \\let",
        "constant switch \\iftrue is true: removed 0 dead branches, kept 1",
    ]


@pytest.mark.parametrize(
    "source",
    [
        "\\newif\\ifarxiv\\ifpdf\\arxivtrue\\fi \\ifarxiv A\\else B\\fi",
        "\\newif\\ifarxiv\\let\\enable\\arxivtrue \\ifarxiv A\\else B\\fi",
        "\\newif\\ifarxiv\\arxivtrue\\newif\\ifarxiv \\ifarxiv A\\else B\\fi",
        "\\newif\\ifarxiv\\bgroup\\arxivtrue\\egroup \\ifarxiv A\\else B\\fi",
        "\\newif\\ifarxiv\\begin{setup}\\arxivtrue\\end{setup} \\ifarxiv A\\else B\\fi",
    ],
)
def test_strip_leaves_switch_whose_assignment_is_not_a_top_level_execution(source: str) -> None:
    output, warnings = strip(source)
    assert output == source
    assert warnings == []


def test_strip_leaves_literal_redefined_by_let() -> None:
    source = "\\let\\iftrue\\iffalse \\iftrue A\\else B\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == [
        "\\iffalse at line 1 is kept: operand of \\let",
        "constant switch \\iffalse is false: removed 0 dead branches, kept 1",
    ]


def test_strip_let_with_equals_sign_takes_its_operand() -> None:
    output, warnings = strip("\\let\\check=\\iftrue \\iftrue A\\fi")
    assert output == "\\let\\check=\\iftrue A"
    assert warnings == [
        "\\iftrue at line 1 is kept: operand of \\let",
        "constant switch \\iftrue is true: removed 1 dead branches, kept 1",
    ]


def test_strip_operands_do_not_pair() -> None:
    output, _ = strip("\\iftrue\\ifx\\fi\\relax A\\else B\\fi\\else C\\fi")
    assert output == "\\ifx\\fi\\relax A\\else B\\fi"
    output, warnings = strip("\\newif\\ifarxiv\\arxivtrue \\ifarxiv\\let\\ifpreprint\\iftrue\\fi")
    assert output == "\\newif\\ifarxiv\\arxivtrue \\let\\ifpreprint\\iftrue"
    assert warnings == [
        "\\iftrue at line 1 is kept: operand of \\let",
        "constant switch \\ifarxiv is true: removed 1 dead branches, kept 0",
        "constant switch \\iftrue is true: removed 0 dead branches, kept 1",
    ]


def test_strip_unless_as_operand_does_not_negate() -> None:
    output, _ = strip(SWITCH_TRUE + "\\ifdefined\\unless\\ifarxiv A\\else B\\fi\\fi")
    assert output == SWITCH_TRUE + "\\ifdefined\\unless A\\fi"


def test_strip_keeps_switch_after_expandafter() -> None:
    source = "\\newif\\ifarxiv\\arxivtrue \\expandafter\\ifarxiv A\\else B\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == [
        "\\ifarxiv at line 1 is kept: follows \\expandafter",
        "constant switch \\ifarxiv is true: removed 0 dead branches, kept 1",
    ]


def test_strip_keeps_operand_of_ifdefined() -> None:
    source = "\\newif\\ifarxiv\\arxivtrue \\ifdefined\\ifarxiv X\\else Y\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == [
        "\\ifarxiv at line 1 is kept: operand of \\ifdefined",
        "constant switch \\ifarxiv is true: removed 0 dead branches, kept 1",
    ]


def test_strip_keeps_operand_separated_by_comment() -> None:
    source = "\\newif\\ifarxiv\\arxivtrue \\ifdefined%\n\\ifarxiv X\\else Y\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == [
        "\\ifarxiv at line 2 is kept: operand of \\ifdefined",
        "constant switch \\ifarxiv is true: removed 0 dead branches, kept 1",
    ]


def test_strip_keeps_operand_of_ifx_after_character_operand() -> None:
    source = "\\newif\\ifarxiv\\arxivtrue \\ifx a\\ifarxiv X\\else Y\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == [
        "\\ifarxiv at line 1 is kept: operand of \\ifx",
        "constant switch \\ifarxiv is true: removed 0 dead branches, kept 1",
    ]


def test_strip_keeps_second_operand_of_ifx() -> None:
    source = "\\newif\\ifarxiv\\arxivtrue \\ifx\\ifarxiv\\iftrue X\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == [
        "\\ifarxiv at line 1 is kept: operand of \\ifx",
        "\\iftrue at line 1 is kept: operand of \\ifx",
        "constant switch \\ifarxiv is true: removed 0 dead branches, kept 1",
        "constant switch \\iftrue is true: removed 0 dead branches, kept 1",
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a\\iftrue\n  b\\fi", "ab"),
        ("a\\iftrue\n\nb\\fi", "a%\n\nb"),
        ("\\iftrue a\\fi\n\nb", "a%\n\nb"),
    ],
)
def test_strip_skips_space_like_tex_but_keeps_blank_lines(source: str, expected: str) -> None:
    output, _ = strip(source)
    assert output == expected


def test_strip_pairs_dead_branch_by_name() -> None:
    output, _ = strip("\\iffalse\\let\\check\\iftrue X\\fi\\fi Y")
    assert output == "Y"


def test_strip_consumed_command_does_not_consume() -> None:
    output, _ = strip("\\iftrue\\let\\check\\ifx a\\iffalse A\\else B\\fi\\else C\\fi")
    assert output == "\\let\\check\\ifx aB"


def test_strip_space_after_character_is_the_second_operand() -> None:
    output, warnings = strip("\\ifx a \\iffalse A\\else B\\fi")
    assert output == "\\ifx a B"
    assert warnings == ["constant switch \\iffalse is false: removed 1 dead branches, kept 0"]
    source = "\\ifx a\\iffalse A\\else B\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == [
        "\\iffalse at line 1 is kept: operand of \\ifx",
        "constant switch \\iffalse is false: removed 0 dead branches, kept 1",
    ]


def test_strip_ifcat_expands_its_operand() -> None:
    output, _ = strip("\\ifcat\\iftrue a1\\else bb\\fi")
    assert output == "\\ifcat a1"


def test_strip_newif_as_operand_does_not_declare() -> None:
    source = "\\ifdefined\\newif\\ifXeTeX A\\else B\\fi\\fi"
    output, warnings = strip(source)
    assert output == source
    assert warnings == []


def test_strip_leaves_switch_redefined_by_def() -> None:
    source = "\\newif\\ifarxiv\\arxivtrue\\def\\ifarxiv{\\iffalse} \\ifarxiv A\\else B\\fi"
    output, _ = strip(source)
    assert output == source


def test_strip_separates_single_at_from_following_letters() -> None:
    output, _ = strip("\\@\\iftrue foo\\fi")
    assert output == "\\@ foo"
