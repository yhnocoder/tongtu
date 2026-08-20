# precompile —— 把原文编译到通过，产出下游输入与基线数据

> 阶段定位见 [ARCHITECTURE.md](../ARCHITECTURE.md) §3 阶段表，本文是 precompile 阶段设计的权威。实现在 `tongtu/stages/precompile.py`；manifest 的字段级权威定义在 `tongtu/artifacts/precompile.py`（pydantic model，文档不复述字段表）。阶段名连写：阶段名同时是 CLI 枚举值、Python 模块名与 manifest 文件名，带连字符做不了 Python 标识符，三处名字会分裂。

**要解决的问题**：翻译改坏源码导致的编译失败，与论文本身在本机 toolchain 下编不过，是两类问题，混在一起就无从归因。precompile 在任何翻译动作之前把原文编译到通过：首次编译失败不终止，而是拉起 agent 修复会话，把源码与引擎的不匹配（pdftex 专有原语、pdflatex 时代的中文机制、坏的图引用）修到 xelatex 编译通过；修复后由脚本复验，仍不过才终止。产出两样东西：一份**保证 xelatex 编译通过的原文**（`build/precompile.tex`，多数论文与 flat.tex 逐字节相同），它是 mask 起下游全部阶段的输入；一份**基线数据**（页数与三类日志计数），供 compile 阶段对 `zh.pdf` 的出口判据做增量比对——需要引擎适配的论文，只有在本阶段完成适配，基线数据才存在，compile 的「页数与基线相当」判据才有参照系。flatten 留给本阶段裁决的两件事（残留 `\input` 的展开语义、bbl 内联的正确性）也在编译中兑现。

**输入 → 输出**：`build/flat.tex` + `src/` + `build/manifests/flatten.json` → `build/precompile.tex`（下游输入）+ `build/precompile/`（编译树，含 `flat.pdf` 与 `flat.log`）+ `build/manifests/precompile.json`。precompile 只读 `src/` 与 `build/`，只写 `build/` 与 `logs/`（会话 transcript）。

## 前置条件

precompile 从 flatten manifest 装载上游结论与输入 hash，不重扫源码树，也不读 fetch manifest——所需的 `fetch_files_sha256` flatten manifest 已转录。

- flatten manifest 缺失或不可解析，或状态是 ok 但 `build/flat.tex` 不在 → 状态 `flatten_missing`，message 提示先跑 `tongtu stage flatten`。
- flatten 状态不是 ok → 状态 `flatten_not_ok`，转录 flatten 的状态与它记录的 fetch 状态（pdf_only 沿链退 3，失败态要先重跑上游）。

前置失败同样写 manifest：驱动器不抛栈、每次执行的结论都落盘，沿用 fetch 起确立的约定。

## 编译树

编译对象是 `build/flat.tex`，不是 `src/` 里的原始主文件。理由有三：flatten 的出口判据只做形式检查，展开语义与 bbl 内联的裁决权在本阶段，裁决只能通过编译 flat.tex 本身兑现；compile 阶段编译的 `zh.tex` 从本阶段的输出一路变换而来，基线与比对对象同构（同为单文件、bbl 已内联、同一棵资源树），页数与 overfull 的增量比对才成立；编 `src/` 主文件要么把副产物写进只读的 `src/`，要么依赖输出重定向语义，都不如在 build 区编译干净。

flat.tex 不自包含（`.cls` / `.sty` / 图源 / `.bib` 都在 `src/`），指向方式是拷贝：把 `src/` 内容全量拷进 `build/precompile/`，再把 `build/flat.tex` 的 bytes 写为 `build/precompile/flat.tex`，cwd 设在 `build/precompile/` 编译。行为与 TeX 的正常编译完全一致：宏包与图按相对路径就位，走 bibtex 的论文（flat.tex 保留着 `\bibliography{…}` 命令）latexmk 直接处理，不需要另设搜索路径；副产物全部落在 `build/precompile/`，`src/` 保持只读。曾考虑 `TEXINPUTS` 指向 `src/` 免拷贝：`\graphicspath` 相对路径与 `BIBINPUTS` 要分别处理，递归搜索在杂文件多的源码树上有意外匹配的风险，省一次 build 区内的拷贝（通常几 MB 到几十 MB）换三处特殊语义，不值。`src/` 里本来就有名为 `flat.tex` 的文件时会被覆盖，记一条 warning（实测语料无此形态）。

## 首次编译

```
latexmk -xelatex -interaction=nonstopmode flat.tex
```

