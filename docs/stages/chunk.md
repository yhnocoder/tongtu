# chunk —— 章节树优先分块

> 阶段定位见 [ARCHITECTURE.md](../ARCHITECTURE.md) §3 阶段表，本文是 chunk 阶段设计的权威。实现分两处：`tongtu/chunking.py` 是扫描、定级、切点与合并的实现（核心文本层，零第三方依赖，与 masking.py、glossary.py 同级），`tongtu/stages/chunk.py` 是阶段驱动器（前置条件、跳过判定、自检编排、落盘）。manifest 的字段级权威定义在 `tongtu/artifacts/chunk.py`（pydantic model，文档不复述字段表）。

**要解决的问题**：一次交给模型多少内容。chunk 小了切断节内衔接，而节内衔接是翻译质量的主要来源；chunk 大了单次生成会风格漂移、偷工减料、触及输出上限。chunk 阶段把掩码文本按章节结构切成大小受控的 chunk 序列；chunk 首尾相接拼起来逐字符等于 `masked.tex`，compile 的 backfill 与按 chunk 定位都依赖这一条。

**输入 → 输出**：`build/masked.tex` + `build/manifests/mask.json` → `build/chunks/<id>.tex`（每 chunk 一个文件）+ `build/manifests/chunk.json`。chunk 只读 `build/`、只写 `build/`：纯文本变换，不访问网络、不编译、不拉起 agent。chunk 不消费 survey 的产物，二者只共享上游 `masked.tex`（[stages/survey.md](survey.md)）；流程图里 survey 在前是 `run` 的执行序，不是数据依赖。

## 前置条件

沿用 fetch 起确立的约定：前置失败也写 manifest，驱动器不抛栈，每次执行的结论都落盘。

- mask manifest 缺失或不可解析，或状态是 ok 但 `build/masked.tex` 不在 → 状态 `mask_missing`，message 提示先跑 `tongtu stage mask`。
- mask 状态不是 ok → 状态 `mask_not_ok`，转录 mask 的状态与它记录的上游状态（pdf_only 沿链退 3，与上游各阶段同构）。

`stage chunk` 不要求 survey 已跑。

## 扫描层

一次线性扫描产出后续全部决策所需的信息：环境的 `\begin` / `\end` 位置、标题命令位置、appendix 标记位置、空行分隔符位置。词法规则与 mask 阶段共用一份实现——`masking.py` 的控制序列读取、inline math 与 `\verb` 的跳过、可选参数与花括号参数组的读取提升为模块公开函数，chunk 不另写第二份：同一条词法规则两处各写一遍，日后差一个字符就是边界错位。

掩码文本的一条性质是扫描层的前提：**凡留在掩码文本里的 `\begin` / `\end` 一律配对**。non-translatable 环境、注释、verbatim 环境、display math 在 mask 阶段已成 placeholder 或整块摘除；留下来的环境是 text 环境，而 precompile 通过保证环境正确嵌套，一半在掩码文本里、另一半被掩进 block 的形态编译不过。inline math 与 `\verb` 的内容随跳过规则整段跳过，其中的 `\begin`（如 `$…$` 里的 `pmatrix`）不参与计数。配对失败——深度转负，或文件尾深度不为零——与 mask 阶段对结构错误的处理规则相同：论文编译通过却解析不动是词法层的缺陷，直接 `chunk_failed`，不做保守回退。

## 定级与透明环境

标题层级序列固定为 `\part` > `\chapter` > `\section` > `\subsection` > `\subsubsection` > `\paragraph`，星号变体等同于无星形式，命令名按词法读出（`\appendixname`、`\sectionmark` 这类同前缀命令因此不会误判）。

**定级两步**，取出的层级称**首选层级**：

1. 所有环境都计入深度，取深度 0 处出现过的最浅层级。多数论文在这一步定级（标题命令直接写在正文顶层）。
2. 第一步取不到时（全文标题命令都在环境体内），取全文出现过的最浅层级。

