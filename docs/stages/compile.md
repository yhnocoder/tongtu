# compile —— 从译文 chunk 编到出中文 PDF

> 阶段定位见 [ARCHITECTURE.md](../ARCHITECTURE.md) §3 阶段表，本文是 compile 阶段设计的权威。实现分两处：`tongtu/inject_cjk.py` 是 preamble 注入（核心文本层，与 masking.py、chunking.py、validation.py 同级），`tongtu/stages/compile.py` 是阶段驱动器（backfill、编译树组装、会话拉起、终审、落盘）。manifest 的字段级权威定义在 `tongtu/artifacts/compile.py`（pydantic model，文档不复述字段表）；documentclass 适配表在 `tongtu/data/documentclass.json`。

**要解决的问题**：译文是掩码文本，不能直接编译；英文 preamble 排不出中文；而编译错误的种类是开放的，package 冲突、字体缺失、float 溢出、documentclass 不认识的选项，每篇论文遇到的都不一样。更麻烦的是「编译通过」不等于「排得对」：缺字形、图跑飞、双栏串行、断行难看，这些只有看渲染结果才知道。

分四段：脚本把译文 chunk 还原成完整 TeX（backfill），脚本注入中文排版所需的 preamble（inject_cjk），编译一次并跑出口判据，不过则拉起 agent 会话修复，最后由脚本独立终审。会话恰拉起一次，agent 的自述不作数。

**输入 → 输出**：

```
build/translated/<id>.tex   译文 chunk，translate 产出
build/chunks/<id>.tex       原文掩码切片，哨兵校验用
build/blocks.json           block 与 caption 记录，mask 产出
build/manifests/{translate,chunk,mask,precompile}.json
src/                        资源树：.cls / .sty / 图源 / .bib
fonts/                      仓库内的霞鹜文楷字体文件
        │
        ▼
build/zh-raw.tex            backfill 产物，inject 之前，含哨兵
build/zh/                   编译树：src/ 拷贝 + fonts/ + zh.tex + 编译副产物
build/zh/zh.pdf             中文 PDF
build/zh/zh.synctex.gz      源码行与 PDF 坐标的映射，export 合成 anchors 时消费
build/captions-zh.json      caption 译文，export 并入 figures 元数据
build/manifests/compile.json
logs/compile-session.jsonl  会话 transcript
```

compile 只读 `src/`、`build/` 与仓库内的 `fonts/`，只写 `build/` 与 `logs/`。`out/` 由 export 组装，本阶段不碰。

## 前置条件

沿用 fetch 起确立的约定：前置失败也写 manifest，驱动器不抛栈，每次执行的结论都落盘。

- translate manifest 缺失或不可解析，或状态是 ok 但任一译文文件缺失 → 状态 `translate_missing`。
- translate 状态不是 ok → 状态 `translate_not_ok`，转录上游状态。
- mask manifest 缺失或不可解析，或 `build/blocks.json` 不在 → 状态 `mask_missing`。
- precompile manifest 缺失、不可解析，或其中没有页数基线 → 状态 `precompile_missing`。出口判据要拿页数基线作参照系，没有基线就没有判据。

## backfill

把译文 chunk 拼回完整 TeX，共用 mask 阶段定死的 `masking.unmask` 实现（语义见 [stages/mask.md](mask.md)「unmask 与往返自检」节）。

```
按文档序读 build/translated/<id>.tex
        → 在相邻 chunk 之间插入一行边界哨兵
        → 拼成完整的译文掩码流
        → unmask(流, blocks, captions)
        → build/zh-raw.tex
```

`unmask` 的返回值直接给出两样下游要的东西，不需要另写提取逻辑：

- `translated` 是「caption id → 译文」的映射，落 `build/captions-zh.json`，export 并入 figures 元数据。
- `fallbacks` 是流中找不到 CAP token、按原文回填的 caption 清单。**零期这份清单非空即判 `backfill_failed`**：CAP token 丢失本该被 translate 的 placeholder 校验层拦下，走到这里说明上游漏了，按原文回填会把这个漏洞盖住（见下「零期不实现 fallback」节）。

