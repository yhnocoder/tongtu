from __future__ import annotations

import os
from pathlib import Path

import pytest

from tongtu.reconstruct import checks

pytestmark = pytest.mark.compile

SOURCE = "\\documentclass{article}\\usepackage{graphicx}\n\\begin{document}\nHello wonderful world\n\\end{document}\n"


def test_draft_words_ignore_unreadable_files_of_the_session(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(SOURCE, encoding="utf-8")
    os.mkfifo(tmp_path / ".bashrc")
    (tmp_path / ".claude").mkdir()
    os.mkfifo(tmp_path / ".claude" / "skills")
    (tmp_path / ".skills").mkdir()
    os.mkfifo(tmp_path / ".skills" / "check.py")
    assert checks.rebuilt_words(tmp_path) == [("hello", 0), ("wonderful", 0), ("world", 0)]
