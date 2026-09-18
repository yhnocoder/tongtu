from __future__ import annotations

from tongtu import preamble

ACL_HEAD = """% This must be in the first 5 lines to tell arXiv to use pdfLaTeX, which is strongly recommended.
\\pdfoutput=1
% In particular, the hyperref package requires pdfLaTeX in order to break URLs across lines.

\\documentclass[11pt]{article}
\\usepackage{microtype}
\\usepackage{CJK}
\\usepackage{fdsymbol}
\\begin{document}
Body.
\\end{document}
"""

AAAI_PDFINFO = """\\documentclass[letterpaper]{article}
 \\pdfinfo{
/Title (Neural Machine Translation with Byte-Level Subwords)
/Author (Changhan Wang, Kyunghyun Cho, Jiatao Gu)
} %Leave this
\\begin{document}
Body.
\\end{document}
"""

NEURIPS_2207 = """\\documentclass[postscript]{article}
\\usepackage[dvipsnames]{xcolor}
\\usepackage[postscript, cjkjis]{ucs}
\\usepackage{pifont}
\\usepackage[whole]{bxcjkjatype}
\\usepackage{fdsymbol}
\\usepackage{microtype}      % microtypography
\\begin{document}
Body.
\\end{document}
"""

NEURIPS_2510 = """\\documentclass{article}
\\usepackage{microtype}      % microtypography
\\usepackage{enumitem}
\\usepackage{fontawesome}
\\usepackage{cleveref}
\\begin{document}
\\faGithub
\\end{document}
"""