### chunk 边界哨兵

拼接时在相邻 chunk 之间插入一行 LaTeX 注释：

```
%%tongtu chunk c012
```

它承担两件事：终审通过后重扫一遍 `zh.tex` 即得每个 chunk 的行区间；哨兵之间的文本与 `zh-raw.tex` 的对应区间比对，即可判定哪些 chunk 被会话改动过。

- **只插在 chunk 之间，不插在文件开头**。N 个 chunk 用 N−1 个哨兵：第一个 chunk 的区间从文件开头到第一个哨兵，起点已知。这样避开首行必须是 `%&format` 一类 TeX 指令的形态。
- **哨兵是普通文本行，unmask 不处理它**。它既不是 placeholder，也不落在 CAP 插入行上——CAP 行紧跟在 BLK token 之后、中间只有单个换行，属于同一段落单元，而 chunk 切点只落在标题命令偏移或段落单元首行行首（[stages/chunk.md](chunk.md) 段落与切点节），两者不会重合。
- **整行注释不产生排版输出**。TeX 读到 `%` 跳到行尾并吞掉换行符，整行注释在段落之间不改变段落结构。
- **哨兵前后各补一个换行**，保证它独占一行。多出的空行在段落之间无害，连续空行等同单个 `\par`。

**backfill 自检**，任一不成立即 `backfill_failed`：`unmask` 自身的完整性检查通过；`fallbacks` 为空；哨兵数量等于 chunk 数减一；哨兵顺序与 chunk id 的文档序一致；每个哨兵独占一行。

## inject_cjk

在 `zh-raw.tex` 的 `\begin{document}` 之前插入中文排版所需的 preamble，产出编译树里的 `zh.tex`。注入点的定位复用 `masking.py` 已有的「找注释外首次出现的 `\begin{document}`」实现。

选 preamble 末尾而不是 `\documentclass` 之后：论文自己的宏包都已加载完，xeCJK 的字体设置不会被后加载的包推翻。

```latex
\usepackage{xeCJK}
\xeCJKsetup{CJKmath=true}
\setCJKmainfont{LXGWWenKai-Light.ttf}[Path=fonts/, BoldFont=LXGWWenKai-Medium.ttf]
\tracinglostchars=2
```

- **字体拷进编译树，用相对路径引用**。artifact contract 里 `zh.tex` 的定义是自包含的 pack，用绝对路径引用仓库内的字体会让它换一台机器就编不了。字体文件是否随 artifact package 分发，留给 export 决定。
- **font fallback chain 零期只配文楷两个字重**。生僻字缺字形会被出口判据直接抓出来，届时按实际缺的字决定补什么，不预防性配置覆盖全部字符集的链。
- **`\tracinglostchars=2` 显式写入**，不依赖 LaTeX 内核版本的默认值——缺字形判据建立在它之上。
- **documentclass 适配表 `tongtu/data/documentclass.json` 零期是空表加一份 schema**（documentclass 名 → 追加或替换的注入片段）。条目只从真实论文的会话成果沉淀（ARCHITECTURE §2 原则 3），不预先填写猜测的适配规则——这一点与 `environments.json` 不同，那张表有 v2 的既往清单可继承，这张没有。

## 编译树

与 precompile 同构：把 `src/` 内容全量拷进 `build/zh/`，把仓库 `fonts/` 下的字体文件拷进 `build/zh/fonts/`，把注入后的文本写为 `build/zh/zh.tex`，cwd 设在 `build/zh/` 编译。宏包与图按相对路径就位，走 bibtex 的论文 latexmk 直接处理，副产物全部落在 `build/zh/`，`src/` 保持只读。

precompile 的修复会话若改过 `flat.tex` 之外的文件（如 `.sty`），那些改动不传播到这里（[stages/precompile.md](precompile.md)「改动传播的边界」节已写明），到这边要重做。已观测的失败模式都只需改 flat.tex。

