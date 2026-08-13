"""测试夹具：把「机器上的用户配置」挡在测试之外。

术语表的第一层是全局表 `$XDG_CONFIG_HOME/tongtu/glossary.json`（架构 §8）。它是**用户
配置**，开发机上很可能真的存在——若不隔离，同一份测试在有全局表的机器上跑出的译文、
缓存 key 与 manifest 都会不一样。故所有测试一律指向一个空的临时 XDG 目录；要测全局层的
用例自己往里写文件（见 `tests/test_glossary.py`）。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path_factory, monkeypatch):
    """全局术语表层指向空的临时目录，测试之间互不串味。"""
    root = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    return root
