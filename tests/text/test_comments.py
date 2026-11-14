from __future__ import annotations

from collections.abc import Mapping

from tongtu import comments, masking


def strip(source: str, table: Mapping[str, masking.TableEntry]) -> str:
    return comments.strip_comment_text(source.encode("utf-8"), table).decode("utf-8")


def test_comment_text_is_emptied_and_newlines_are_kept(environments_table: Mapping[str, masking.TableEntry]) -> None:
    source = "% whole line\nText % tail\n\\% escaped % gone\n\\\\% after a line break\n"
    assert strip(source, environments_table) == "%\nText %\n\\% escaped %\n\\\\%\n"


def test_code_environments_keep_their_percent_signs(environments_table: Mapping[str, masking.TableEntry]) -> None:
    source = (
        "\\begin{lstlisting}\ninit[layer_id % rate, 0] = 1.\n\\end{lstlisting} % note\n"
        "\\begin{verbatim*}\n50% done\n\\end{verbatim*}\n"
        "\\begin{minted}{python}\nx = a % b\n\\end{minted}\n"
        "\\begin{Verbatim}[numbers=left]\n# 100% here\n\\end{Verbatim}\n"
    )
    expected = source.replace("\\end{lstlisting} % note", "\\end{lstlisting} %")
    assert strip(source, environments_table) == expected


def test_comment_environment_is_stripped_like_text(environments_table: Mapping[str, masking.TableEntry]) -> None:
    source = "\\begin{comment}\nold % text\n\\end{comment}\n"
    assert strip(source, environments_table) == "\\begin{comment}\nold %\n\\end{comment}\n"


def test_verb_keeps_its_percent_sign(environments_table: Mapping[str, masking.TableEntry]) -> None:
    source = "Use \\verb|a % b| and \\verb*+%+ here % tail\n"
    assert strip(source, environments_table) == "Use \\verb|a % b| and \\verb*+%+ here %\n"


def test_url_percent_is_escaped(environments_table: Mapping[str, masking.TableEntry]) -> None:
    source = "See \\url{https://x.org/a%20b} now % tail\n"
    assert strip(source, environments_table) == "See \\url{https://x.org/a\\%20b} now %\n"


def test_commented_begin_does_not_open_a_code_environment(
    environments_table: Mapping[str, masking.TableEntry],
) -> None:
    source = "% \\begin{verbatim}\nText % tail\n"
    assert strip(source, environments_table) == "%\nText %\n"


def test_unclosed_code_environment_is_stripped_like_text(
    environments_table: Mapping[str, masking.TableEntry],
) -> None:
    source = "\\begin{verbatim}\n50% done\n"
    assert strip(source, environments_table) == "\\begin{verbatim}\n50%\n"


def test_lines_after_end_document_are_dropped(environments_table: Mapping[str, masking.TableEntry]) -> None:
    source = "Body\n\\end{document}\nleftover notes\n"
    assert strip(source, environments_table) == "Body\n\\end{document}\n"


def test_invalid_utf8_bytes_pass_through(environments_table: Mapping[str, masking.TableEntry]) -> None:
    source = b"Caf\xe9 % comment \xff\n"
    assert comments.strip_comment_text(source, environments_table) == b"Caf\xe9 %\n"
