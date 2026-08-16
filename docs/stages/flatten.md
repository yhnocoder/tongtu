# flatten —— 多文件源码展开成单文件

> 阶段定位见 [ARCHITECTURE.md](../ARCHITECTURE.md) §3 阶段表，本文是 flatten 阶段设计的权威。实现在 `tongtu/stages/flatten.py`；manifest 的字段级权威定义在 `tongtu/artifacts/flatten.py`（pydantic model，文档不复述字段表）。

**要解决的问题**：e-print 源码树通常是多文件：`\input` / `\include` 拆分正文，参考文献在预编译的 `.bbl` 里。下游的 mask、survey、chunk 都要在单一文本流上工作，flatten 把源码树收敛成一份 `build/flat.tex`。展开前要先回答「哪个文件是主文件」；绝大多数论文可由确定性规则判定，判定不了的属于 hook①（`ask` 原语）的场合，实测语料中尚未出现，实现推迟（见「主文件判定」节）。

**输入 → 输出**：`src/` 源码树 + `build/manifests/fetch.json` → `build/flat.tex` + `build/manifests/flatten.json`。flatten 只读 `src/`、只写 `build/`。

## 前置条件

flatten 从 fetch manifest 装载上游结论，不重扫源码树：fetch manifest 的 `files` 是输入 hash 的权威记录，`tex_files` 是主文件候选的枚举来源。

- fetch manifest 缺失或不可解析 → 状态 `fetch_missing`，message 提示先跑 `tongtu stage fetch`。
- fetch 状态不是 `ok` → 状态 `fetch_not_ok`，message 转述 fetch 的状态（`pdf_only` 走 degraded path，失败态要先重跑 fetch）。

前置失败同样写 manifest：驱动器不抛栈、每次执行的结论都落盘，沿用 fetch 起确立的约定。

## 主文件判定

候选集 = `tex_files` 中内容含 `\documentclass`（或 `\documentstyle`）的文件；逐行判定，只看每行第一个未转义 `%` 之前的部分，注释里的不算。规则按序收敛：

1. 候选恰一个 → 主文件。历史语料九篇与验收十一篇全部停在这一步（主文件名并不总是 `main.tex`：`arxiv.tex`、`neurips_2021.tex`、`iclr2025_conference.tex`、`colm2024_conference.tex` 都出现过，单候选规则对名字无假设）。
2. 候选多于一个 → 只留内容含 `\begin{document}` 的候选；筛完恰一个 → 主文件；筛成空集则退回筛选前的集合继续下一条。
3. 仍多于一个 → 恰有一个基名是 `main.tex` → 主文件。
4. 仍多于一个 → 状态 `main_ambiguous`，message 列出全部候选。这里是 hook① 的位置（`ask` 原语判定主文件，见 ARCHITECTURE §3 agent 适配层节）；实测语料中没有一篇走到这一步，agent 调用与适配层因此推迟实现，重启条件 = 真实遇到规则收敛不到唯一候选的论文。
5. 候选为空 → 状态 `main_not_found`。不拉 agent：源码里没有 `\documentclass`，agent 也无从指认主文件。

## latexpand 展开

```
latexpand --keep-comments --fatal <主文件相对路径>
```

- cwd 是 `src/` 根目录，`\input` 相对路径以此为基准解析，与 TeX 编译时的工作目录一致。stdout 由驱动器捕获，成功才写 `build/flat.tex`（bytes 原样，不做编码转换）；stderr 非空行记入 manifest `warnings`（latexpand 对 @-命令的 `--makeatletter` 建议是常见一例，实测为误报——@-命令本就包在 `\makeatletter…\makeatother` 里——照记不拦）。
- `--keep-comments`：latexpand 默认剥掉注释与 `\end{document}` 之后的内容，这是有损变换；mask 阶段把注释当作 block 处理，flatten 不做任何有损变换。实测确认该选项下注释里的 `\input` 不被展开，原样保留。
- `--fatal`：`\input` 指向的文件找不到时立刻失败（状态 `expand_failed`），不静默留下残缺产物。arXiv 源在 arXiv 编译通过，文件本应齐全；实测十一篇无一触发。
- latexpand 不在 PATH → 状态 `expand_failed`，message 说明需要安装 TeX 发行版（latexpand 随 TeX Live 分发）。`tongtu doctor` 的对应检查项等编译工具面接线时一并实现。
- `\usepackage` 不展开（不传 `--expand-usepackage`）：`.sty` 与 `.cls` 是排版资产不是翻译对象，展开只会把样式代码灌进下游的 mask 与 survey 输入。因此 **`flat.tex` 不是自包含文件**：正文与参考文献已内联，类文件、样式文件、图源仍留在 `src/`，由 baseline 编译时指向 `src/` 提供。

