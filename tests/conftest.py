"""测试共用的路径与数据 fixture。

分层与执行环境见 docs/ci/README.md：不带标记的用例属文本层，无外部依赖；`compile`
标记的用例需要 TeX 与参考镜像，`network` 标记的用例还要访问 arXiv。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from tongtu import masking

#: 仓库根：本文件在 `<仓库根>/tests/` 下。
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 三篇自造论文所在目录，与 examples/README.md 的清单一致。
PAPERS_DIR = REPO_ROOT / "examples" / "papers"

#: 三篇自造论文的目录名，工作目录名由 CLI 取自它。
FIXTURE_PAPERS: tuple[str, ...] = ("article", "revtex", "conference")

#: 已安装的 `tongtu` 可执行文件：pytest 在项目环境里运行，它与解释器同目录。
TONGTU_BIN = Path(sys.executable).parent / "tongtu"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def environments_table() -> Mapping[str, masking.TableEntry]:
    """环境分类表，解析一次供全部用例复用。"""
    content = masking.ENVIRONMENTS_TABLE_PATH.read_text(encoding="utf-8")
    return masking.parse_environment_table(content)


@pytest.fixture(scope="session")
def paper_manifests() -> dict[str, dict]:
    """三篇自造论文的 MANIFEST.json，按目录名索引。"""
    return {
        name: json.loads((PAPERS_DIR / name / "MANIFEST.json").read_text(encoding="utf-8")) for name in FIXTURE_PAPERS
    }


def paper_dir(name: str) -> Path:
    """一篇自造论文的源码目录。"""
    return PAPERS_DIR / name


def tex_sources(name: str) -> list[Path]:
    """一篇自造论文源码树里的全部 `.tex` 文件，路径排序。"""
    return sorted(paper_dir(name).rglob("*.tex"))