```
latexmk -xelatex -interaction=nonstopmode -synctex=1 zh.tex
```

比 precompile 只多一个 `-synctex=1`：产出 `zh.synctex.gz`，记录源码行号与 PDF 页码矩形的映射，是 export 合成 `anchors.json` 时的首选来源。precompile 不加它，它不影响页数基线。

单次 latexmk 超时 600 秒，与 precompile 同值（起步值，按真实论文校准）；latexmk 会派生 xelatex 子进程，超时要杀整个进程组。latexmk 不在 PATH → `compile_failed`，message 说明需要安装 TeX 发行版。

## 首次编译与出口判据

编译一次，跑一遍出口判据。**全部通过则不拉会话，直接进入产物写出**——多数论文走这条路，零模型成本。任一不过则拉起修复会话。

### 四条硬判据

| 判据 | 口径 | 不过的状态 |
|---|---|---|
| 编译退出码 | latexmk 退出码为 0 | `compile_failed` |
| PDF | `zh.pdf` 存在且非空 | `compile_failed` |
| 页数 | 与 precompile 基线之比落在 `[PAGE_RATIO_MIN, PAGE_RATIO_MAX]`（`[0.7, 1.3]`，模块级常量） | `page_check_failed` |
| CJK 缺字形 | `zh.log` 中报的字符落在 CJK 区段（含全角标点）的 `Missing character` 行数为 0 | `glyph_check_failed` |
| 正文控制序列 | 相对 `zh-raw.tex` 正文段的 multiset **只许增不许减** | `content_check_failed` |

**页数取宽区间**：它是安全阀不是质量指标，要拦的是译文丢了半篇、编出一页空白这类事故，不是排版比原文紧凑。中文译文通常比英文原文短，比值的实际分布用真实论文校准。

**CJK 缺字形是绝对零，不减基线**。precompile 的基线 `missing_characters` 可能非零（原文里有中文字符而当时没有 CJK 字体，例如 CJKutf8 机制被修复会话摘除之后），但那些字符在本阶段注入 xeCJK 之后就应该正常渲染，precompile.md 的基线数据节已写明这一点。译文里的中文字符缺字形只有一个原因：font fallback chain 没接上，出来的 PDF 必然带缺字形方块，而页数判据看不见它。

判据需要 `texlog.py` 补一处实现：现在只数 `Missing character` 的总行数，需要读出那一行报的是哪个字符（日志形态 `Missing character: There is no X in font …`），按字符是否落在 CJK 区段分成两个计数。这份实现与 CI 编译层 pseudo-translation 的缺字形断言共用一份代码，与 validate 有多个调用方而只有一份实现的做法相同。

**正文控制序列只许增不许减**：比对基准是 `zh-raw.tex` 的正文段（`\begin{document}` 之后，内容完全确定），比对对象是当前 `zh.tex` 的正文段，用的是 `tongtu/validation.py` 已有的 control sequence multiset 实现——validate 的调用方因此从两个变成三个。preamble 段排除在外，那里允许自由改。差集的方向有意义：增加是正常的修复动作（`\clearpage`、`\resizebox`、`\raggedbottom`），**减少是内容丢失的信号**——注释掉一个 `\includegraphics`、删掉一个编不过的 `figure` 环境、砍掉一段带问题的正文，这些动作会让编译退出码、PDF 非空、CJK 计数三条判据全绿而 PDF 缺内容。这是其余判据看不见的唯一一类静默失败，零期设为硬门（理由见下「零期不实现 fallback」节）。首次编译时基准与对象逐字节相同，这条判据恒过；它实际生效在会话之后的终审。

### 三类相对增量，记 warning 不设门

非 CJK 缺字形、`Overfull \hbox` 行数、未定义引用与未定义引文，都取相对 precompile 基线的增量，进 manifest 的 warnings 与 report。「多难看才算坏」没有机械答案，这部分靠会话内 agent 看渲染页。