**透明环境**：定级后，体内（`\begin` 与配对 `\end` 之间的字符区间，按文本判定，嵌套不影响）出现首选层级标题命令的环境标为**透明**——它只做包裹，不构成语义单元，不计入深度。以透明集重算的深度称**有效深度**，段落切分、标题边界与 appendix 标记识别都以有效深度 0 为准。驱动这条规则的实测形态：`2512.02556` 与 `2512.24880` 的正文 96–97% 被单个 `\begin{CJK*}` 包住（`CJK` 在分类表里是 text 环境，星号变体继承），全部 `\section` 与 `\appendix` 都处于深度 1；appendix 宏包的 `\begin{appendices}` 同型。按环境名逐个豁免覆盖不了下一个同型环境，按「体内含首选层级标题」判定则零维护。

**透明集在定级时求出后固定，下分沿用**。超大节向更深层级下分时不重求透明集：`2409.19606` 的 `\begin{proof}` 体内有三个 `\subsection*`，proof 不含 `\section`、不在透明集里，这三个子标题处于有效深度 1，不成为切点——定理证明不被切开。这也是「体内出现任意标题命令即透明」的更宽规则被否决的理由（决策见 ARCHITECTURE 附录 A.28）。

**兜底硬判据**：全文存在标题命令、而有效深度 0 处一个也没有 → `chunk_failed`，message 报出最外层未判透明的包裹环境名。两步定级按构造不会走到这里；这条防的是将来出现规则覆盖不到的包裹形态时得到一次失败，而不是一篇静默退化成单一巨块的译文。

**无标题退化路径**：全文一个标题命令都没有 → 整篇视为单一节点，直接按段落单元走同一套分块（见「分块算法」节），`part` 全部记 `body`。验收语料没有这个形态，规则先写明，测试期再造用例。

## 段落与切点

**段落单元**：有效深度 0 处的空行是段落分隔符；有效深度大于 0 的空行不分段（不透明环境体内的空行属于该环境所在的段落单元）。placeholder 行（`⟦BLK-n⟧`、`⟦CAP-n⟧`）是普通文本行，随空行划分归属段落单元。

**切点**两种，全部落在字符偏移上，chunk 边界只由切点序列决定：

- **标题切点**：首选层级（下分时为下分层级）标题命令自身的反斜杠偏移。不回退到所在段落单元的段首——`\section` 在 TeX 里本身终止当前段落，空行划出的段落单元在标题处不是排版意义上的段落。行内标题（前文与标题命令同段，十一篇实测两处）因此自然成为边界；标题前没有空行的形态（十一篇里八篇存在）不会把上一节的末段划进下一个 chunk。决策见 ARCHITECTURE 附录 A.29。
- **段落切点**：段落单元首行行首（无标题退化路径与段落级下分使用）。

切点之前的空白与空行归前一个 chunk 尾部。c000 从偏移 0 起，末 chunk 到文件尾。每个 chunk 文件是 `masked.tex` 的逐字符切片，**不追加任何字符，包括尾换行**；拼接恒等由构造成立，出口自检兜实现缺陷。

**区界是强制切点**：全文分 front / body / appendix 三个区，区之间不合并。front 区从文件头到第一个首选层级标题切点（仅在存在首选层级标题时存在）；appendix 区从 appendix 标记的偏移起。appendix 标记识别三条路：`\appendix` 命令、`\appendices` 命令（IEEEtran）、`\begin{appendices}` 环境（appendix 宏包），均在有效深度 0 处识别。

## 分块算法

单元 = 首选层级切点划出的节（front 区与 appendix 区内同理）。**一节就是一个 chunk**，在此之上只有两条修正，都不针对具体的命令名：

1. **单元超过 `SPLIT_ABOVE` → 沿层级序列向深层取下分层级**，以该层级的标题切点把单元切成段，各段递归走同一套规则；退完标题层级仍超过才用段落切点。
2. **段落单元仍超过 `SPLIT_ABOVE` → 独占一个 chunk，不切开**。不透明环境体内没有切点，超长的定理证明或列表整体成 chunk 是接受的终态。
3. **不足 `MERGE_BELOW` 的 chunk 与相邻 chunk 合并**：先正序一遍吸收后一个，再倒序一遍并回前一个，条件都是 `part` 相同且合并后不超过 `SPLIT_ABOVE`，倒序遍历使连续碎片级联合并。正序一遍收拾区首的碎片（`\appendix` 这类区界标记行自成一个单元，它后面才是第一节），倒序一遍收拾区尾的碎片（致谢、结论）。

