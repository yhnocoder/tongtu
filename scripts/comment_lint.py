#!/usr/bin/env python3
from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOTS = ("tongtu/", "tests/", "scripts/")
DIRECTIVES = ("noqa", "type: ignore", "pragma: no cover")
WHY = re.compile(r"why\(#\d+\): \S")
SCRIPT_METADATA = re.compile(r"(?m)^# /// [a-zA-Z0-9-]+$\s(?:^#(?:| .*)$\s)+^# ///$")
DOC_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def in_scope(name: str) -> bool:
    path = Path(name)
    return path.suffix == ".py" and path.as_posix().startswith(ROOTS)


def brief(text: str) -> str:
    return " ".join(text.split())[:60]


def violations(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    found: list[tuple[int, str]] = []
    metadata_lines = {
        line
        for block in SCRIPT_METADATA.finditer(text)
        for line in range(text.count("\n", 0, block.start()) + 1, text.count("\n", 0, block.end()) + 2)
    }
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT or token.start[0] in metadata_lines:
            continue
        if token.start == (1, 0) and token.string.startswith("#!"):
            continue
        body = token.string.lstrip("#").strip()
        if body.startswith(DIRECTIVES) or WHY.match(body):
            continue
        found.append((token.start[0], f"注释: {brief(token.string)}"))
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, DOC_NODES):
            continue
        first = node.body[0] if node.body else None
        if not isinstance(first, ast.Expr):
            continue
        if isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            found.append((first.lineno, f"docstring: {brief(first.value.value)}"))
    return [f"{path}:{line}: {message}" for line, message in sorted(found)]


def merge_base() -> str:
    for ref in ("origin/main", "main"):
        done = subprocess.run(["git", "merge-base", ref, "HEAD"], capture_output=True, text=True)
        if done.returncode == 0:
            return done.stdout.strip()
    raise SystemExit("comment_lint：找不到 origin/main 或 main，无法确定改动范围")


def git_lines(args: list[str]) -> list[str]:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return done.stdout.splitlines()


def changed_files() -> list[str]:
    tracked = git_lines(["diff", "--name-only", "--diff-filter=AMR", merge_base()])
    untracked = git_lines(["ls-files", "--others", "--exclude-standard"])
    return sorted(set(tracked + untracked))


def main(argv: list[str]) -> int:
    lines: list[str] = []
    for name in argv or changed_files():
        path = Path(name)
        if not in_scope(name) or not path.exists():
            continue
        lines.extend(violations(path))
    if lines:
        print("\n".join(lines))
        print(f"comment_lint：{len(lines)} 处违例，改动过的文件不得留注释与 docstring")
        return 1
    print("comment_lint：无违例")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
