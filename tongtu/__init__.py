"""通途（tongtu）：基于 LaTeX 源码的 arXiv 论文英译中引擎。

设计权威文档见 `docs/ARCHITECTURE.md`（v0.7），零期施工清单见 `docs/PHASE0.md`。
本包是确定性流水线的实现：`stages/` 是各阶段驱动器，`agent/` 是 agent 运行时适配层。
"""

__version__ = "0.0.1"

# 产物契约版本号。所有 JSON 产物（out/*.json）与 --json 事件流均携带此值；
# 字段级定义见 docs/schemas/*.schema.json。契约变更流程：先改 schema 再 bump 此处。
CONTRACT_VERSION = "0.2"

__all__ = ["__version__", "CONTRACT_VERSION"]