chunk 大小因此落在 (0, `SPLIT_ABOVE`]，唯一例外是第 2 条的不可再分单元。实现取向：单遍递归加一个「当前层级」参数，层级序列长度天然限住递归深度，不写通用递归框架——十一篇实测 100 个顶层单元里只有 1 个超过 `SPLIT_ABOVE`，最大段落单元 732 token，第 1、2 条是罕见路径。

**不把多节攒成一个 chunk**，理由是攒了也换不到质量：一节内部的衔接本来就完整，攒相邻节只改善节间衔接，而节间衔接在论文中本来就弱，需要跨节保持一致的只有术语与记号，那由 glossary 与 brief 承担。十一篇实测，按 4000 的上限攒相邻节得到 51 个 chunk、按本节规则得到 54 个，会话次数几乎不变，而边界从「凑够 4000」变成「就是一节」：`tex fallback <chunk-id>` 回退的是一节，改一个术语失效的是一节，validate 不通过丢掉的也是一节。决策见 ARCHITECTURE 附录 A.30。

**区界的已知代价**（实测）：附录很小的论文会留下一个低于 `MERGE_BELOW` 的 appendix chunk（实测 112–847 token），因 `part` 不同合并不掉，独占一次翻译会话；front 区每篇都低于 `MERGE_BELOW`（实测 116–636 token），摘要固定独占一次会话。跨区合并零期不做：混合 chunk 会让 `part` 字段失去含义，而实测取消区界约束只把 54 个 chunk 降到 42 个，不值得拿字段语义去换。

## part 与 chunk id

`part` 三值：`front` / `body` / `appendix`，按 chunk 起始偏移对照区界判定。front 区超过 `SPLIT_ABOVE` 下分时会产出多个 front chunk——`part` 是区域属性，不与「第一个 chunk」绑定，不存在「first chunk 标记」这个独立概念。

chunk id = `c` + 三位零填充十进制序号，0 起，按文档序（`c000`、`c001`……，千位溢出自然进位为四位）。id 是位置序号，**只在同一次分块结果（manifest 的 `chunks_sha256`）下有效**：上游变化或配置校准触发重分块后，同一 id 指向不同内容，下游持有的 id 引用要连同 `chunks_sha256` 一起校验才能发现失效。

## token 估算

`estimate_tokens(text) = ceil(len(text) / CHARS_PER_TOKEN)`。`CHARS_PER_TOKEN`（4）与 `SPLIT_ABOVE`（5000）、`MERGE_BELOW`（1500）同为 `tongtu/chunking.py` 的模块级常量。选字符数除以系数而不是词数乘系数：两者都是近似，前者对 placeholder 与 LaTeX 命令没有词边界歧义，且掩码文本约九成是普通英文散文（实测 placeholder 占 1%、inline math 占 3%、控制序列占 5%），系数稳定。估算值只用于分块决策，不进缓存 key，也不做预算承诺。

系数已用真实 tokenizer 校准：拿三篇论文对同一模型发两次请求、两次只差一段附带文本，用输入 token 的差值反解出掩码文本的真实字符每 token 为 4.15–4.49（章节标题树 3.88–4.25，中文约 1.6）。取 4 因此偏保守——按 4 估算等于把 token 数算多 4%–12%，切出的块小于名义限额，方向是安全的。三个常量都参与跳过判定，改动即作废全部分块并连带失效翻译记忆（ARCHITECTURE 附录 B 第 1 条）。

## 关键取舍

- **chunk 的边界是章节边界，定位与回退单元小**，两者由段落数比对解耦。一次翻译会话看到的是完整的一节，而出错时能指认的位置细到段落。
- **`SPLIT_ABOVE` 是安全阀，不是分块目标**。它约束的是单次翻译会话的输出量（译文长度约等于原文，故上限设在输出侧），不表达「chunk 该多大」——该多大由章节结构决定。实测它在十一篇上只触发 12 次下分，正是安全阀该有的样子。
- **chunk 变大不会让回退粒度变粗**。validate 强制原译段落一一对应，任何 chunk 都可确定性拆回段落对，编译修复会话里回退原文的最小单位细到段落而不是整节（`tex fallback` 不带 `--paragraph` 时仍是整个 chunk 回退，粒度由 agent 按现场定，见 ARCHITECTURE §3 compile 节）。translate 出口 validate 失败时的回退单位零期是整个 chunk；`internal_cuts` 字段为将来按更细边界拆分重试保留了确定性切点，是否消费由 translate 设计定。
- **透明环境可以被切点跨越**。`\begin{CJK*}` 在一个 chunk、配对的 `\end` 在另一个，对流水线没有影响：拼接恒等按构造成立；backfill 是全篇拼接后 unmask；validate 的 control sequence multiset 是同一 chunk 内原译比对，不要求 chunk 自身环境平衡。「不切入环境内部」保护的是语义单元，不是括号平衡（决策见 ARCHITECTURE 附录 A.28）。