- **引擎是 xelatex，不按论文原始引擎探测**。本阶段的目的是保证 compile 阶段的前提、为它立基线，两者都要求与 compile 同引擎（中文必须 xelatex）：若这里用 pdflatex 通过而这篇论文在 xelatex 下本来就编不过，compile 的失败仍分不清是翻译还是引擎；引擎不同，页数与 overfull 的基线里也混入引擎差异这个变量。原文按 pdflatex 写因而在 xelatex 下编不过的论文，正是修复会话要处理的对象。
- latexmk 恰好拉起一次，不重试；多 pass 收敛（含 bibtex）是 latexmk 自己的职责。单次 latexmk 超时 600 秒（起步值，按真实论文校准）：latexmk 会派生 xelatex 子进程，超时要杀整个进程组。
- latexmk 不在 PATH → `compile_failed`，message 说明需要安装 TeX 发行版。`tongtu doctor` 已接线，检查清单与行为见 [CLI.md](../CLI.md)。
- 首次编译通过 → 不拉会话，直接进入基线数据解析与产物写出，`build/flat.tex` 逐字节拷出为 `build/precompile.tex`。

## 修复会话

首次编译失败即拉起恰一次 agent 修复会话（架构 §3 阶段表的 hook②，`work` 原语的首个落地）；会话内 agent 自行迭代（改、编、看日志），改到它认为可以为止。

- **现场**：cwd 是编译树 `build/precompile/`，agent 可自由读写树内文件、执行命令。不建命令集：树整体可丢、里面只有原文拷贝。compile 阶段后来采用了同样的做法（[stages/compile.md](compile.md)，决策见架构附录 A.36），架构附录 B 第 7 条的约束力问题随之关闭。
- **prompt**：任务说明与约束在 prompt 资产 [`skill/precompile/SKILL.md`](../../skill/precompile/SKILL.md)（含已观测失败模式的 example；新模式的沉淀方式就是往里加 example，规则同 diction denylist——实测抓到才加，不预防性扩张），驱动器拼上本篇的 flat.log 错误行摘录。核心约束：只改树内文件、优先只改 flat.tex、不改动文字内容、只改到能编译为止。
- **不建确定性修复规则集**。曾考虑：把已观测模式（`\pdfoutput` 注释、CJKutf8 摘除、图扩展名改写）写成确定性预处理规则，agent 会话推迟。否决理由：规则集是需要维护的框架代码，而 example 进 prompt 后新模式的维护动作是零代码；修复成果烙进输出产物、随 manifest 缓存持久（见「重跑语义」），会话成本不随重跑重复发生；规则覆盖不了的失败终究要会话兜底，两套机制并存不如一套。
- **运行时**：`work` 原语经 agent 适配层（`tongtu/agent/`）拉起，首发运行时 Claude Code CLI，headless。模型默认 `claude-sonnet-5`、reasoning effort xhigh，`--model` 透传覆盖。会话 transcript 原样落 `logs/`；架构里 trace 的精细语义（start-state hash + command sequence + end-state hash）依赖命令集记账，而零期两个修复会话都不建命令集（架构附录 A.36），此处记 transcript 与会话前后编译树相对 `src/` 的文件 hash 比对结果。
- **预算**：会话轮数上限 30、墙钟超时上限 900 秒（模块级常量）。轮数上限按两篇失败案例的实测轮数（11 与 15）留一倍余量定出；墙钟上限防会话卡死而非控制成本，给会话内编译大篇幅论文留出时间。超限即终止会话，但不直接判失败——会话可能在超限之前已把问题修完，结论仍由脚本终审给出。

## 脚本终审（agent 的编译结果不作数）

会话结束后脚本自己验证：`latexmk -C` 清理编译产物，再跑一遍自己的 `latexmk -xelatex`，出口判据与基线数据全部取自这一遍。
- 通过 → 把树内（可能已被修改的）`flat.tex` 拷出为 `build/precompile.tex`；
- 不过 → `compile_failed`，pipeline 终止。

**改动传播的边界（零期简化）**：只承诺 flat.tex 的调整传播到下游。会话若改了树内其他文件（如 `.sty`），脚本按「与 `src/` 逐文件 hash 比对」检出、记入 manifest 的 `changed_files` 与 warnings，但不传播——compile 阶段的编译树仍从 `src/` 组装，这类修复到那边要重做。真实出现再设计传播机制；已观测的失败模式都只需改 flat.tex。

## 基线数据

五个计数全部从 `build/precompile/flat.log` 解析，不引新依赖、不读 PDF 内容：

- `pages`：`Output written on …(N pages…` 行。latexmk 的 xelatex 路径经 `.xdv` 中转，该行的文件名可能是 `flat.xdv`，解析不依赖扩展名。
- `overfull_hboxes`：`Overfull \hbox` 行计数。
- `undefined_references` / `undefined_citations`：`LaTeX Warning: Reference` / `LaTeX Warning: Citation` 前缀的行计数。log 默认在 79 列折行，`undefined` 未必与前缀同行，故只按前缀匹配；标准内核下这两种前缀只来自未定义警告。
- `missing_characters`：含 `Missing character` 的行计数。`\tracinglostchars=2` 是 2021 年后 LaTeX 内核的默认值，本阶段编的是原文、不注入 preamble，靠内核默认即可；compile 阶段的 inject_cjk 才显式写入。经修复会话的论文若源里有中文字符（如 CJKutf8 机制被摘除后），这些字符在本阶段缺字形属预期，计入基线即可——compile 阶段注入 xeCJK 后它们正常渲染，其 CJK missing glyph 判据与本基线无关。

