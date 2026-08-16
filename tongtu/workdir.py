"""工作目录解析与四区布局。

论文工作目录不在仓库内。解析优先级（高 → 低）：

1. `--workdir DIR`：直接指定论文工作目录本身
2. `$TONGTU_HOME/<编号>`
3. `~/.local/share/tongtu/<编号>`

四区布局：

    <workdir>/
    ├── src/            # e-print 内容的原样落盘，只读不改
    ├── build/          # 流水线工作区，可整体删除
    │   └── manifests/  # 阶段级 stage manifest（<stage>.json）
    ├── out/            # artifact package（契约文件）
    └── logs/           # agent 会话 trace、编译日志
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: `$TONGTU_HOME` 未设时的工作目录根。
DEFAULT_ROOT = Path("~/.local/share/tongtu")

#: 覆盖工作目录根的环境变量名。
HOME_ENV = "TONGTU_HOME"

#: 四区目录名。
AREAS: tuple[str, ...] = ("src", "build", "out", "logs")

#: stage manifest 目录名（位于 build/ 之下，随 build/ 一同可丢弃）。
MANIFESTS_DIRNAME = "manifests"


class WorkdirError(ValueError):
    """工作目录解析失败（编号不合法、参数缺失）。"""


def normalize_arxiv_id(arxiv_id: str) -> str:
    """把 arXiv 编号规范成单层目录名。

    编号里的斜杠（如 `hep-th/9901001`）替换为下划线，避免建出多层目录；版本号
    后缀（如 `2002.05202v1`）原样保留。空串、含空白或路径穿越形态的输入一律拒绝。
    """
    raw = (arxiv_id or "").strip()
    if not raw:
        raise WorkdirError("arXiv 编号为空")
    if raw.startswith(("/", "~", ".")) or "\\" in raw or ".." in raw or any(ch.isspace() for ch in raw):
        raise WorkdirError(f"不是合法的 arXiv 编号：{arxiv_id!r}")
    return raw.replace("/", "_")


def default_root(env: Mapping[str, str] | None = None) -> Path:
    """工作目录根：`$TONGTU_HOME`，未设则 `~/.local/share/tongtu`。"""
    environ = os.environ if env is None else env
    home = (environ.get(HOME_ENV) or "").strip()
    if home:
        return Path(home).expanduser()
    return DEFAULT_ROOT.expanduser()


def resolve(
    arxiv_id: str | None = None,
    workdir: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """按优先级解析论文工作目录路径（不创建目录）。

    `workdir` 给出时直接采用——它指的是论文工作目录本身，不是根目录；否则用
    `default_root()/normalize_arxiv_id(arxiv_id)`。
    """
    if workdir is not None:
        return Path(workdir).expanduser().absolute()
    if arxiv_id is None:
        raise WorkdirError("需要 arXiv 编号或 --workdir 之一")
    return (default_root(env) / normalize_arxiv_id(arxiv_id)).absolute()


@dataclass(frozen=True)
class Workdir:
    """一篇论文的工作目录及其四区。只描述路径，不做 IO；`create()` 才落盘（幂等）。"""

    path: Path

    @property
    def src(self) -> Path:
        return self.path / "src"

    @property
    def build(self) -> Path:
        return self.path / "build"

    @property
    def out(self) -> Path:
        return self.path / "out"

    @property
    def logs(self) -> Path:
        return self.path / "logs"

    @property
    def manifests(self) -> Path:
        return self.build / MANIFESTS_DIRNAME

    def manifest_path(self, stage: str) -> Path:
        return self.manifests / f"{stage}.json"

    def create(self) -> None:
        """建出四区与 manifest 目录（幂等）。"""
        for name in AREAS:
            (self.path / name).mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
