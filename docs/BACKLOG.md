# 遗留整改清单

> 来源：PR #3 评审期间的架构评估（2026-08-14）。本清单只收「已确认要做、但不适合在当前 PR 内完成」的事项；已在 PR 内完成的不列。条目完成后从本清单移除，涉及设计变更的先改 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 结构性重构（建议合并 PR #3 后单独立项）

1. **pipeline.py 拆分为泛型编排器 + 逐阶段 driver 协议。**
   现状：编排、各阶段输入 hash 构成、事件流、六个 agent 调用点的接线、干预记录集中在单文件（约 1100 行），新增阶段需改动多处。
   目标：阶段自行声明 inputs / outputs / run，编排器退化为通用循环；`stages/__init__.py` 的 STAGES 表与 pipeline 内部 spec 合并为单一来源。
   时机：一期加入 fallback 流水线之前完成，避免在旧结构上继续堆阶段。

2. **段落切分语义与词法工具去重。**
   chunk（环境深度感知切分）与 validate（纯空行切分）的语义差异目前靠注释维持，考虑抽出共享模块并显式命名两种切分；chunk 内约 40 行与 texlex 重复的词法逻辑合并进 texlex。

3. **命名与行文统一整改。**
   现状分两类。其一是比喻式命名：「关节」（「确定性骨架 + agent 关节」比喻的一半）贯穿 `README.md`、四个 `skill/*/SKILL.md` 的 description、标识符 `agent.JOINTS` / `prompts.JOINT_SKILLS` / `prompts.joint_version()` / `stages.*.JOINT` / `tests/test_joints.py`，以及 `report.schema.json` 的 `agent_interventions[].joint`，约 40 个文件、300+ 处；另有测试注释里指 survey 降级路径的「骨架」。其二是修辞化行文（「解毒剂」「钥匙」「双保险」「化身」「粒度哲学」一类），散见于各文档与注释。
   目标：术语统一为「agent 调用点」/ call site（`agent.SITES`、`Intervention.site`、`agent_interventions[].site`、`tests/test_agent_sites.py`）；行文以 [ARCHITECTURE.md](ARCHITECTURE.md) 为准，该文件已整改完毕，作为其余文档与注释的样板。
   注意：`report.schema.json` 的字段改名是契约变更，按架构 §5 产物契约节需先改 schema 并 bump `contract_version`；静态的调用点集合**不可**命名为 `INTERVENTIONS`，那与运行时干预记录 `PipelineResult.interventions` 冲突。
   时机：与条目 1 同批做，两者都要全仓扫动，分两次改无谓地扩大冲突面。

## 需要真实数据校准后再动的

5. **anchors 叠加次序与热区容差**（架构附录 B 开放问题 4）。
   现有常量为起步值，拿真实论文的 synctex 数据实测后定稿。

## 增量与工程效率

6. **manifest 失效粒度细化。**
   现状：`tongtu_version` 参与 freshness，任何代码改动使全部阶段重算。目标：按阶段代码 hash 判定，只失效受影响阶段及下游。

7. **fonts 分发方式。**
   50 MB 字体文件当前直接入 git 与 wheel。评估 git LFS 或构建期下载；改动需同步 `find_fonts` 查找链、docker 构建与 zh-pack 组装三处。

8. **report.html 体积。**
   为支持 file:// 双击打开，zh.pdf 以 base64 内嵌进 report-data.js（体积约 +33%），且与 http 场景的 fetch 加载并存两条路径。若产物体积成为问题，评估仅保留 `tongtu preview --serve` 路径或按需生成内嵌版。

22. **编译修复的重放快路（下一阶段）。**
    零期语义：块失效后的重编译从 unmask + inject 的新起点重来、重新拉修复会话，上次修复不保存（架构 §4，决策 A.21）。攒够真实论文的修复 trace 后：重编译先重放上次会话的确定性命令、或命中已沉淀的适配表，不拉 LLM，失败再拉会话；预期能覆盖七成左右的重编译场景，比例待 trace 数据验证。

## 测试覆盖缺口

9. **survey 成功路径的 e2e 覆盖。**
   e2e 中 MockAgent 恒等返回使 survey 始终走降级路径，真 JSON 往返只有单元测试覆盖。可为 e2e 增加一个返回合法 brief JSON 的测试 agent 变体。