## 修复会话

首次编译或出口判据不过，即拉起恰一次 agent 修复会话（ARCHITECTURE §3 阶段表的 hook⑥）。会话内 agent 自行迭代（改、编、看日志、渲染页看排版），改到它认为可以为止。

### 会话现场

cwd 是编译树 `build/zh/`，agent 可自由读写树内文件、执行命令，与 precompile 的修复会话同形态。**零期不做权限隔离**：树整体可丢，改坏了重跑一次 backfill 即复原。

原设计（ARCHITECTURE 附录 A.17 / A.18）是 agent 经 `tongtu tex` 一组 CLI 子命令读写 `zh.tex`、按区域分区权限、metadata 只来自显式动作，配套可重放的 command sequence 与 bypass detection。零期不实施，理由有四：

1. 它的前提在附录 B.7 里本就是待验证项——agent 要调用命令就得有 Bash，拿到通用 shell 之后它可以绕开命令直接改文件，命令集只剩记账作用；走那条路要先把适配层的 `ALLOWED_TOOLS` 参数化，再验证命令前缀规则能否拦住命令拼接与替换。
2. precompile 已经走通了相反的路线（不建命令集、给 cwd 自由读写、脚本终审加与 `src/` 逐文件 hash 比对）。
3. 原设计买到的三样东西里，chunk 的行区间与「哪些 chunk 被改过」由哨兵得到，只剩语义区分与 bypass detection 拿不到。
4. **命令集防不住真正的风险**。agent 用 `tex patch --chunk` 把一段删掉，`chunks.json` 会记下这个 chunk 状态是 `edited`，内容照样没了，只是删得有记录。命令集买到的是记账精度，而拦住内容丢失靠的是出口判据——判据由脚本在终审时跑，与 agent 有没有命令集无关。这与 ARCHITECTURE §2 原则 1 一致：验证在出口，不在过程。

**代价明确记下**：A.17 的论证「文本差异记得住改了什么，记不住这个改动意味着什么」仍然成立，零期 chunk 的会话侧状态只到 `edited`（被改过）这个粒度；trace 只有 transcript，不是可重放的 command sequence，A.18 的 bypass detection 与 A.21 里「攒够 trace 后重放确定性修复」的后续路线一并推迟。

### 会话可调用的一条命令

```
tongtu tex compile        # 在 cwd 的编译树内编译一次，返回退出码、错误行摘录与日志路径
```

保留它的理由不是权限，是**参数统一**：会话编译与脚本终审必须是同一条 latexmk 命令（同引擎、同超时、同 synctex 开关），agent 自己拼参数可能用 pdflatex 或漏掉 `-synctex=1`，那样终审与会话看到的不是同一个编译。参数写在脚本里而不是编译树内的 `.latexmkrc`，正是因为后者 agent 能改。

原设计的其余五条命令全部删除：`tex read` / `tex patch` 由 agent 自带的 Read / Edit 承担；`tex render --page N` 由 agent 直接调 `pdftocairo -png -f N -l N zh.pdf page` 再读图承担（`pdftocairo` 已是 `tongtu doctor` 的检查项）；`tex retranslate` 与 `tex fallback` 属于回退路径，零期不实现（见下节）。

### prompt 与预算

- **prompt 资产**：`skill/compile/SKILL.md`，形态照 [`skill/precompile/SKILL.md`](../../skill/precompile/SKILL.md)——任务说明与约束加已观测失败模式的 example，新模式的沉淀方式就是往里加 example，实测抓到才加，不预防性扩张。驱动器拼上本篇的 `zh.log` 错误行摘录与出口判据的当前结论。
- **核心约束**（写进 SKILL.md）：只改树内文件；优先只改 preamble；不改动正文的文字内容；不删哨兵行；**不删正文的任何控制序列**——译文本身有结构错误导致编不过时，如实报告并停止，不要靠删内容换编译通过。
- **运行时**：`work` 原语经 agent 适配层（`tongtu/agent/`）拉起，首发运行时 Claude Code CLI，headless。模型默认 `claude-sonnet-5`、reasoning effort xhigh，`--model` 透传覆盖。会话 transcript 原样落 `logs/`。
- **预算**：会话轮数上限 30、墙钟超时上限 900 秒（模块级常量），与 precompile 同值。编译次数不单独设限，它被轮数蕴含。超限即终止会话，但不直接判失败——会话可能在超限之前已把问题修完，结论仍由脚本终审给出。

