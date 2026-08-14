"""确定性流水线各阶段（架构 §3）。

**所有阶段都由脚本驱动，不存在「agent 驱动的阶段」**；表中「agent 介入点」的含义是
该阶段的驱动器在特定触发条件下拉起一次有界的 agent 调用（`tongtu.agent` 两原语之一），
拿回结果后仍由脚本校验推进，控制流从不移交。每个阶段的出口判据都是机械的。

零期各阶段按里程碑逐个填（见 docs/PHASE0.md §4）：

| 阶段      | 里程碑 | 出口判据 |
|-----------|--------|----------|
| fetch     | M2 | 源码树落 `src/`（PDF-only → 报错并标记降级路线） |
| flatten   | M2 | 单文件 `flat.tex` |
| baseline  | M2 | 原文 PDF 编译通过（最早的编译门控） |
| mask      | M1 | `unmask(mask(x)) == x` 恒等；`blocks.json` 完整 |
| survey    | M3 | `brief.json` 与结构化术语表通过 schema 校验（模型那一路失败则降级为确定性骨架，不阻塞） |
| chunk     | M1 | 块清单（每块 = 完整段落序列） |
| translate | M2 驱动 / M3 扩全 | validate 全绿（占位符 / 控制序列 multiset / 段落数） |
| compile   | M2 | `zh.pdf` 编译通过（坏段回退原文保底） |
| figures   | M4 | `figures/*.png` + 元数据齐全 |
| export    | M4 | 产物包契约 schema 校验通过 |

阶段序、阶段级增量（`build/manifests/<stage>.json`）与 `--json` 事件流由
:mod:`tongtu.pipeline` 编排——本包只放各阶段的驱动器，谁也不认识谁，更不认识编排器；
figures / export 尚未实现，编排器把它们记为 `skipped`（`tongtu.pipeline.
SKIPPED_STAGES`），不是从阶段序里删掉——阶段图对所有论文不变（架构 §3）。
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
