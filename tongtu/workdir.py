"""工作目录解析与四区布局（架构 §5）。

论文工作目录**不在仓库内**。解析优先级（高 → 低）：

1. `--workdir DIR`   —— 直接指定论文工作目录本身
2. `$TONGTU_HOME/<arxiv_id>`
3. `~/.local/share/tongtu/<arxiv_id>`

四区布局：

    <workdir>/
    ├── src/          # e-print 原始解包，只读不改
    ├── build/        # 流水线工作区，可整体删除
    │   └── manifests/  # 阶段级增量构建 manifest（<stage>.json）
    ├── out/          # 产物包（契约文件）
    └── logs/         # agent 会话转录、编译日志
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: 默认工作目录根（`$TONGTU_HOME` 未设时）。
DEFAULT_ROOT = Path("~/.local/share/tongtu")

#: 环境变量名：覆盖工作目录根。
HOME_ENV = "TONGTU_HOME"

#: 四区目录名，顺序即架构 §5 的书写顺序。
AREAS: tuple[str, ...] = ("src", "build", "out", "logs")

#: 阶段 manifest 目录（位于 build/ 之下，随 build/ 一同可丢弃）。
MANIFESTS_DIRNAME = "manifests"


class WorkdirError(ValueError):
    """工作目录解析失败（非法 arXiv id 等）。"""


def normalize_arxiv_id(arxiv_id: str) -> str:
    """把 arXiv id 规范成安全的单层目录名。

    新式 id（`2401.01234`、`2401.01234v2`）原样返回；旧式 id 含斜杠
    （`hep-th/9901001`）转成 `hep-th_9901001`，避免建出多层目录。
    拒绝空串与任何形式的路径穿越。
    """
    raw = (arxiv_id or "").strip()
    if not raw:
        raise WorkdirError("arXiv id 为空")
    if raw.startswith(("/", "~", ".")) or "\\" in raw:
        raise WorkdirError(f"非法 arXiv id：{arxiv_id!r}")
    normalized = raw.replace("/", "_")
    if normalized in (".", "..") or os.sep in normalized:
        raise WorkdirError(f"非法 arXiv id：{arxiv_id!r}")
    return normalized


def default_root(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """工作目录根：`$TONGTU_HOME`，未设则 `~/.local/share/tongtu`。"""
    environ = os.environ if env is None else env
    home = (environ.get(HOME_ENV) or "").strip()
    if home:
        return Path(home).expanduser()
    return DEFAULT_ROOT.expanduser()


def resolve(
    arxiv_id: str | None = None,
    workdir: str | os.PathLike[str] | None = None,
    env: os._Environ[str] | dict[str, str] | None = None,
) -> Path:
    """按优先级解析论文工作目录路径（不创建目录）。

    `workdir` 给出时直接采用（指的是论文目录本身，不是根目录），
    否则用 `default_root()/normalize_arxiv_id(arxiv_id)`。
    """
    if workdir is not None:
        return Path(workdir).expanduser().absolute()
    if arxiv_id is None:
        raise WorkdirError("需要 arxiv_id 或 --workdir 之一")
    return (default_root(env) / normalize_arxiv_id(arxiv_id)).absolute()


@dataclass(frozen=True)
class Workdir:
    """一篇论文的工作目录及其四区。

    只描述路径，不做 IO；`create()` 才落盘（幂等）。
    """

    path: Path
    arxiv_id: str | None = None

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

    @property
    def areas(self) -> tuple[Path, ...]:
        return tuple(self.path / name for name in AREAS)

    def manifest_path(self, stage: str) -> Path:
        """阶段级增量构建 manifest 的路径（架构 §4）。"""
        if not stage or "/" in stage or os.sep in stage:
            raise WorkdirError(f"非法阶段名：{stage!r}")
        return self.manifests / f"{stage}.json"

    def exists(self) -> bool:
        return self.path.is_dir()

    def create(self) -> "Workdir":
        """创建工作目录、四区与 `build/manifests/`；已存在则原样返回。"""
        for directory in (self.path, *self.areas, self.manifests):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def open_workdir(
    arxiv_id: str | None = None,
    workdir: str | os.PathLike[str] | None = None,
    env: os._Environ[str] | dict[str, str] | None = None,
    create: bool = False,
) -> Workdir:
    """解析（可选创建）论文工作目录。CLI 各子命令的统一入口。"""
    resolved = Workdir(
        path=resolve(arxiv_id=arxiv_id, workdir=workdir, env=env),
        arxiv_id=normalize_arxiv_id(arxiv_id) if arxiv_id else None,
    )
    return resolved.create() if create else resolved
