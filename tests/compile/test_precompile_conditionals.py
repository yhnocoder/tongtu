from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tongtu import conditionals, masking
from tongtu.stages import precompile

pytestmark = pytest.mark.compile

ENVIRONMENTS_TABLE = masking.parse_environment_table(masking.ENVIRONMENTS_TABLE_PATH.read_text(encoding="utf-8"))

CASES = {
    "at_right_boundary": (
        "\\documentclass{article}\n"
        "\\makeatletter\n"
        "\\newcommand{\\foo}{Hello}\n"
        "\\begin{document}\n"
        "\\foo\\iftrue @bar\\fi\n"
        "\\end{document}\n"
    ),
    "bgroup_assignment": (
        "\\documentclass{article}\n"
        "\\newif\\ifarxiv\n"
        "\\bgroup\\arxivtrue\\egroup\n"
        "\\begin{document}\n"
        "\\ifarxiv\\typeout{RESULT=TRUE}\\else\\typeout{RESULT=FALSE}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "blank_line_after_fi": (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\edef\\chosen{\\iftrue a\\fi\n"
        "\n"
        "b}\n"
        "\\typeout{RESULT=\\meaning\\chosen}\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "blank_line_after_if": (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\edef\\chosen{a\\iftrue\n"
        "\n"
        "b\\fi}\n"
        "\\typeout{RESULT=\\meaning\\chosen}\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "consumed_binary_command": (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\iftrue\n"
        "\\let\\check\\ifx a\\iffalse\\typeout{RESULT=INNERTRUE}\\else\\typeout{RESULT=INNERFALSE}\\fi\n"
        "\\else\n"
        "\\typeout{RESULT=OUTERFALSE}\n"
        "\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "environment_group_assignment": (
        "\\documentclass{article}\n"
        "\\newif\\ifarxiv\n"
        "\\newenvironment{setup}{}{}\n"
        "\\begin{setup}\\arxivtrue\\end{setup}\n"
        "\\begin{document}\n"
        "\\ifarxiv\\typeout{RESULT=TRUE}\\else\\typeout{RESULT=FALSE}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "escaped_backslash_boundary": (
        "\\documentclass{article}\n"
        "\\def\\\\{}\n"
        "\\begin{document}\n"
        "\\edef\\chosen{\\\\foo\\iftrue bar\\fi}\n"
        "\\typeout{RESULT=\\chosen}\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "ifcat_expanded_operand": (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\iftrue\n"
        "\\ifcat\\iftrue a1\\else bb\\fi\n"
        "\\typeout{RESULT=SAME}\n"
        "\\else\\typeout{RESULT=DIFFERENT}\\fi\n"
        "\\else\\typeout{RESULT=OUTERFALSE}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "indentation_after_if": (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\edef\\chosen{a\\iftrue\n"
        "  b\\fi}\n"
        "\\typeout{RESULT=\\meaning\\chosen}\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "let_equals_alias": (
        "\\documentclass{article}\n"
        "\\let\\check=\\iftrue\n"
        "\\begin{document}\n"
        "\\check\\typeout{RESULT=TRUE}\\else\\typeout{RESULT=FALSE}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "macro_unless_operand": (
        "\\documentclass{article}\n"
        "\\newif\\ifarxiv\n"
        "\\begin{document}\n"
        "\\ifdefined\\unless\\ifarxiv\\typeout{RESULT=TRUE}\\else\\typeout{RESULT=FALSE}\\fi\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "nested_fi_operand": (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\iftrue\n"
        "\\ifx\\fi\\relax\\typeout{RESULT=EQUAL}\\else\\typeout{RESULT=DIFFERENT}\\fi\n"
        "\\else\n"
        "\\typeout{RESULT=OUTERFALSE}\n"
        "\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "newif_operand_not_declaration": (
        "\\documentclass{article}\n"
        "\\usepackage{iftex}\n"
        "\\ifdefined\\newif\\ifXeTeX\\typeout{SETUP=XETEX}\\else\\typeout{SETUP=OTHER}\\fi\\fi\n"
        "\\begin{document}\n"
        "\\ifXeTeX\\typeout{RESULT=XETEX}\\else\\typeout{RESULT=OTHER}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "package_conditional_assignment": (
        "\\documentclass{article}\n"
        "\\usepackage{ifpdf}\n"
        "\\newif\\ifarxiv\n"
        "\\ifpdf\n"
        "\\arxivtrue\n"
        "\\fi\n"
        "\\begin{document}\n"
        "\\ifarxiv\\typeout{RESULT=TRUE}\\else\\typeout{RESULT=FALSE}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "redeclaration": (
        "\\documentclass{article}\n"
        "\\newif\\ifarxiv\n"
        "\\arxivtrue\n"
        "\\newif\\ifarxiv\n"
        "\\begin{document}\n"
        "\\ifarxiv\\typeout{RESULT=TRUE}\\else\\typeout{RESULT=FALSE}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "redefined_by_def": (
        "\\documentclass{article}\n"
        "\\newif\\ifarxiv\n"
        "\\arxivtrue\n"
        "\\def\\ifarxiv{\\iffalse}\n"
        "\\begin{document}\n"
        "\\ifarxiv\\typeout{RESULT=TRUE}\\else\\typeout{RESULT=FALSE}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "redefined_literal": (
        "\\documentclass{article}\n"
        "\\let\\iftrue\\iffalse\n"
        "\\begin{document}\n"
        "\\iftrue\\typeout{RESULT=TRUE}\\else\\typeout{RESULT=FALSE}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "setter_as_let_operand": (
        "\\documentclass{article}\n"
        "\\newif\\ifarxiv\n"
        "\\let\\enable\\arxivtrue\n"
        "\\begin{document}\n"
        "\\ifarxiv\\typeout{RESULT=TRUE}\\else\\typeout{RESULT=FALSE}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "single_at_boundary": (
        "\\documentclass{article}\n"
        "\\makeatletter\n"
        "\\def\\@{AT}\n"
        "\\begin{document}\n"
        "\\@\\iftrue foo\\fi\n"
        "\\end{document}\n"
    ),
    "skipped_branch_operand_roles": (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\iffalse\n"
        "\\let\\check\\iftrue\n"
        "Hidden\n"
        "\\fi\n"
        "\\fi\n"
        "\\typeout{RESULT=VISIBLE}\n"
        "Hello\n"
        "\\end{document}\n"
    ),
    "space_token_operand": (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\iftrue\n"
        "\\ifx a \\iffalse\\typeout{RESULT=INNERTRUE}\\else\\typeout{RESULT=INNERFALSE}\\fi\n"
        "\\else\\typeout{RESULT=MIDDLEFALSE}\\fi\n"
        "\\else\\typeout{RESULT=OUTERFALSE}\\fi\n"
        "Hello\n"
        "\\end{document}\n"
    ),
}


def compile_markers(directory: Path, name: str) -> tuple[int, list[str]]:
    completed = subprocess.run(
        ["xelatex", "-interaction=nonstopmode", "-halt-on-error", name],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode, [line for line in completed.stdout.splitlines() if line.startswith("RESULT=")]


@pytest.mark.parametrize("name", sorted(CASES))
def test_stripped_source_compiles_like_the_original(tmp_path: Path, name: str) -> None:
    (tmp_path / "input.tex").write_text(CASES[name], encoding="utf-8")
    expanded = subprocess.run(
        [*precompile.LATEXPAND_COMMAND, "input.tex"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    warnings: list[str] = []
    stripped = conditionals.strip_dead_branches(expanded, ENVIRONMENTS_TABLE, warnings)
    (tmp_path / "original.tex").write_text(expanded, encoding="utf-8")
    (tmp_path / "stripped.tex").write_text(stripped, encoding="utf-8")
    original = compile_markers(tmp_path, "original.tex")
    assert original[0] == 0
    assert compile_markers(tmp_path, "stripped.tex") == original
