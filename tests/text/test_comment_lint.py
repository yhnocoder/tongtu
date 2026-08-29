from pathlib import Path

from scripts.comment_lint import in_scope, violations


def write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_plain_comment_is_violation(tmp_path: Path) -> None:
    path = write(tmp_path, "x = 1  # 说明这一行\n")
    found = violations(path)
    assert len(found) == 1
    assert found[0].startswith(f"{path}:1: 注释: ")


def test_module_class_function_docstrings_are_violations(tmp_path: Path) -> None:
    source = '"""模块说明"""\n\n\nclass A:\n    """类说明"""\n\n    def m(self) -> None:\n        """方法说明"""\n\n\ndef f() -> None:\n    """函数说明"""\n'
    found = violations(write(tmp_path, source))
    assert len(found) == 4
    assert all(": docstring: " in line for line in found)


def test_tool_directives_are_allowed(tmp_path: Path) -> None:
    source = "import os  # noqa: F401\nx: int = os  # type: ignore[assignment]\n\n\ndef f() -> None:  # pragma: no cover\n    return None\n"
    assert violations(write(tmp_path, source)) == []


def test_shebang_on_first_line_is_allowed(tmp_path: Path) -> None:
    assert violations(write(tmp_path, "#!/usr/bin/env python3\nx = 1\n")) == []


def test_shebang_not_on_first_line_is_violation(tmp_path: Path) -> None:
    path = write(tmp_path, "x = 1\n#!/usr/bin/env python3\n")
    found = violations(path)
    assert found == [f"{path}:2: 注释: #!/usr/bin/env python3"]


def test_clean_file_has_no_violations(tmp_path: Path) -> None:
    source = "from pathlib import Path\n\n\ndef f(p: Path) -> str:\n    return p.name\n"
    assert violations(write(tmp_path, source)) == []


def test_in_scope_filters_path_and_suffix() -> None:
    assert in_scope("tongtu/cli.py")
    assert in_scope("tests/text/test_comment_lint.py")
    assert in_scope("scripts/comment_lint.py")
    assert not in_scope("docs/design/pipeline.html")
    assert not in_scope("examples/papers/main.tex")
    assert not in_scope("noxfile.py")
