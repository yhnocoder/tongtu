"""确定性流水线各阶段（架构 §3）。

当前只保留阶段名清单，供 CLI 的 `stage` 命令约束取值；各阶段驱动器逐个重建，
实现随重建落回本包。阶段图对所有论文不变：不适用的阶段记 skipped，不从序里删。
"""

from __future__ import annotations

#: 流水线阶段名，顺序即 `tongtu run` 的执行顺序（figures 仅依赖 src/，可并行）。
STAGES: tuple[str, ...] = (
    "fetch",
    "flatten",
    "baseline",
    "mask",
    "survey",
    "chunk",
    "translate",
    "compile",
    "figures",
    "export",
)

__all__ = ["STAGES"]
