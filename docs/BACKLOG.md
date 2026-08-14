# 遗留整改清单

> 来源：PR #3 评审期间的架构评估（2026-08-14）。本清单只收「已确认要做、但不适合在当前 PR 内完成」的事项；已在 PR 内完成的不列。条目完成后从本清单移除，涉及设计变更的先改 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 结构性重构（建议合并 PR #3 后单独立项）

1. **pipeline.py 拆分为泛型编排器 + 逐阶段 driver 协议。**
   现状：编排、各阶段输入 hash 构成、事件流、六关节接线、干预记录集中在单文件（约 1100 行），新增阶段需改动多处。
   目标：阶段自行声明 inputs / outputs / run，编排器退化为通用循环；`stages/__init__.py` 的 STAGES 表与 pipeline 内部 spec 合并为单一来源。
   时机：一期加入 fallback 流水线之前完成，避免在旧结构上继续堆阶段。

2. **段落切分语义与词法工具去重。**
   chunk（环境深度感知切分）与 validate（纯空行切分）的语义差异目前靠注释维持，考虑抽出共享模块并显式命名两种切分；chunk 内约 40 行与 texlex 重复的词法逻辑合并进 texlex。

## 需要真实数据校准后再动的

3. **compile 的宽松判据复核。**
   「译文错误数不超过原文」的放宽规则在原文自带编译错误时可能掩盖译文引入的同类新错误。批量跑真实论文后，用 report.json 数据核对该规则的误放率，再决定是否收紧（例如按错误指纹比对而非计数）。

4. **anchors 叠加次序与热区容差**（架构附录 B 开放问题 4）。
   现有常量为起步值，拿真实论文的 synctex 数据实测后定稿。

## 增量与工程效率

5. **manifest 失效粒度细化。**
   现状：`tongtu_version` 参与 freshness，任何代码改动使全部阶段重算。目标：按阶段代码 hash 判定，只失效受影响阶段及下游。

6. **fonts 分发方式。**
   50 MB 字体文件当前直接入 git 与 wheel。评估 git LFS 或构建期下载；改动需同步 `find_fonts` 查找链、docker 构建与 zh-pack 组装三处。

7. **report.html 体积。**
   为支持 file:// 双击打开，zh.pdf 以 base64 内嵌进 report-data.js（体积约 +33%），且与 http 场景的 fetch 加载并存两条路径。若产物体积成为问题，评估仅保留 `tongtu preview --serve` 路径或按需生成内嵌版。

## 测试覆盖缺口

8. **survey 成功路径的 e2e 覆盖。**
   e2e 中 MockAgent 恒等返回使 survey 始终走降级骨架，真 JSON 往返只有单元测试覆盖。可为 e2e 增加一个返回合法 brief JSON 的测试 agent 变体。

9. **figures 真实渲染进 CI。**
   pdftocairo / ImageMagick 的真实渲染用例目前本机 skip、CI 的 `-m compile` 未选中。参考镜像（已含 poppler / ghostscript / ImageMagick）发布并接入 ci.yml 后，为这些用例补 `compile` 标记；同时补 EPS / JPEG 的 fixture 资产。

10. **真实论文验收**（零期验收判据 3）。
    本 PR 的开发环境无 arXiv 网络访问与 TeX，尚未用真实论文跑通全流水线。需在具备网络与 TeX 的环境执行 `tongtu run <arxiv-id> --agent codex --model <模型>` 至少一篇，核对产物包与 report.json（`--model` 是必填项：模型标识进翻译缓存 key）。

## 发布链路

11. **参考镜像首次构建验证**：手动触发 release-image（`push=false`）；通过后发布并把 ci.yml 编译层 `container:` 切换到 `ghcr.io/yhnocoder/tongtu`（对应 PHASE0 §3.7 未勾选项）。
12. **合并方式**：分支历史含两个会话中断产生的 WIP 快照提交，建议 squash merge。