10. **figures 真实渲染进 CI。**
    pdftocairo / ImageMagick 的真实渲染用例目前本机 skip、CI 的 `-m compile` 未选中。参考镜像（已含 poppler / ghostscript / ImageMagick）发布并接入 ci.yml 后，为这些用例补 `compile` 标记；同时补 EPS / JPEG 的 fixture 资产。

11. **真实论文验收**（零期验收判据 3）。
    本 PR 的开发环境无 arXiv 网络访问与 TeX，尚未用真实论文跑通全流水线。需在具备网络与 TeX 的环境执行 `tongtu run <arxiv-id> --agent codex --model <模型>` 至少一篇，核对产物包与 report.json（`--model` 是必填项：模型标识进翻译缓存 key）。

## 设计变更待落地（架构文档已改，代码未跟进）

14. **survey 通读视图纳入表格与算法。**
    架构 §3 survey 节已改：通读视图除数学类块外还要回填表格与算法（表头与行列标签是方法名 / 数据集名 / 指标名的密集来源，术语预扫要靠它们；算法体含记号与待译说明）。
    现状：`tongtu/stages/mask.py:129` 的 `SURVEY_RESTORE_CATEGORIES = frozenset({"math"})` 只回填数学类；`docs/schemas/blocks.schema.json` 的 `survey_restore` 描述仍写「表格/图/代码为 false」，与架构文档矛盾。
    改动点：常量扩为 `{"math", "table", "algorithm"}`、schema 描述同步、survey 通读视图的 golden 基线更新。
    注意：回填表格会明显增大通读输入，规模实测见架构附录 B 第 6 条。

15. **架构文档章节引用更新。**
    两轮改动使全仓的 `架构 §N` 引用失效，合并成一次扫动。
    其一：§3 的阶段小节已去掉数字编号（改为 `### mask` 形式，引用写「§3 mask 节第 N 条」）。约 20 处 `§3.1` 引用因此失效，分布在 `tongtu/texlex.py`、`stages/mask.py`、`stages/flatten.py`、`stages/figures.py`、`pipeline.py`、`data/environments.json`、`docs/schemas/blocks.schema.json`、5 个测试文件与 `tests/fixtures/README.md`；多数是细粒度的「§3.1 第 N 条」，四层机制的条目编号未变，只需改前缀。
    其二：正文按四段分组合并为 7 节 + 附录 C（2026-08-15 已在 ARCHITECTURE.md v0.9 落地，文内引用与本清单已同步），映射：

    | 旧 | 新 |
    |---|---|
    | §5 工作目录布局 | §5 工作目录布局节（并入数据面，数字碰巧未变） |
    | §6 CLI 命令面 | §6 CLI 命令面节（并入调用与运行，数字碰巧未变） |
    | §7 产物契约 | §5 产物契约节 |
    | §8 术语表 | §5 术语表节 |
    | §9 agent 适配层 | §3 agent 适配层节 |
    | §10 运行环境 | §6 运行环境节 |
    | §11 静态检验页 | §5 静态检验页节 |
    | §12 测试与 CI/CD | §7 |
    | §13 选型清单 | 附录 C |

    §1–§4 编号未变。剩余范围是代码、schema、测试、`PHASE0.md`、`README.md` 等全仓引用，`grep -rn "架构 §"` 可完整枚举；**必须按旧编号逐处判定**——§5 / §6 / §7 在新旧两版都存在但指向不同内容（旧 §7 契约 → 新 §5 产物契约节，而新 §7 是测试与 CI/CD），漏改不会断链只会错指。建议两步走：先把每处引用按旧目录展开成「§N 节名」，核对后再按本表换号；改后一律保留节名（「§5 产物契约节」），与「§3 mask 节」同一惯例，将来再挪结构不再受编号牵连。
    时机：与条目 3 同批做（同 PR、分 commit：一个 commit 纯引用扫动、一个 commit 纯改名），两者都要全仓扫动。

