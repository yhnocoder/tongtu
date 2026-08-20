# 通途 CLI 契约

> 通途的接口 = 产物契约（[ARCHITECTURE.md](ARCHITECTURE.md) §5 artifact contract 节）+ 本文档的 CLI 调用约定。本文档是命令面的权威定义：命令、参数语义、退出码与 `--json` 事件流；wenshu 的容器调度侧只依赖这两份契约。

## 命令面（草案）

```
tongtu run <论文>  [--glossary FILE]...  [--workdir DIR]  [--force]  [--json]
tongtu retranslate <编号>  (--chunks c012,c045 | --term WORD | --all)
tongtu stage <name> <论文>  [--glossary FILE]...  [--force]  [--model ID]  [--jobs N]  [--max-fallback-ratio R]
                                           # 单阶段入口，调试用
tongtu validate <src> <dst>                # 四层 validation，逐项报告失败
tongtu doctor                              # 检查 xelatex/latexmk/latexpand/pdftocairo/epstopdf、字体与 OpenCode 密钥，逐项报告缺失
tongtu preview <编号>                      # 打开 inspection page

tongtu tex compile                         # 编译修复会话可调用的命令，不面向人（见下节）
```

- **`<论文>` 参数接受三种形态**：arXiv 编号（可含斜杠与版本号后缀，如 `hep-th/9901001`、`2002.05202v1`）；arXiv 链接（`arxiv.org` 的 `/abs/`、`/pdf/`、`/html/` 路径，解析出编号后与编号同路）；本地源码目录。识别顺序与解析规则见 [stages/fetch.md](stages/fetch.md)。
- `run` 幂等：重复执行按 manifest 与翻译缓存跳过已完成部分；`--force` 无视缓存 full rerun。
- `stage`：单阶段调试入口。重跑语义按各阶段自己的设计（见 `stages/` 下对应文档）；`--force` 无视已有结论重新执行。已接线：fetch、flatten、precompile、mask、survey、chunk、translate；其余阶段为占位实现（退出码 99）。precompile 编译失败时拉起 agent 修复会话（`--model` 透传给运行时，默认见 stages/precompile.md）。translate 的 `--model` 透传给 `ask`（默认 muse-spark-1.2-contributor，见 [models.md](models.md)），`--jobs` 是 chunk 级并发度（默认 4），`--max-fallback-ratio` 是回退比例阈值（默认 0.2，超过它整体判失败、不进入 compile）；三者的语义见 [stages/translate.md](stages/translate.md)。translate 的复用粒度是整个阶段，零期没有 chunk 级翻译记忆，故 `--force` 就是整篇重翻。`--glossary` 与 `run` 同语义（命令行层术语表，合并语义见 [stages/survey.md](stages/survey.md)），供 survey 及其下游阶段的单阶段调试传入。
- `validate` 已接线，有三个调用方、同一份实现：translate 驱动器的出口终审、compile 终审对正文控制序列 multiset 的比对（只用第 2 层）、开发者手工排查（层与判定见 [stages/translate.md](stages/translate.md) validate 四层节）。比对的是两份文件的正文，首尾空白不参与判定；沿用「0 = 通过、1 = 查出问题」的谓词惯例，逐层报告通过与失败。
- `retranslate` 的失效语义见 [ARCHITECTURE.md](ARCHITECTURE.md) §4 返工触发表。chunk id 是位置序号，只在同一次分块结果（chunk manifest 的 `chunks_sha256`）下有效，重分块后同一 id 指向不同内容（[stages/chunk.md](stages/chunk.md) part 与 chunk id 节）。**边界行为还没想**：chunk id 写错、术语没命中任何 chunk、失效后要不要连带重编译，这些当前只有实现里的做法，没做设计。
- `doctor` 已接线：xelatex / latexmk / latexpand / pdftocairo / epstopdf 按 PATH 查找，中文字体查仓库 `fonts/` 下的霞鹜文楷字体文件，OpenCode 密钥按环境变量 `$OPENCODE_API_KEY` → `~/.config/tongtu/credentials.json` → 本机 opencode 登录态的顺序解析并报告来源。

  **检查项分两组，退出码只对第一组负责**：工具链与字体缺任一项则编译无法进行，退 1；运行期凭证（当前只有 OpenCode 密钥）缺失如实打印但退 0。理由是参考镜像在构建期跑 `tongtu doctor` 自检（`docker/Dockerfile`），而镜像是要推 GHCR 的可分发产物，构建它的机器不该需要凭证；凭证只有 translate 用得上，缺失不影响 fetch 到 chunk 与 compile 之后的各阶段执行。凭证缺失时末行给出说明。
