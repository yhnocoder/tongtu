# mask —— 把不该翻译的部分换成 placeholder

> 阶段定位见 [ARCHITECTURE.md](../ARCHITECTURE.md) §3 阶段表，本文是 mask 阶段设计的权威。实现分两处：`tongtu/masking.py` 是词法状态机与 mask / unmask 的实现（核心文本层，零第三方依赖，与 `texlog.py` 同级），`tongtu/stages/mask.py` 是阶段驱动器（前置条件、跳过判定、自检编排、落盘）。manifest 与 blocks.json 的字段级权威定义在 `tongtu/artifacts/mask.py`（pydantic model，文档不复述字段表）；环境分类表在 `tongtu/data/environments.json`。

**要解决的问题**：模型有可能改坏不该改的文本——公式里的 `\alpha` 被当成词翻译、表格的 `&` 造成对齐错位、`\label` 被改名，这类错误要到编译阶段才暴露。mask 在第一次 LLM 调用之前把不该翻译的部分整块摘出去，逐字节存进 `blocks.json`，翻完原样填回；同时划定翻译范围——需要翻译的只有正文散文、caption 与摘要。产出的 `build/masked.tex` 称**掩码文本**：placeholder 与待译文本相间的单一字符序列，下游 survey、chunk、translate 都在这份序列上工作，chunk 首尾相接拼起来逐字符等于它。

**输入 → 输出**：`build/precompile.tex` + `build/manifests/precompile.json` → `build/masked.tex`（掩码文本）+ `build/blocks.json`（被摘出去的 block，artifact contract 的一员）+ `build/manifests/mask.json`。mask 只读 `build/`、只写 `build/`：纯文本变换，不访问网络、不编译，也不拉起 agent（hook③ 推迟，见「环境分类」节）。

## 前置条件

沿用 fetch 起确立的约定：前置失败也写 manifest，驱动器不抛栈，每次执行的结论都落盘。

- precompile manifest 缺失或不可解析，或状态是 ok 但 `build/precompile.tex` 不在 → 状态 `precompile_missing`，message 提示先跑 `tongtu stage precompile`。
- precompile 状态不是 ok → 状态 `precompile_not_ok`，转录 precompile 的状态与它记录的 fetch 状态（pdf_only 沿链退 3，与 flatten / precompile 同构）。

## 编码与哨兵

- 处理在解码后的字符层进行：读 bytes 按 UTF-8 严格解码，写出时编码回 UTF-8，全程不做换行规范化，偏移与比对都以字符计。解码失败 → `mask_failed`。依据：xelatex 对非法 UTF-8 输入直接报错，precompile 状态 ok 意味着这份文件已被 xelatex 接受过，实践中即保证合法 UTF-8。
- 源码本身已含 `⟦` 或 `⟧` 字符 → `mask_failed`（哨兵冲突；实测语料无此形态，真出现再考虑换哨兵，不预防性设计）。

## 掩码文本的形态

placeholder 两种：`⟦BLK-n⟧`（block）与 `⟦CAP-n⟧`（caption 槽位），n 是十进制序号，各自从 0 起按文档序递增。两种 placeholder 进入掩码文本的方式不同，这个区别是往返恒等的基础：

- **BLK 是等位替换**：placeholder 精确顶替被摘除的字符区间，不增删任何其他字符。unmask 把 token 换回 block 内容即恒等。
- **CAP 是插入行**：block token 之后插入 `"\n" + "⟦CAP-n⟧ " + 单行文本 + "\n"` 这一段完整字符；unmask 删除的正是「前导换行 + 该行 + 行尾换行」这一段。单行文本的生成规则：原始 caption 文本剥除注释、连续空白折叠为单个空格、去除首尾空白；多段 abstract 先按空行切段、逐段同样规范化、再以 ` \par ` 连接。

## 示例与翻译范围

```latex
% precompile.tex
Figure~\ref{fig:arch} shows the pipeline.
\begin{figure}[t]
  \includegraphics{arch.pdf}
  \caption{System overview.}
\end{figure}
% TODO: cite the original paper here
The loss is $\mathcal{L} = \sum_i \ell_i$.
```

```latex
% masked.tex
Figure~\ref{fig:arch} shows the pipeline.
⟦BLK-7⟧
⟦CAP-2⟧ System overview.
⟦BLK-8⟧
The loss is $\mathcal{L} = \sum_i \ell_i$.
```

- `\ref` 与 inline math 留在原文中：它们是句子的组成部分，掩掉模型就读不懂这句话了。完整性交给外层拦截（见「通用性从哪来」节）。
- 整个 figure 成 `⟦BLK-7⟧`，其中的 caption 抽出成单独一行 `⟦CAP-2⟧`——caption 是需要翻译的文本，需要被提炼出来。
- 注释同样成块（`⟦BLK-8⟧`）。