16. **翻译介入点⑤改用会话原语，内环交给 agent 自跑。**
    架构 §3 translate 节、agent 适配层节与附录 C 已改，决策见架构附录 A.15。目标形态：
    1. `tongtu validate` 独立成 CLI 子命令，同时作为 skill script 暴露给 agent；
    2. 介入点⑤从 `complete` 换成 `session`：脚本给出块文件与译文输出路径，agent 在会话内自跑「翻译 → validate → 修」，写完退出；
    3. 缓存查询留在拉起会话之前，保持一块一次调用（缓存粒度是块，是 §4 增量模型的根基，整篇一次会话会让增量重翻退化为全量）；
    4. 出口由脚本跑 validate 裁决，不过则该块回退原文并记 report，不退出 pipeline；
    5. `max_retries` 换成 budget。
    现状：`translate.py` 的 `translate_body` 是脚本驱动重试（`complete` + `format_errors` 喂回），`compile` 的坏段重译（`retranslate_segment`）复用同一份实现，两处一起改。
    连带：MockAgent 与 PseudoAgent 要从「`complete` 原样返回」改成模拟会话（把原文写到译文路径），§7 编译层的恒等 e2e 与伪翻译变体依赖它们。
    实测项：一块一会话的拉起开销（进程启动、系统提示词与工具定义、环境探索、每块重读 brief 与 glossary）乘以每篇 10–30 块，与同篇论文走单次调用对比墙钟时间和 token 消耗。开销过高则退回脚本驱动重试。

17. **agent trace 完整记录。**
    开发阶段的取向是不省钱：能交给 agent 的一律走 agent，不为省调用做快路径优化（例如「先编一次，过了就不拉 agent」这类优化推迟到有数据之后）。前提是每次 agent 调用的完整 trace 都留下来，供事后分析并按固化规则（架构 §2 原则 3）总结成确定性代码。
    要记的东西，形态以架构 A.18 为准（起点 hash + 命令序列 + 终态 hash，不另存 diff）：调用点、输入提示词与上下文、会话内执行过的命令及其参数与返回、退出状态、耗时与 token 消耗。转录落 `logs/`（已有），但自然语言转录总结不出统计——`report.json` 需要结构化的干预记录，固化决策才有判据。
    旁路检测同样按 A.18：重放命令序列、比对终态 hash，对不上即说明有改动没走工具面；不做会话前后的文件快照 diff。

18. **figures 保留矢量原件 + 位图，不再统一转 PNG。**
    架构决策 A.14 的论证建立在「消费者只有视觉模型与检验页」这个假设上，假设已经变了：下游还包括 markdown 与 typst 渲染，而 1568px 是 Claude 视觉 API 的数字，不该焊进产物契约。现设计对位图源还是净损失（2400px 的 PNG 被缩到 1568px，原图信息永久丢失）。
    目标规则：源是位图（png / jpg / webp）→ 原样带走，不转码不缩放，元数据记原始尺寸；源是矢量（pdf / eps）→ 转一份位图供必须吃位图的消费者，同时保留矢量原件。输出格式集合因此是 png / jpg / webp / pdf，`format` 字段已预留。矢量转位图按固定 DPI（起步 150）而非固定长边。
    SVG 暂不做：markdown 与 typst 确有需求，但 PDF→SVG 的保真度（字体转路径 vs 依赖字体可用）要拿三篇 fixture 实测 pdf2svg / mutool 之后再定。保留 PDF 原件已能覆盖需要矢量的场景。
    连带：`figures.schema.json` 的 `render` 要允许多格式与一图多表示；缓存按「源 hash → 多个产出」组织；检验页要显示各种格式（浏览器原生支持）；决策 A.14 需改写。产物包会变大，开发阶段接受。

19. **compile 改造：编译修复交给会话，脚本侧的分诊与二分退场。**
    架构 §3 compile 节已改，决策见 A.16 / A.17 / A.18。目标形态：
    1. 脚本只做确定性起点（unmask 回填、inject_cjk 注入、组装 `build/zh/`）与出口裁决（`zh.pdf` 存在、非空、页数与 baseline 相当、日志无 CJK 缺字——见条目 23）；
    2. 编译修复由介入点⑥的会话主导，agent 经 `tongtu tex` 工具面动作，不直接访问文件系统；
    3. 工具面：`read` / `patch`（导言区自由，正文须标 `--chunk`）/ `compile` / `render --page` / `fallback` / `retranslate`；
    4. metadata 只来自显式动作：`chunks.json` 的 status 增加 `edited`，动作发生时直接写，不从 `zh.tex` 反推；
    5. `zh-spans.json` 由 patch 增量维护（每次 patch 的位置与长度差已知），取代现有的「逐字节比对，不一致就不写」。
    要一并移除的现有实现：失败分诊（恒等回填判据）、块→段落两级二分定位、探测 budget、「译文错误数不超过原文」的放宽判据。连同 `compile.py` 里的对应分支一起删，不留开关。
    前提：运行时能否只给工具不给 shell，见架构附录 B 第 7 条；做不到则先上容器隔离。

