"""随仓库分发的资产定位：中文字体（`fonts/`）与 prompt 资产（`skill/`）的路径。

同一份资产在两种布局下位置不同：仓库里它们在仓库根，装成 wheel 时 pyproject 的
force-include 把它们放进包目录下的 `data/`。`asset_path` 先查打包路径、再退回仓库布局，
消费方按名字取路径即可，不必各自判断当前是哪种布局。
"""

from __future__ import annotations

from pathlib import Path

#: tongtu 包目录；仓库布局下它的上一级是仓库根。
PACKAGE_DIR = Path(__file__).resolve().parent

#: 打包进 wheel 时资产所在的目录名（相对包目录），取值与 pyproject 的 force-include 一致。
PACKAGED_DIRNAME = "data"


def asset_path(name: str) -> Path:
    """按名字定位资产目录或文件，如 `asset_path("fonts")`、`asset_path("skill")`。

    先查打包路径 `<包目录>/data/<name>`，它存在即返回；否则返回仓库布局下的
    `<仓库根>/<name>`。两处都不存在时返回后者，缺失由调用方按自己的方式处置。
    """
    packaged = PACKAGE_DIR / PACKAGED_DIRNAME / name
    if packaged.exists():
        return packaged
    return PACKAGE_DIR.parent / name