掩码同时划定翻译范围：non-translatable environment 内部的文本一律不译——表格的表头与单元格、公式里的 `\text{…}` 都随所在 block 保留原文，换来的是对齐、数学与代码不可能被改坏。preamble 整体也是一个 block，从中抽出的可译槽位只有 abstract；`\title` 不抽成槽位，标题保留英文原题——找论文、对引用都靠原题。需要翻译的只有正文散文、caption 与摘要。

## 成块对象（六类）

1. **preamble**：文件头到注释外首次出现的 `\begin{document}`（含）→ `BLK-0`。其中 `\begin{abstract}…\end{abstract}` 出现在前导区时（个别文档类要求摘要写在 `\begin{document}` 之前），环境体抽出 abstract 槽位；`\title` 不抽槽位，标题保留英文原题。注释外找不到 `\begin{document}` → `mask_failed`。
2. **non-translatable environment**：分类结论为掩码的环境（见「环境分类」节），`\begin` 起至配对 `\end` 整段成块。同名嵌套计数；体内 category 为 code 的子环境直接跳到它的 `\end`；体内注释跳过（注释里的 `\end` 不算配对）；到文件尾仍未配对 → `mask_failed`。块内 caption 抽槽位（见「caption 槽位」节）。
3. **注释**：整行注释（注释外部分为空或纯空白的行）的连续行合并为一块，块起自首行行首、含行间换行、不含末行换行，token 因此独占一行；行尾注释单独成块，`%` 起至行尾、不含换行。`\%` 与 category 为 code 的环境体内的 `%` 不是注释。
4. **display math**：`\[ … \]` 与 `$$ … $$` 整段成块，category 记 `math`。`$ … $` 与 `\( … \)` 是句子成分，留在掩码文本里；扫描遇到未转义 `$` 时看下一字符——也是 `$` 则按 display math 收块，否则跳到配对的未转义 `$` 为止（其间不做注释与环境处理，`\verb` 同理只为定位结束定界符）。
5. **元信息命令**：`\title`、`\author`、`\date`、`\affiliation`、`\email` 出现在正文时，命令起至必选参数组闭合（含前面的可选参数）整体成块，category 记 `metadata`。理由：revtex 与部分会议模板把标题、作者写在 `\begin{document}` 之后，留在掩码文本里就会被翻译，违反「标题保留英文原题」，作者与机构名同理。前导区内的这些命令随 `BLK-0` 掩码，不单独成块。清单是 `masking.py` 模块常量。
6. **postamble**：扫描中在流内遇到 `\end{document}` 时，从它起到文件尾整体成块。之后的内容 TeX 不读，也不该进翻译范围。未遇到（例如整段被别的 block 吞并）则不设此块，不判失败。

## 环境分类

**两遍式**。第一遍词法扫描做两件事：枚举全部 `\begin{X}` 环境名（环境名字符集为字母、数字与 `@`，可带尾随星号；注释里与 category 为 code 的环境体内的不计入），并收集 `\newtheorem` / `\newenvironment`（含星号与 `\renewenvironment` 变体）声明的环境名。名字全部分类完毕后，第二遍执行掩码。hook③ 将来接线时正好落在两遍之间，每个未知环境名一篇只需问一次。

分类按四级下沉，前一级给出结论即停：

1. **文档自带声明** → text（留在掩码文本里），`decided_by` 记 `newtheorem` / `newenvironment`。已知盲区：定义体展开成重环境的 `\newenvironment`（例如包装 tabular 的自定义环境）会被误留，由 validate 与编译循环兜底；实测出问题再精细化，方向是看定义体的首个命令下沉为掩码。
2. **分类表** `tongtu/data/environments.json`：环境名 → class（`text` / `non_translatable`）与 category（仅 non_translatable 需要）。种子内容取 v2 的掩码环境集与 classify SKILL 的两列清单，另补真实语料确认的 `overpic`（category `figure`）与常见散文环境（定理家族、列表家族、`ack`、`acknowledgments`、`quote` 等）。`widetext` 与 `sidenote` 有意不入表，保守默认这条路才有真实的验收用例。`decided_by` 记 `table`。
3. **hook③**（`ask` 原语按 classify SKILL 分类）：**推迟实现**。理由：`ask` 原语与适配层尚未存在；保守默认永不损坏、只降覆盖率且有记录；分类表增补是零代码维护动作。对十一篇验收语料的实测（2026-08）：套完前两级后剩余的未知环境，或嵌在已掩 block 内部（tikz 的 `scope`、threeparttable 的 `tablenotes`），或体内本就是不该翻的内容（`multicols` 包的贡献者名单、`spacing` 包的目录），保守默认零覆盖损失。重启条件 = 真实论文 manifest 的 unknown 记录里反复出现体内是可译散文的环境、靠表增补跟不上。
4. **保守默认**：整块掩码，category 记 `unknown`、`decided_by` 记 `default`。代价是该 block 不被翻译并记入 report，源码不受破坏。