## 出口判据

同时成立才是 ok：扫描无错（`begin` / `end` 配对、深度归零）；定级兜底硬判据通过；全部 chunk 按序拼接逐字符等于 `masked.tex`；每个 chunk 段落数至少 1；chunk 文件与 manifest 落盘并通过 artifact model 校验。

## 状态与退出码

`ChunkStatus`：`ok` / `mask_missing` / `mask_not_ok` / `chunk_failed`。退出码：`ok` → 0；`mask_not_ok` 且沿链 fetch 状态是 `pdf_only` → 3（跨子命令同码同义）；其余 → 1。

## 重跑语义

- **输入 hash 与配置值**：输入 hash 是 `masked_sha256`（从 mask manifest 转录，上游输出 hash 的权威）；manifest 另记 `split_above` / `merge_below` / `chars_per_token` 三个配置值，跳过判定要求它们与当前模块常量一致——校准期这几个数会改，不参与判定的话改完常量旧分块会静默留存（与 mask 让分类表参与输入判定同理）。
- **输出 hash**：`chunks_sha256`——按文档序连接各 chunk 文件的 sha256 十六进制串再取 sha256。translate 的 stage 级「输入未变不重算」判定以它为权威；chunk 级正确性由缓存 key 里的 `norm(chunk_src)` 与 `neighbor_src` 承担（ARCHITECTURE §4），边界一变两者都变，不需要额外字段。
- **跳过判定**：chunk manifest 存在、可解析、状态 ok、输入 hash 与三个配置值与当前一致、清单内全部 chunk 文件存在 → 跳过。不校验产物内容与 manifest 是否一致（初期简化，同上游各阶段）。
- 失败状态不跳过；`--force` 无视已有结论。每次非跳过的执行开始先整目录删除 `build/chunks/`，失败时不留上次的产物误导下游。该目录只存本阶段产物，translate 的译文 chunk 不落在这里（存放位置由 translate 设计定），整目录删除因此是安全的。

## 产物模型

manifest 即 `ChunkManifest`。承担契约职责的字段：`status` 是唯一分流依据；`masked_sha256` 是输入 hash；`split_above` / `merge_below` / `chars_per_token` 是参与跳过判定的配置值；`chunks_sha256` 是输出 hash，下游输入判定的权威；`chunks` 是 chunk 记录列表；`chunks_total` 是计数；`heading_level`（首选层级命令名，null 即无标题退化路径）、`transparent_environments`（透明集环境名清单）与 `appendix_source`（`command` / `environment` / `absent`）是定级结论，观察与排查用；`mask_status` 与 `fetch_status` 转录本次看到的上游状态（退出码映射与排查用）；`warnings` 记不阻断的情形，当前只有一类——分块算法第 2 条的终态，即已下分到不可再分的单元仍超过 `SPLIT_ABOVE` 的 chunk，translate 会在这些 chunk 上撞到长生成。

`ChunkRecord` 承担契约职责的字段：`id`；`start` / `end`（在 masked.tex 解码后字符序列中的偏移，文件内容即该区间切片）；`sha256`（chunk 文件内容）；`token_estimate`；`paragraphs`（段落计数，口径见下）；`part`；`headings`（chunk 内有效深度 0 的标题，按文档序，每条含命令名 `level` 与参数原文 `argument`；排查、人工挑 chunk id，以及 translate 拼章节标题树用——摘要 chunk 的附带上下文取全文标题树，依据见 [models.md](../models.md)）；`internal_cuts`（合并进本 chunk 的各单元起始偏移列表，首项等于 `start`，未发生合并时只有这一项；translate 出口 validate 失败后按更细边界拆分重试的确定性切点来源，是否消费由 translate 设计定）；`translatable_chars`（剥除 placeholder 后的非空白字符数；纯 placeholder chunk（附录只有图表的论文会出现）要不要拉翻译会话，translate 据此判断）。