页数是 compile 出口「页数与基线相当」的基准；其余三类计数供 compile 取相对增量记入 report。`flat.pdf` 与 `flat.log` 留在 `build/precompile/` 原地，随 build/ 可丢，不进 artifact package。

## 出口判据

三条同时成立才是 ok：
- 终审那次 latexmk 退出码 0；
- `flat.pdf` 存在且非空；
- 页数解析成功（大于 0）。

任一不成立 → `compile_failed`，message 摘录 flat.log 中以 `!` 开头的错误行（至多五条）与 log 路径；log 不存在时摘 latexmk 的 stderr。首编即过的论文，终审就是首编那一次。

有意从严，不用 `latexmk -f` 强行编完求 PDF：带错误编出的 PDF，页数与 overfull 计数不可信，基线就失去意义。

## 状态与退出码

`PrecompileStatus`：`ok` / `flatten_missing` / `flatten_not_ok` / `compile_failed`。

`compile_failed` 覆盖：编译超时、agent 运行时不可用、会话超预算、复验仍不过，message 区分。退出码：`ok` → 0；`flatten_not_ok` 且 flatten 记录的 fetch 状态是 `pdf_only` → 3（跨子命令同码同义）；其余 → 1。

## 重跑语义

- **输入 hash 是两个值**，都从 flatten manifest 转录：`flat_sha256`（flat.tex 内容）与 `fetch_files_sha256`（`src/` 全量清单的规范化 hash）。只看 flat_sha256 不够：改一张图不动 `.tex` 时 flat.tex 不变，编译结果却会变；fetch_files_sha256 恰好补上 `src/` 这一半输入，且已在 flatten manifest 里，转录零成本。
- **输出 hash**：manifest 记 `precompile_sha256`（`build/precompile.tex` 的内容 hash），是下游阶段判定「输入未变不重算」时引用的权威记录。修复成果因此持久：输入不变则跳过命中，同一篇论文不重复付会话成本；修复内容变了（重修或 `--force`），下游经此 hash 自动失效重算。
- **跳过判定**：precompile manifest 存在、可解析、状态 ok、两个输入 hash 与当前 flatten manifest 一致、`build/precompile.tex` 与 `build/precompile/flat.pdf` 都存在 → 跳过。不校验产物内容与 manifest 是否一致（初期简化，同 fetch / flatten）。
- 失败状态不跳过；`--force` 无视已有结论。每次非跳过的执行开始先整目录删除 `build/precompile/` 并删除 `build/precompile.tex`：旧 aux 文件会污染重编结果，失败时也不留上次的产物误导下游。

## 产物模型

manifest 即 `PrecompileManifest`。承担契约职责的字段：`status` 是唯一分流依据；`flat_sha256` 与 `fetch_files_sha256` 是本阶段的输入 hash；`precompile_sha256` 是输出 hash，下游输入判定的权威；`pages` / `overfull_hboxes` / `undefined_references` / `undefined_citations` / `missing_characters` 是 compile 阶段增量比对引用的基线数据；`fix_session` 记录本次是否拉过修复会话，`session_stop_reason` / `session_model` / `session_duration_seconds` 记录会话结局（report 的 hook 干预统计取自这里）；`changed_files` 是会话对 flat.tex 之外文件的改动清单；`flatten_status` 与 `fetch_status` 转录本次看到的上游状态（退出码映射与排查用）；`command` 记录终审的 latexmk 命令行；`pdf_bytes` 与 `duration_seconds`（终审编译耗时）供排查与超时校准。

## 验收与试跑对象

十一篇（三篇自造论文 + [examples/README.md](../../examples/README.md) 真实论文表八篇）全部要求：状态 ok、页数大于 0 且与真实 PDF 规模相符、重跑命中跳过、`--force` 重算。其中 `1701.06538`（`\pdfoutput` 原语加缺失的 `.eps` 图源）与 `2412.19437`（CJKutf8 机制）首编失败，须经修复会话修到复验通过，会话轮数与耗时的实测值用于校准预算上限。专项核对：九篇首编即过的论文不拉会话（`fix_session` 为 false）、`precompile.tex` 与 flat.tex 逐字节相同；`1412.6980`（pdf_only 套壳）走 `flatten_not_ok`、退出码 3；有 `.bib` 无 `.bbl` 的论文（`2512.02556` 等）确认 latexmk 自动跑通 bibtex、未定义引用计数为零。真实论文源码不入库。