星号变体继承：`X*` 在声明与分类表里都查不到时按 `X` 查（`figure*` 继承 `figure`）。`document` 是结构标记，不参与分类。text 环境的 `\begin` / `\end` 与体内内容留在掩码文本里（由 validate 的 control sequence 层保护），体内继续扫描，嵌套的 non-translatable 环境照常成块。

**category 词表**：分类表使用 `math` / `table` / `figure` / `tikz` / `code` / `algorithm` / `bibliography` / `box`，保守默认产生 `unknown`，结构性成块产生 `preamble` / `postamble` / `comment` / `metadata`（display math 记 `math`）。category 的消费方是 figures（按它取图 block）；category 为 `code` 的环境同时以 verbatim 语义扫描（体内 `%` 与 `\begin` 不解析）。

## caption 槽位

- 抽取对象：block 内的 `\caption`、`\caption*`、`\captionof{类型}`，可选参数 `[短标题]` 不抽（figures 节的配对口径同此），花括号必选参数的文本抽为 CAP 槽位，block 的 `tex` 里该参数换成 `⟦CAP-n⟧`。
- 两处不抽：category 为 code 的 block 内（`\caption` 字样是代码内容）；block 内注释里（注释掉的 caption 不该被翻译）。unknown block 照常抽，保守掩码不损失 caption 覆盖。
- revtex 惯用的 `\caption{\label{…}…}`：label 命令随文本进槽位，靠 validate 的 control sequence 层保护。
- 槽位记录所属 `block_id`；kind 两值，前导区 abstract 槽位记 `abstract`，其余记 `caption`。

## unmask 与往返自检

unmask 是本阶段自检与 compile 阶段 backfill 共用的实现，语义在本阶段定死：

1. 逐 CAP 槽位在流中找「前导换行 + 含该 token 的行 + 行尾换行」，删除这一段并取出行内文本（token 后去掉一个空格；token 前的空白容忍）。
2. 槽位回填判定：取出的文本与掩码时写出的单行形态（blocks.json 的 `masked_text`）相同 → 回填原始文本；不同 → 视为已翻译，回填该文本；流中找不到该 token 的行 → 回填原始文本并记回退。「未改动的槽位 backfill 原文」这条 compile 节已有的语义由此兑现，往返恒等也由此成立。
3. `⟦BLK-n⟧` 换回填好的 block 内容。
4. 完整性检查，任一不满足即报错：输出无残留 `⟦` `⟧`；每个 block 的 token 恰好使用一次；每个 CAP token 在流中至多出现一次；流中出现 blocks.json 没有的 token。

**自检**：mask 落盘前对未翻译的掩码文本跑 unmask，与 `precompile.tex` 全文逐字符比对，不等 → `mask_failed`，message 报首处差异的字符偏移与两侧上下文摘录。词法遍历中的结构错误（配对不上的环境、不平衡的花括号）不做保守回退，直接 `mask_failed`——论文编译通过却解析不动，是词法状态机的缺陷，要在第一次 LLM 调用之前暴露。

## 通用性从哪来

论文之间的 LaTeX 写法差异很大，mask 的可靠性不依赖解析规则覆盖全部情况，而由四层机制承担：

1. **词法解析**。LaTeX 的花括号嵌套与 verbatim 语义超出正则表达能力，mask 用词法状态机识别环境与分组（解析器选型见 [ARCHITECTURE.md](../ARCHITECTURE.md) 附录 C）。
2. **枚举完备，分类保守**。词法扫描可以完备枚举文档中出现的全部 `\begin{X}` 环境名，这一步不需要先验知识；分类才需要知识，按「环境分类」节的四级下沉，末级的保守默认永不损坏源码，只降覆盖率且有记录。hook③ 接线后，agent 的分类结论按固化规则记入分类表（ARCHITECTURE.md §2 原则 3）。
3. **运行时往返自检**。「unmask 与往返自检」节的自检对每篇论文都执行，解析缺陷在第一次 LLM 调用之前暴露。
4. **外层还有两道拦截**。即便有遗漏，validate 的 control sequence multiset 比对与编译循环仍会拦下，最后的 safety net 是回退原文。