## 脚本终审

会话结束后脚本自己验证：`latexmk -C` 清理编译产物，再跑一遍自己的 latexmk，出口判据全部取自这一遍。伪造的 `zh.pdf` 或 `zh.log` 在清理这一步即被删除，改过的编译参数也不起作用——终审用的是脚本自己的命令行。

终审通过后扫一遍 `zh.tex` 的哨兵，得到每个 chunk 的行区间，与 `zh-raw.tex` 的对应区间比对，判出 `edited_chunks`。扫描放在终审之后一次做完：注入与会话改动都已尘埃落定，不做增量维护。

哨兵被会话删掉时**降级不阻断**：该 chunk 的区间标记为未知，记 warning。PDF 已经编出来了，缺的只是 export 合成索引时的一项输入；哨兵大面积缺失本身即是排查线索。

## 零期不实现 fallback

**凡是回退到原文的路径，零期一律不实现**。这不是取消 fallback 这个机制，而是在调试期把问题暴露出来：

| 原设计的回退路径 | 零期 |
|---|---|
| `tex fallback <chunk-id>`，会话把某个 chunk 回退成原文 | 不实现，命令不建；配套的编译树内预置原文也不建 |
| `tex retranslate <chunk-id>`，会话内重译一次 | 不实现，需要时再加 |
| `unmask` 的 caption 原文回填（`fallbacks` 非空） | 视为 `backfill_failed`，不按原文回填后继续 |
| 正文控制序列减少 | 硬门 `content_check_failed`，不记 warning 放行 |

理由：这些路径的共同作用是**让一次有问题的执行仍然产出 PDF**，而调试期需要的恰好相反——译文丢了一个 placeholder、agent 删了一张图、某段译文编不过，都应该当场失败并指出位置，而不是安静地退化成英文原文然后出一份看起来正常的 PDF。ARCHITECTURE §3 compile 节「保证总能出 PDF」这条要求因此在零期让位；等三篇自造论文与八篇真实论文的失败模式看清楚之后再逐条加回来，那时每一条回退都有具体的触发场景作依据。

translate 阶段的 chunk 级 fallback 与 `FALLBACK_RATIO_MAX` 不在本条范围内，它们由 [stages/translate.md](translate.md) 自己决定。

## 状态与退出码

`CompileStatus`：`ok` / `translate_missing` / `translate_not_ok` / `mask_missing` / `precompile_missing` / `backfill_failed` / `compile_failed` / `page_check_failed` / `glyph_check_failed` / `content_check_failed`。

四个检查失败的状态分开而不合成一个，是因为它们的排查方向完全不同：`compile_failed` 看日志错误行，`page_check_failed` 看内容是否丢失或排版异常，`glyph_check_failed` 看字体注入，`content_check_failed` 看会话删了什么。

`compile_failed` 覆盖：latexmk 不在 PATH、编译超时、agent 运行时不可用、会话超预算、终审仍编不过，message 区分。

退出码：`ok` → 0；上游状态沿链且源头是 `pdf_only` → 3（跨子命令同码同义）；其余 → 1。`tongtu tex compile` 沿用检查类命令的谓词惯例：编译通过退 0，编译失败退 1。

## 重跑语义