**段落计数的两个口径**：本阶段用的是**全部段落**——按空行切分、逐段剥除首尾空白、丢弃空段后计数（`tongtu/chunking.py` 的 `count_paragraphs`），manifest 的 `paragraphs` 字段与出口判据「每个 chunk 段落数至少 1」都按它算。真实语料里连续空行常见（五篇实测各 14–31 处空段），空段若计入，就是在要求译文保持同样多的连续空行，而模型合并连续空行是最常见的无害改动。

translate 的 validate 第 4 层用的是另一个口径：**含可译文本的段落**——一段剥除 placeholder（`masking.TOKEN_RE`）、`\begin{X}` 与 `\end{X}` 整体、其余控制序列的命令名（参数保留）之后，仍有非空白字符才计入。参数保留是为了让 `\section{Introduction}` 里的 Introduction 算作内容，而 `\maketitle` 剥完为空。理由是 placeholder 密集的 chunk 上全部段落口径会误判：`\end{abstract}` 之前与 `\newpage` 前后的空行对排版没有影响（`\end{…}` 自身终止段落，`\newpage` 是命令），模型吞掉这些空行按全部段落口径就是失败，而这些 chunk 里真正要拦的「两段正文被并成一段」并不存在——2409.19606 的 front chunk 全部段落 4 段、含可译文本的段落只有 1 段，2412.19437 的 front chunk 对应 7 段与 2 段。

两个口径不能互相替换。含可译文本的段落口径用在本阶段的出口判据上会把合法输入判成失败：不含可译文本的 chunk 是合法形态（附录只有图表的论文会出现，`translatable_chars` 字段就是为它设的；十一篇语料里 `1701.06538` 只含一行 `\appendix` 的 chunk 按含可译文本的段落口径算出 0 段），而出口判据要拦的是切分实现的缺陷。含可译文本的段落计数随 translate 实现时以 `count_translatable_paragraphs` 加在 `tongtu/chunking.py`，与 `count_paragraphs` 并列，两处调用方各取所需（先例：survey 的全文命中过滤与 translate 的逐 chunk 命中共用 `tongtu/glossary.py`）。

## 验收与试跑对象

十一篇（三篇自造论文 + [examples/README.md](../../examples/README.md) 真实论文表八篇）全部要求：状态 ok、按序拼接与 `masked.tex` 逐字节相等（含不追加尾换行）、重跑命中跳过、`--force` 重算。`1412.6980`（pdf_only 套壳）走 `mask_not_ok`、退出码 3。专项核对：

- `2512.02556` 与 `2512.24880`（`CJK*` 包裹整篇正文）：`heading_level` 为 `section`、`transparent_environments` 含 `CJK*`、chunk 数大于 1、`appendix_source` 为 `command`、存在 `part` 为 `appendix` 的 chunk。透明环境规则的回归判据。
- conference 篇 `\appendices` 之后的 chunk `part` 为 `appendix`（examples/README.md 已知问题第 1 条的回归）。
- `2604.15804` 与 `1701.06538` 各一处行内标题：断言成为切点。是否同时是 chunk 边界要看那一节的大小——不足 `MERGE_BELOW` 时它会与相邻 chunk 合并，切点仍留在 `internal_cuts` 里。
- article 篇 `\end{takeaway}` 紧跟 `\section` 的形态：断言 takeaway 环境整体留在前一个 chunk。
- `2409.19606`：`\begin{proof}` 体内的三个 `\subsection*` 不成为切点；inline math 里的五处 `\begin{pmatrix}` 不影响深度计数。
- 有连续空行的论文（至少五篇）：manifest 的 `paragraphs` 与段落计数函数对 chunk 文件直接计算的结果一致。
- 三篇自造论文的 chunk 边界与章节结构逐一核对；真实论文记录 token 估算分布，供附录 B 第 1 条校准。

真实论文源码不入库。