- **密钥 preflight**：需要 `ask` 的命令（translate 是第一个消费方）开跑前解析一次密钥。解析到环境变量或录入的密钥 → 静默；解析到 opencode 登录态 → 首次打一行提示（此后静默，标记位在 `credentials.json`）；三处都没有且处于交互终端 → 提示两条路：去 opencode 里 `/connect` 登录 Go 订阅后回车重查，或当场录入密钥（隐藏输入，先打一次 `/models` 验证有效再存入 `credentials.json`）；非交互环境（无 TTY 或 `--json`）不提问，按失败语义进 manifest。
  已接线的只有解析顺序（`tongtu.agent.opencode.resolve_api_key`）与非交互的失败语义（translate 有待翻 chunk 且解析不到密钥 → 状态 `translate_failed`）；登录态首次提示与交互式录入尚未实现。

## 退出码

小集合、文档化、按需增码。细粒度状态走 stderr、`--json` 事件流与 report.json，退出码只承载「调用方不解析输出就要分流」的结果：

| 退出码 | 含义 |
|---|---|
| `0` | 正面结果：`run` 出包（含有回退 chunk 的情形，详情在 report.json）、单阶段完成、校验全绿、环境齐全、编译通过 |
| `1` | 负面结果或一般失败：未能出包、下载或解包失败、校验有失败层、环境有缺失、编译失败 |
| `2` | 用法错误（typer 默认） |
| `3`–`9` | 业务分支段：既非成功也非失败、调用方要据此改道的结果，跨子命令同码同义。已登记：`3` = PDF-only（源是 PDF 而非 LaTeX 源码，走 degraded path） |
| `99` | 占位实现：命令尚未接线。接线一个删一个，全部接线后此码退役 |

- 检查类命令（`validate` / `doctor` / `tex compile`）沿用「0 = 通过、1 = 查出问题」的谓词惯例：查出问题是正常业务结果，同样退 1，细节在输出里。`doctor` 的「问题」只算工具链与字体那一组，运行期凭证缺失不计入（见上节）。
- 增码判据：某调用方需要在不解析输出的前提下按该状态机械分流；达不到的一律归 `1`，细节由输出携带。新码依次取 `3`–`9` 未用值；不使用 `64`–`78`（BSD sysexits 区段）与 `126` 以上（shell 保留区段）。
- 登记处是本表与 `tongtu/cli.py` 的模块级常量，两处同步修改。

## `--json` 事件流

向 stdout 输出机器可读事件流（阶段起止、chunk 进度、最终结果），属于 wenshu 容器调度侧消费的调用约定。**schema 还没定**：一期容器调度前冻结，零期先出草案（[ARCHITECTURE.md](ARCHITECTURE.md) 附录 B 第 3 条）。当前行为：已接线的命令收到 `--json` 时向 stderr 说明 schema 尚未定义，忽略该选项照常执行。

## `tex` 子命令（不面向人）

编译修复会话内 agent 可调用的命令，零期只有一条。会话现场是编译树 `build/zh/`，agent 用自带的文件工具与 PATH 上的工具读写文件、看日志、渲染页，不经这里中转（决策见 [ARCHITECTURE.md](ARCHITECTURE.md) 附录 A.36，机制见 [stages/compile.md](stages/compile.md)）。

```
tongtu tex compile        # 在 cwd 的编译树内编译一次，返回退出码、错误行摘录与日志路径
```

保留这一条的理由是参数统一：会话编译与脚本终审必须是同一条 latexmk 命令（同引擎、同超时、同 `-synctex` 开关），而参数留在脚本里而不是编译树内的配置文件，因为后者 agent 能改。退出码沿用检查类命令的谓词惯例：编译通过退 0，编译失败退 1。

原设计的另外五条零期不建：`read` / `patch` / `render` 由 agent 自带的 Read、Edit 与 PATH 上的 `pdftocairo` 承担；`fallback` / `retranslate` 属于回退路径，零期一律不实现（[stages/compile.md](stages/compile.md)「零期不实现 fallback」节）。
