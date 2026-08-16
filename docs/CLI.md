# 通途 CLI 契约

> 通途的接口 = 产物契约（[ARCHITECTURE.md](ARCHITECTURE.md) §5 artifact contract 节）+ 本文档的 CLI 调用约定。本文档是命令面的权威定义：命令、参数语义、退出码与 `--json` 事件流；wenshu 的容器调度侧只依赖这两份契约。

## 命令面（草案）

```
tongtu run <论文>  [--glossary FILE]...  [--workdir DIR]  [--force]  [--json]
tongtu retranslate <编号>  (--chunks c012,c045 | --term WORD | --all)
tongtu stage <name> <论文>  [--force]      # 单阶段入口，调试用
tongtu validate <src> <dst>                # 四层 validation，逐项报告失败
tongtu doctor                              # 检查 xelatex/latexmk/latexpand/pdftocairo/epstopdf 与字体，逐项报告缺失
tongtu preview <编号>                      # 打开 inspection page

tongtu tex <cmd> …                         # 编译修复会话的工具面，不面向人（见下节）
```

- **`<论文>` 参数接受三种形态**：arXiv 编号（可含斜杠与版本号后缀，如 `hep-th/9901001`、`2002.05202v1`）；arXiv 链接（`arxiv.org` 的 `/abs/`、`/pdf/`、`/html/` 路径，解析出编号后与编号同路）；本地源码目录。识别顺序与解析规则见 [stages/fetch.md](stages/fetch.md)。
- `run` 幂等：重复执行按 manifest 与翻译缓存跳过已完成部分；`--force` 无视缓存 full rerun。
- `stage`：单阶段调试入口。重跑语义按各阶段自己的设计（见 `stages/` 下对应文档）；`--force` 无视已有结论重新执行。已接线：fetch、flatten；其余阶段为占位实现（退出码 99）。
- `validate` 有三个调用方，同一份实现：agent 在翻译会话内自查、脚本在出口终审、开发者手工排查（[ARCHITECTURE.md](ARCHITECTURE.md) §3 translate 节）。
- `retranslate` 的失效语义见 [ARCHITECTURE.md](ARCHITECTURE.md) §4 返工触发表。**边界行为还没想**：chunk id 写错、术语没命中任何 chunk、失效后要不要连带重编译，这些当前只有实现里的做法，没做设计。

## 退出码

小集合、文档化、按需增码。细粒度状态走 stderr、`--json` 事件流与 report.json，退出码只承载「调用方不解析输出就要分流」的结果：

| 退出码 | 含义 |
|---|---|
| `0` | 正面结果：`run` 出包（含有回退 chunk 的情形，详情在 report.json）、单阶段完成、校验全绿、环境齐全、编译通过 |
| `1` | 负面结果或一般失败：未能出包、下载或解包失败、校验有失败层、环境有缺失、编译失败 |
| `2` | 用法错误（typer 默认） |
| `3`–`9` | 业务分支段：既非成功也非失败、调用方要据此改道的结果，跨子命令同码同义。已登记：`3` = PDF-only（源是 PDF 而非 LaTeX 源码，走 degraded path） |
| `99` | 占位实现：命令尚未接线。接线一个删一个，全部接线后此码退役 |

- 检查类命令（`validate` / `doctor` / `tex compile`）沿用「0 = 通过、1 = 查出问题」的谓词惯例：查出问题是正常业务结果，同样退 1，细节在输出里。
- 增码判据：某调用方需要在不解析输出的前提下按该状态机械分流；达不到的一律归 `1`，细节由输出携带。新码依次取 `3`–`9` 未用值；不使用 `64`–`78`（BSD sysexits 区段）与 `126` 以上（shell 保留区段）。
- 登记处是本表与 `tongtu/cli.py` 的模块级常量，两处同步修改。

## `--json` 事件流

向 stdout 输出机器可读事件流（阶段起止、chunk 进度、最终结果），属于 wenshu 容器调度侧消费的调用约定。**schema 还没定**：一期容器调度前冻结，零期先出草案（[ARCHITECTURE.md](ARCHITECTURE.md) 附录 B 第 3 条）。当前行为：已接线的命令收到 `--json` 时向 stderr 说明 schema 尚未定义，忽略该选项照常执行。

## `tex` 工具面（不面向人）

编译修复会话内 agent 的全部动作面：读写 `zh.tex`、编译、看渲染页、回退或重译某 chunk，动作与 metadata 一并记账。分区权限（preamble 自由 patch，正文的每次变化对应显式动作）与记账规则是 compile 阶段机制的一部分，见 [ARCHITECTURE.md](ARCHITECTURE.md) §3 compile 节。

```
tongtu tex read [--preamble | --chunk <id> | --lines A-B]
tongtu tex patch --old <文本> --new <文本>          # preamble
tongtu tex patch --chunk <id> --old … --new …      # 正文，该 chunk 状态记 edited
tongtu tex compile                                  # 编译一次，返回错误列表与日志摘要
tongtu tex render --page N                          # 渲染某页为图，供 agent 看排版
tongtu tex fallback <chunk-id> [--paragraph N]      # 该段回退原文
tongtu tex retranslate <chunk-id>                   # 重译一次（复用 translate 的翻译调用）
```
