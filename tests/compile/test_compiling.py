from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tongtu import compiling

pytestmark = pytest.mark.compile

DOCUMENT = """\\documentclass{article}
\\usepackage{xeCJK}
\\setCJKmainfont[BoldFont=LXGWWenKai-Medium.ttf]{LXGWWenKai-Light.ttf}
\\setCJKsansfont[BoldFont=SourceHanSansSC-Bold.otf]{SourceHanSansSC-Regular.otf}
\\begin{document}
正文宋体 \\textbf{正文粗体} \\textsf{无衬线 \\textbf{无衬线粗体}}
\\end{document}
"""


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONGTU_HOME", str(tmp_path / "home"))


def test_bundled_fonts_are_found_by_name_after_the_tree_moves(tmp_path: Path) -> None:
    tree = tmp_path / "paper"
    tree.mkdir()
    (tree / "main.tex").write_text(DOCUMENT, encoding="utf-8")
    first = compiling.attempt_compile(tree, "main.tex")
    assert first.passed, compiling.failure_message(first)
    assert not (tree / "fonts").exists()
    moved = tmp_path / "elsewhere"
    shutil.move(tree, moved)
    assert compiling.clean_tree(moved, "main.tex") == []
    assert not (moved / "main.pdf").exists()
    second = compiling.attempt_compile(moved, "main.tex")
    assert second.passed, compiling.failure_message(second)
    assert (moved / "main.pdf").stat().st_size > 0