## bbl 内联

主文件同目录存在同主干 `.bbl`（`main.tex` → `main.bbl`）时，把它内联进 flat.tex——这对应 arXiv 的编译约定：参考文献不从 `.bib` 现编，直接用作者上传的预编译 bbl（按主文件名匹配）。

内联由 flatten 自己做，不用 latexpand 的 `--expand-bbl`：后者按字面子串匹配 `\bibliography{…}`，不识别注释语义，实测（2412.19437）把注释行 `% \bibliography{main}` 也展开了，产出重复且不可编译的参考文献段；上游对此没有可用参数，v1.7.2 即当前最新版。自研内联在 latexpand 展开完成后执行：逐行扫描 flat.tex，找注释外的 `\bibliography{…}` 命令（判注释同主文件判定：每行第一个未转义 `%` 之前的部分）；恰一处 → 以 bbl 文件内容整体替换该命令；零处或多处 → 不内联，记 warning，交 baseline 裁决。字节级操作，不做编码转换。

没有同主干 `.bbl` 时不内联也不警告：带 `.bib` 的源走 bibtex 的路径，由 baseline 的 latexmk 处理。biblatex（`\addbibresource` 加 biber）暂不支持，真实遇到再加。

## 出口判据

机械三条：latexpand 退出码 0；输出非空且含 `\begin{document}` 与 `\end{document}`（字节包含检查）；`flat.tex` 与状态 `ok` 的 manifest 落盘。展开后仍残留的注释外 `\input` / `\include` 只记 warning 不判失败——flatten 的出口判据是形式检查，展开语义对不对由 baseline 编译裁决。

## 状态与退出码

六状态（`FlattenStatus`，StrEnum）：`ok` / `fetch_missing` / `fetch_not_ok` / `main_not_found` / `main_ambiguous` / `expand_failed`。退出码：`ok` → 0；`fetch_not_ok` 且 fetch 的状态是 `pdf_only` → 3（跨子命令同码同义）；其余 → 1。

## 重跑语义

- **输入 hash**：fetch manifest `files` 清单的规范化 hash（按路径排序，每行「路径 + 制表符 + sha256 + 换行」，UTF-8 编码后取 sha256），记入 manifest `fetch_files_sha256`。用全量清单而不只算被展开的文件：依赖闭包要从展开结果反推，而 flatten 本身是毫秒级操作，改一张图多跑一次不值得精细化。
- **跳过判定**：flatten manifest 存在、可解析、状态 `ok`、`fetch_files_sha256` 与当前 fetch manifest 算出的值一致、`build/flat.tex` 存在 → 跳过。不校验 `flat.tex` 内容与 manifest 是否一致（初期简化，同 fetch）。
- 失败状态不跳过，每次重跑；`--force` 无视已有结论重新执行。执行开始先删除已有 `build/flat.tex`，失败时不留上次的产物误导下游。

## 产物模型

manifest 即 `FlattenManifest`。承担契约职责的字段：`status` 是唯一分流依据；`flat_sha256` 与 `flat_bytes` 是下游阶段判定「输入未变不重算」时引用的权威记录；`main_file` 与 `candidates` 记录主文件判定；`fetch_files_sha256` 是本阶段的输入 hash；`fetch_status` 转录本次看到的 fetch 状态（退出码映射与排查用）；`bbl_file` 记录内联了哪个 bbl（未内联为空）；`command` 记录实际执行的 latexpand 命令行，供排查。

## 验收与试跑对象

十一篇：三篇自造论文（article 验证 `\input` 展开与「有 `.bib` 无 `.bbl` 不内联」、conference 验证 bbl 内联、revtex 验证手写 thebibliography 原样通过）；八篇真实论文清单与各自覆盖的形态见 [examples/README.md](../../examples/README.md) 真实论文节。专项核对：`2412.19437` 的注释行 `% \bibliography{main}` 原样保留、`\begin{thebibliography}` 恰出现一次；`2512.24880` 的 @-命令警告记入 warnings 且状态 `ok`。每篇核对 flat.tex 无注释外 `\input` 残留、重跑命中跳过、`--force` 重算。真实论文源码不入库。