**不设强制的「agent 复核掩码结果」步骤**：那是「自我验证不可信」的变体（复核通过的效力等同于「我检查过了」），且逐篇付费。脚本只在环境分类这一处需要判断的地方拉起 agent（hook③，推迟实现见「环境分类」节）。

## 出口判据

三条同时成立才是 ok：词法遍历无错（含解码与哨兵检查）；往返自检逐字符恒等；`masked.tex` 与 `blocks.json` 落盘。阶段表里「`blocks.json` 完整」由自检的完整性检查蕴含。

## 状态与退出码

`MaskStatus`：`ok` / `precompile_missing` / `precompile_not_ok` / `mask_failed`。退出码：`ok` → 0；`precompile_not_ok` 且沿链 fetch 状态是 `pdf_only` → 3（跨子命令同码同义）；其余 → 1。

## 重跑语义

- **输入 hash 是两个值**：`precompile_sha256`（从 precompile manifest 转录，上游输出 hash 的权威）与 `environments_table_sha256`（分类表文件内容的 sha256）。表也参与的理由：重建期分类表会频繁增补，不参与跳过判定的话，改表之后旧的掩码结果会静默留存。
- **输出 hash**：`masked_sha256` 与 `blocks_sha256` 都记，都是下游判定「输入未变不重算」的权威——改分类表可能只改 blocks.json 的 category 而不动掩码文本，而 figures 消费 category、survey 消费 caption 槽位（abstract 照录）。
- **跳过判定**：mask manifest 存在、可解析、状态 ok、两个输入 hash 与当前值一致、`build/masked.tex` 与 `build/blocks.json` 都存在 → 跳过。不校验产物内容与 manifest 是否一致（初期简化，同 fetch / flatten / precompile）。
- 失败状态不跳过；`--force` 无视已有结论。每次非跳过的执行开始先删除已有的 `masked.tex` 与 `blocks.json`，失败时不留上次的产物误导下游。

## 产物模型

manifest 即 `MaskManifest`。承担契约职责的字段：`status` 是唯一分流依据；`precompile_sha256` 与 `environments_table_sha256` 是本阶段的输入 hash；`masked_sha256` / `masked_bytes` 与 `blocks_sha256` 是输出 hash，下游输入判定的权威；`environments` 是环境分类结论一览（环境名 → class / decided_by / 枚举出现次数 / 实际成块数——嵌在已掩 block 内部的未知环境成块数为 0，将来 hook③ 只对成块数大于 0 的未知环境提问），report 的 hook 干预统计与固化判据取自这里；`blocks_total` / `captions_total` 是两类记录的计数；`masked_chars_ratio` 是掩码文本占原文的字符比（观察值）；`precompile_status` 与 `fetch_status` 转录本次看到的上游状态（退出码映射与排查用）。

`blocks.json` 即 `BlocksFile`，`blocks` 与 `captions` 两个列表。BlockRecord 承担契约职责的字段：`id`（`BLK-7` 形，token 由它拼出）；`category`；`environment`（环境名，结构性 block 为空）；`decided_by`（结构性 block 为空）；`labels`（block 内 `\label` 的参数清单）；`tex`——**带 CAP 槽位的形式**，原始文本可由槽位处代入 caption 原文重建，backfill 与 figures 的配对正需要槽位形式；`start` / `end`（在 precompile.tex 解码后字符序列中的偏移）；`line`（起始行号，1 起，排查用）。CaptionRecord：`id`（`CAP-2` 形）；`block_id`；`kind`（`caption` / `abstract`）;`tex`（原始文本）；`masked_text`（掩码文本里的单行形态，unmask 回填判定的比较基准）。

## 验收与试跑对象

十一篇（三篇自造论文 + [examples/README.md](../../examples/README.md) 真实论文表八篇）全部要求：状态 ok（往返自检恒等蕴含其中）、重跑命中跳过、`--force` 重算。`1412.6980`（pdf_only 套壳）走 `precompile_not_ok`、退出码 3。专项核对：revtex 的 `widetext` 与 conference 的 `sidenote` 记 category `unknown`、decided_by `default`；article 的定理环境与声明的自定义环境不成块（manifest `environments` 记 decided_by `newtheorem` / `newenvironment`）；revtex 正文的 `\title` / `\author` 成 metadata block、掩码文本中不再出现原题文本；三篇自造论文的 caption 槽位数与源码图表数吻合；`2106.04426` 的 `scope` / `pgfonlayer` 在 `environments` 里成块数为 0；`2002.05202` 的掩码保留比例与 v2 既往结果同量级。真实论文源码不入库。