def run(text: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    return preamble.adapt(text, warnings), warnings


def test_pdfoutput_before_documentclass_is_removed() -> None:
    output, warnings = run(ACL_HEAD)
    assert "\\pdfoutput" not in output
    assert "\\documentclass[11pt]{article}" in output
    assert "removed the pdftex assignment \\pdfoutput=1 at line 2" in warnings


def test_pdfoutput_with_spaces_and_relax_is_removed() -> None:
    output, warnings = run("\\pdfoutput = 1 \\relax\n\\documentclass{article}\n\\begin{document}\n\\end{document}\n")
    assert output.startswith("\n\\documentclass{article}")
    assert warnings == ["removed the pdftex assignment \\pdfoutput = 1 \\relax at line 1"]


def test_other_pdftex_assignments_are_removed() -> None:
    text = "\\documentclass{article}\n\\pdfcompresslevel=9\n\\pdfminorversion 7\n\\begin{document}\n\\end{document}\n"
    output, warnings = run(text)
    assert "\\pdfcompresslevel" not in output and "\\pdfminorversion" not in output
    assert len(warnings) == 2


def test_ifpdf_idioms_are_kept() -> None:
    text = (
        "\\documentclass{article}\n"
        "\\ifx\\pdfoutput\\undefined\n\\newcommand{\\engine}{other}\n\\else\n\\newcommand{\\engine}{pdf}\n\\fi\n"
        "\\ifnum\\pdfoutput=1 \\relax\\fi\n"
        "\\begin{document}\n\\end{document}\n"
    )
    output, warnings = run(text)
    assert output == text
    assert warnings == []


def test_pdfinfo_block_spanning_lines_is_removed() -> None:
    output, warnings = run(AAAI_PDFINFO)
    assert "\\pdfinfo" not in output and "/Title" not in output and "/Author" not in output
    assert " %Leave this\n\\begin{document}" in output
    assert warnings == ["removed the pdftex command \\pdfinfo{...} at line 2"]


def test_driver_option_is_removed_and_package_kept() -> None:
    text = "\\documentclass{article}\n\\usepackage[pdftex]{graphicx} \n\\begin{document}\n\\end{document}\n"
    output, warnings = run(text)
    assert "\\usepackage{graphicx} \n" in output
    assert warnings == ["removed the driver option pdftex from \\usepackage{graphicx} at line 2"]


def test_driver_option_removed_from_multiple_packages() -> None:
    output, warnings = run(
        "\\documentclass{article}\n\\usepackage[pdftex]{graphicx,color}\n\\begin{document}\n\\end{document}\n"
    )
    assert "\\usepackage{graphicx,color}\n" in output
    assert warnings == ["removed the driver option pdftex from \\usepackage{graphicx,color} at line 2"]


def test_multiline_options_only_lose_the_driver() -> None:
    text = "\\documentclass{article}\n\\usepackage[\n pdftex,\n colorlinks]{hyperref}\n\\begin{document}\n\\end{document}\n"
    output, warnings = run(text)
    assert "\\usepackage[colorlinks]{hyperref}\n" in output
    assert "pdftex" not in output
    assert len(warnings) == 1


def test_commented_usepackage_is_untouched() -> None:
    text = "\\documentclass{article}\n%\\usepackage[pdftex]{graphicx}\n\\usepackage{graphicx} % \\pdfoutput=1\n\\begin{document}\n\\end{document}\n"
    output, warnings = run(text)
    assert output == text
    assert warnings == []


def test_body_is_untouched() -> None:
    text = (
        "\\documentclass{article}\n\\begin{document}\n\\pdfoutput=1\n\\usepackage[pdftex]{graphicx}\n\\end{document}\n"
    )
    output, warnings = run(text)
    assert output == text
    assert warnings == []


def test_dropped_packages_remove_whole_statement() -> None:
    output, warnings = run(NEURIPS_2207)
    assert "ucs" not in output and "bxcjkjatype" not in output
    assert "\\usepackage[dvipsnames]{xcolor}\n" in output
    assert "\\usepackage{pifont}\n" in output
    assert "\\usepackage{fdsymbol}\n" in output
    assert "\\usepackage[protrusion=false]{microtype}      % microtypography\n" in output
    assert "removed the pdflatex-era package(s) ucs from \\usepackage at line 3" in warnings
    assert "removed the pdflatex-era package(s) bxcjkjatype from \\usepackage at line 5" in warnings
    assert "added protrusion=false to \\usepackage{microtype} at line 7" in warnings
    assert len(warnings) == 3


def test_dropped_package_in_a_list_keeps_the_others() -> None:
    output, warnings = run(
        "\\documentclass{article}\n\\usepackage{amsmath,CJKspace}\n\\begin{document}\n\\end{document}\n"
    )
    assert "\\usepackage{amsmath}\n" in output
    assert warnings == ["removed the pdflatex-era package(s) CJKspace from \\usepackage at line 2"]


def test_legacy_cjk_packages_and_environments_are_stripped() -> None:
    text = (
        "\\documentclass{article}\n\\usepackage{CJKutf8}\n\\begin{document}\n"
        "\\begin{CJK*}{UTF8}{gbsn}\n\\CJKfamily{gbsn}\n正文保留。\n\\end{CJK*}\n\\end{document}\n"
    )
    output, warnings = run(text)
    assert "CJKutf8" not in output and "\\begin{CJK*}" not in output
    assert "\\end{CJK*}" not in output and "\\CJKfamily" not in output
    assert "正文保留。" in output
    assert "stripped 3 CJK environment wrappers and \\CJKfamily settings" in warnings


def test_cjkfamily_is_kept_when_no_legacy_package_was_dropped() -> None:
    text = "\\documentclass{article}\n\\usepackage{xeCJK}\n\\begin{document}\n\\CJKfamily{song}\n\\end{document}\n"
    output, warnings = run(text)
    assert output == text
    assert warnings == []


def test_microtype_gets_protrusion_false() -> None:
    output, warnings = run(NEURIPS_2510)
    assert "\\usepackage[protrusion=false]{microtype}      % microtypography\n" in output
    assert "added protrusion=false to \\usepackage{microtype} at line 2" in warnings


def test_microtype_with_existing_protrusion_option_is_untouched() -> None:
    text = "\\documentclass{article}\n\\usepackage[protrusion=true,expansion]{microtype}\n\\begin{document}\n\\end{document}\n"
    output, warnings = run(text)
    assert output == text
    assert warnings == []


def test_microtype_keeps_its_other_options() -> None:
    output, _ = run("\\documentclass{article}\n\\usepackage[final]{microtype}\n\\begin{document}\n\\end{document}\n")
    assert "\\usepackage[final,protrusion=false]{microtype}\n" in output


def test_microtype_in_a_shared_statement_is_split_out() -> None:
    output, _ = run("\\documentclass{article}\n\\usepackage{xcolor,microtype}\n\\begin{document}\n\\end{document}\n")
    assert "\\usepackage{xcolor}\n\\usepackage[protrusion=false]{microtype}\n" in output


def test_fontawesome_prelude_is_inserted_before_the_package() -> None:
    output, warnings = run(NEURIPS_2510)
    assert (
        "\\usepackage{enumitem}\n"
        "\\newfontfamily\\FA{FontAwesome.otf}\n"
        "\\let\\tongtuorig\\newfontfamily\n"
        "\\def\\newfontfamily#1#2{\\global\\let\\newfontfamily\\tongtuorig}\n"
        "\\usepackage{fontawesome}\n"
        "\\usepackage{cleveref}\n"
    ) in output
    assert "inserted the fontawesome prelude before \\usepackage{fontawesome} at line 4" in warnings
    assert len(warnings) == 2


def test_requirepackage_is_rewritten_too() -> None:
    output, warnings = run(
        "\\documentclass{article}\n\\RequirePackage[dvips]{graphicx}\n\\begin{document}\n\\end{document}\n"
    )
    assert "\\RequirePackage{graphicx}\n" in output
    assert warnings == ["removed the driver option dvips from \\RequirePackage{graphicx} at line 2"]


def test_xecjk_document_is_unchanged() -> None:
    text = "\\documentclass{article}\n\\usepackage{xeCJK}\n\\usepackage{graphicx}\n\\begin{document}\n中文\n\\end{document}\n"
    output, warnings = run(text)
    assert output == text
    assert warnings == []


def test_unbalanced_braces_leave_the_text_alone() -> None:
    text = "\\documentclass{article}\n\\pdfinfo{\n/Title (x)\n\\begin{document}\n\\end{document}\n"
    output, warnings = run(text)
    assert output == text
    assert len(warnings) == 1 and "not rewritten" in warnings[0]