20. **figures 渲染工具进 doctor，移除降级路径。**
    架构 §3 figures 节已改：pdftocairo / epstopdf 与 xelatex 同级，由 `tongtu doctor` 检查、`run` 开跑前校验，缺了报错。现有的 `PurePythonRenderer` 与 `downscale_skipped` / `missing_tool` 降级路径退为测试替身，只服务纯文本层 CI（那一层本就不跑真实渲染），不再是产品行为。
    与条目 18 同批做：位图源原样带走之后，需要外部工具的只剩矢量源。

21. **MockAgent / PseudoAgent 适配新原语形态。**
    §7 编译层是 PR 必过项，现在靠 MockAgent 的 `complete` 原样返回。两个原语的用法都变了（翻译走 `session` 写译文文件、编译走 `session` 调工具面），mock 要跟着改：翻译场景把原文写到译文路径，编译场景调一次 `tex compile` 后退出；PseudoAgent 同理。改完确认恒等 e2e 与伪翻译变体仍能跑通，否则 PR 门禁断掉。

23. **compile 出口加缺字判据。**
    架构 §3 compile 节已改（决策 A.20）：inject_cjk 在导言区显式写入 `\tracinglostchars=2`，出口脚本扫描日志的 `Missing character` 行——CJK 缺字（含全角标点）判失败；非 CJK 缺字、`Overfull \hbox` 行数与未定义引用数取相对 baseline 的增量，进 report 作 warning。
    现状：伪翻译 e2e 已有针对前缀字符的缺字断言（`tests/test_e2e_pseudo.py` 逐字符查 `There is no X in font`），生产出口没有，`\tracinglostchars` 也未注入。检查抽成一份实现，供 compile 出口与 e2e 共用；`report.schema.json` 加对应统计字段，契约变更按架构 §5 产物契约节流程。

24. **caption 译文并入 figures 元数据。**
    架构 §3 compile / figures 节与 §5 产物契约节已改：unmask 回填本就逐个判定 caption 槽位是否被翻译（未改动的回填原文），译过的顺手落一份 caption 译文中间产物，export 组装时并入 figures 元数据——包外消费者因此不必解析掩码流。figures 阶段仍只读 `src/` 与 `blocks.json`，逐图缓存不变。
    改动点：unmask 落中间产物、export 并入、figures 元数据 schema 加译文字段，契约变更按架构 §5 产物契约节流程。

25. **`chunks.json` 加 `key_version`。**
    架构 §4 与 §5 产物契约节已改：缓存 key 构成算法自身的版本号，与每条记录已存的组成要素快照配合，将来改 key 逻辑（brief 分字段参与、按要素降级匹配、非 arXiv 来源）时从要素重算新 key，翻译记忆平滑迁移而不作废（版次更新场景见架构附录 B 第 10 条）。
    改动点：`chunks.schema.json` 与 `tongtu/memory.py`，契约变更按架构 §5 产物契约节流程。

26. **`\title` 不再抽成 caption 槽位。**
    架构 §3 mask 节已改：标题保留英文原题，导言区抽出的可译槽位只剩 abstract。
    现状：`mask.py` 的 `mask_preamble` / `_preamble_slots` 把 `\title` 与前导区 abstract 都抽成 CAP 槽位。改动点：去掉 `\title` 一支，chunk 首块相关注释（`chunk.py` 模块文档「标题与摘要」的说法）与 golden 基线同步。

## 发布链路

12. **参考镜像首次构建验证**：手动触发 release-image（`push=false`）；通过后发布并把 ci.yml 编译层 `container:` 切换到 `ghcr.io/yhnocoder/tongtu`（对应 PHASE0 §3.7 未勾选项）。
13. **合并方式**：分支历史含两个会话中断产生的 WIP 快照提交，建议 squash merge。
