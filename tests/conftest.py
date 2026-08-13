"""公共测试夹具：把「机器上的用户配置」挡在测试之外，并提供假 TeX 工具链。

术语表的第一层是全局表 `$XDG_CONFIG_HOME/tongtu/glossary.json`（架构 §8）。它是**用户
配置**，开发机上很可能真的存在——若不隔离，同一份测试在有全局表的机器上跑出的译文、
缓存 key 与 manifest 都会不一样。故所有测试一律指向一个空的临时 XDG 目录；要测全局层的
用例自己往里写文件（见 `tests/test_glossary.py`）。

`tools` 夹具（假 latexpand / 假 latexmk）原先住在 `tests/test_e2e_identity.py` 里，M3 起
翻译记忆、retranslate 与六关节的测试也要跑整条流水线，故抬到 conftest 共用——三份脚本
各抄一遍必然漂，而漂了就意味着几组测试其实跑在不同的「假 TeX」上。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path_factory, monkeypatch):
    """全局术语表层指向空的临时目录，测试之间互不串味。"""
    root = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    return root


# --------------------------------------------------------------------------- #
# 假 latexpand / 假 latexmk（架构 §12 层 2 的成本纪律：本机没 TeX 也要能跑全链路）
# --------------------------------------------------------------------------- #

FAKE_LATEXPAND = r'''#!/usr/bin/env python3
"""最小 latexpand 替身：递归拼 \input，按需内联 .bbl，结果回显到 stdout。"""
import re, sys
from pathlib import Path

argv = sys.argv[1:]
main, bbl = None, None
i = 0
while i < len(argv):
    if argv[i] == "--expand-bbl":
        bbl = argv[i + 1]
        i += 2
        continue
    if argv[i].startswith("--"):
        i += 1
        continue
    main = argv[i]
    i += 1

INPUT = re.compile(r"\\(?:input|include)\{([^}]*)\}")


def expand(path: Path) -> str:
    text = path.read_text(encoding="utf-8")

    def sub(match):
        name = match.group(1)
        target = Path(name if name.endswith(".tex") else name + ".tex")
        return expand(target)

    return INPUT.sub(sub, text)


text = expand(Path(main))
if bbl:
    # 替换文本用 lambda 递进去：.bbl 里全是反斜杠，当成 re 的替换模板会被当转义解释
    body = Path(bbl).read_text(encoding="utf-8")
    text = re.sub(r"\\bibliography\{[^}]*\}", lambda m: body, text)
sys.stdout.write(text)
'''

FAKE_LATEXMK = r'''#!/usr/bin/env python3
"""最小 latexmk 替身：把 tex 原样写进「PDF」，日志里没有 ! 错误，退出 0。"""
import sys
from pathlib import Path

tex = Path([a for a in sys.argv[1:] if not a.startswith("-")][-1])
if not tex.is_file():
    sys.stderr.write("fake latexmk: %s not found\n" % tex)
    sys.exit(1)
Path(tex.stem + ".log").write_text(
    "This is fake latexmk\nOutput written on %s.pdf (3 pages).\n" % tex.stem, encoding="utf-8"
)
Path(tex.stem + ".pdf").write_bytes(
    b"%PDF-1.4\n" + tex.read_bytes() + b"\n%%EOF\n"
)
'''


def _install(bindir: Path, name: str, body: str) -> None:
    script = bindir / name
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def tools(request, tmp_path, monkeypatch):
    """fake 模式：把假 latexpand / 假 latexmk 塞到 PATH 最前面；real 模式：什么也不做。

    参数化用 `indirect=True` 传 `"fake"` / `"real"`（见 `tests/test_e2e_identity.py`
    的 `MODES`）；不参数化时默认 fake。
    """
    mode = getattr(request, "param", "fake")
    if mode == "fake":
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _install(bindir, "latexpand", FAKE_LATEXPAND)
        _install(bindir, "latexmk", FAKE_LATEXMK)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return mode