- **输入 hash 三个**：`translated_sha256`（从 translate manifest 转录）、`blocks_sha256`（从 mask manifest 转录）、`documentclass_table_sha256`（适配表文件内容的 sha256）。适配表参与判定的理由同 mask 让环境分类表参与：重建期这张表会频繁增补，不参与的话改表之后旧的编译结果会静默留存。
- **输出 hash**：`zh_sha256`（`build/zh/zh.tex` 的内容 hash），export 组装 artifact package 时引用的权威记录。
- **跳过判定**：compile manifest 存在、可解析、状态 ok、三个输入 hash 一致、`build/zh/zh.tex` 与 `build/zh/zh.pdf` 都存在 → 跳过。**跳过命中时不重拉会话**——这是 ARCHITECTURE 附录 A.21「重编译等于重新拉起修复会话」的边界：输入变了才重来，没变就用上次的成果。
- 失败状态不跳过；`--force` 无视已有结论。每次非跳过的执行开始先整目录删除 `build/zh/`，并删除 `build/zh-raw.tex` 与 `build/captions-zh.json`：旧的 aux 文件会污染重编结果，失败时也不留上次的产物误导下游。

## 产物模型

manifest 即 `CompileManifest`。承担契约职责的字段：

| 字段 | 作用 |
|---|---|
| `status` | 唯一分流依据 |
| `translated_sha256` / `blocks_sha256` / `documentclass_table_sha256` | 本阶段的输入 hash |
| `zh_sha256` | 输出 hash，下游输入判定的权威 |
| `pages` / `baseline_pages` / `page_ratio` | 页数判据的实际取值与参照 |
| `cjk_missing_characters` | CJK 缺字形判据的实际计数 |
| `removed_control_sequences` | 正文控制序列比对中减少的部分，`content_check_failed` 的现场 |
| `non_cjk_missing_delta` / `overfull_delta` / `undefined_reference_delta` / `undefined_citation_delta` | 相对 precompile 基线的增量，report warning 的来源 |
| `fix_session` / `session_stop_reason` / `session_model` / `session_duration_seconds` | 本次是否拉过修复会话与会话结局，report 的 hook 干预统计取自这里 |
| `documentclass` | 从 preamble 解析出的 documentclass 名，与适配表是否命中 |
| `chunk_spans` | 哨兵扫描得到的每个 chunk 的行区间，含 `edited` 标记；哨兵缺失的 chunk 区间记为未知 |
| `edited_chunks` | 被会话改动过的 chunk id 清单，是 `chunk_spans` 的摘要 |
| `translate_status` / `mask_status` / `fetch_status` | 转录本次看到的上游状态，退出码映射与排查用 |
| `command` | 终审那次的 latexmk 命令行 |
| `pdf_bytes` / `duration_seconds` | 产物规模与终审编译耗时，排查与超时校准 |
| `warnings` | 哨兵缺失、三类相对增量、字体拷贝异常等不阻断的情形 |

**不落 `zh-spans.json` 这类独立的位置产物**。它要记的四样东西——block 在 `zh.tex` 的行号、chunk 边界行号、标题命令行号、哪些 chunk 被改过——没有一样是只有 compile 知道的：前三样 export 从 `zh.tex` 与 `blocks.json` 扫得出来，第四样从 `zh-raw.tex` 与 `zh.tex` 的哨兵区间比对得出，而这两份文件都在 `build/` 里。另落一份盘只多出一处需要与 `zh.tex` 保持一致的拷贝，理由与 ARCHITECTURE 附录 A.18 否决「另存 diff」相同。`chunk_spans` 留在 manifest 里是因为终审本来就要扫哨兵，这份数据一并得到，且会话干预统计是 manifest 的既有职责。

compile 独有、export 事后补不了的只有两件事，本阶段必须保证：**哨兵在 backfill 时写进 `zh.tex`**（事后无法补插，那时已不知道 chunk 边界在哪），**`zh-raw.tex` 落盘**（backfill 后、inject 前的快照，是 `edited` 判定唯一的比对基准）。

**本阶段不写 `chunks.json`**（artifact contract 里的翻译记忆）。零期它根本不产出（ARCHITECTURE 附录 A.33），将来产出时由 export 组装，来源是 translate manifest 的逐 chunk status 与本 manifest 的 `edited_chunks`。

## 关键取舍

- **会话拿 cwd 自由读写，不做权限隔离**。与 precompile 一致。原设计的分区权限与命令记账推迟，理由与代价见上文「会话现场」节，核心判断是：命令集买到的是记账精度，而拦住内容丢失靠的是出口判据。
- **chunk 边界用注释哨兵标记**。一行 LaTeX 注释同时解决 chunk 行区间与 `edited` 判定，不需要在每次改动时增量维护区间。代价是哨兵可能被会话删掉，按降级处理不阻断。
- **会话只留一条命令**。保留 `tex compile` 是为参数统一，且参数留在脚本里而不是编译树内的配置文件——后者 agent 能改。读写文件、看日志、渲染页都用 agent 自带的工具与 PATH 上的工具。
- **正文控制序列只许增不许减，设为硬门**。这是其余四条判据都看不见的一类静默失败：删内容能让编译通过、PDF 非空、CJK 计数为零、页数仍在区间内。设硬门而不是记 warning，是零期「暴露问题优先」的一部分；误报的代价是多看一眼，漏报的代价是产出一份缺内容却判成功的 PDF。合法删除（例如删掉一个引起 overfull 的 `\vspace`）触发误报时，把该命令收进豁免清单，清单从实测中长出来，不预先猜测。
- **CJK 缺字形是绝对零，其余排版维度只记 warning**。A.20 的论证不变：CJK 缺字形只有 font fallback chain 没接上一个原因，机械可查；「多难看才算坏」没有机械答案，那部分靠会话内 agent 看渲染页。
- **页数判据取宽区间**。它是安全阀不是质量指标。
- **适配表零期是空表加 schema**。条目只从真实论文的会话成果沉淀，不预先填写猜测的适配规则。
- **不建确定性的编译错误修复规则集**。沿用 A.16 与 precompile 的结论：编译错误是开放集合，为每一类写 triage 规则没有尽头，而 example 进 prompt 后新模式的维护动作是零代码。
- **零期不实现任何回退路径**。见上文专节。

## 验收与试跑对象

### 编译层（无模型成本，合并必过）

三篇自造论文（article / revtex / 双栏会议）两种走法：

- **identity translation**：译文即原文。要求状态 ok、`fix_session` 为 false、CJK 缺字形计数为 0（原文没有中文字符）、页数比接近 1、重跑命中跳过、`--force` 重算。这一路验证 backfill、注入与编译链路本身。
- **pseudo-translation**：可译文本替换成固定中文串（ARCHITECTURE 附录 B.2 已定的中文路径覆盖方式）。要求状态 ok、CJK 缺字形计数为 0。这一路是**唯一能真正验证字体注入与缺字形判据的用例**——identity translation 里没有中文字符，该判据恒真。

专项核对：三篇的哨兵数量等于 chunk 数减一且全部存活；`chunk_spans` 的行区间首尾相接、覆盖全文；`captions-zh.json` 的条目数与三篇的 caption 槽位数吻合；revtex 与双栏会议两篇确认 xeCJK 注入不与 documentclass 冲突（若冲突，其解法即适配表的第一批条目）。

故障注入专项（验证零期的硬门确实生效）：在 identity translation 的产物上手工删掉一个 `\includegraphics` 再跑终审，要求 `content_check_failed`；手工删掉一个 CAP token 再跑 backfill，要求 `backfill_failed`。

### LLM 层（手动触发，计费）

[examples/README.md](../../examples/README.md) 真实论文表八篇的真实译文编译。观测项：会话触发率与轮数分布（校准预算上限 30 / 900）、页数比的实际分布（校准 `[0.7, 1.3]`）、`edited_chunks` 的规模与 `removed_control_sequences` 的实际内容（判断硬门的误报率，以及 A.17 的语义区分零期缺失是否够用）。`1412.6980`（pdf_only 套壳）沿链走 `translate_not_ok`、退出码 3。真实论文源码不入库。
