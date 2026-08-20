# translate —— 逐 chunk 翻译与结构校验

> 阶段定位见 [ARCHITECTURE.md](../ARCHITECTURE.md) §3 阶段表，本文是 translate 阶段设计的权威。实现分两处：`tongtu/validation.py` 是四层 validate 的实现（核心文本层，零第三方依赖，与 masking.py、chunking.py、glossary.py 同级），`tongtu/stages/translate.py` 是阶段驱动器（上下文组装、ask 调用与重试、出口终审、落盘）。manifest 的字段级权威定义在 `tongtu/artifacts/translate.py`（pydantic model，文档不复述字段表）。

**要解决的问题**：把每个 chunk 译成中文，同时保证译文与原文结构完全一致。placeholder 少一个、control sequence 多一个、两段被并成一段，backfill 之后都会编译失败或丢内容，而模型的「我检查过了」在这里没有效力。

**输入 → 输出**：chunk + brief + terms + neighbors → 译文 chunk + `build/translated/<id>.tex` + `build/manifests/translate.json`。brief 与 resolved glossary 都是 survey 阶段的产出（[stages/survey.md](survey.md)）；`style` 随 resolved glossary 一起进来，原样进提示词。

## 前置条件

沿用 fetch 起确立的约定：前置失败也写 manifest，驱动器不抛栈，每次执行的结论都落盘。

- chunk manifest 缺失或不可解析，或状态是 ok 但任一 chunk 文件缺失 → 状态 `chunk_missing`。
- chunk 状态不是 ok → 状态 `chunk_not_ok`，转录上游状态。
- survey manifest 缺失或不可解析，或状态是 ok 但 `build/glossary.json` 或 `build/brief.json` 缺失 → 状态 `survey_missing`。
- survey 状态不是 ok → 状态 `survey_not_ok`，转录上游状态。
- 有待翻译的 chunk，但 `--model` 给的模型不在 `tongtu/agent/opencode.py` 的模型清单里，或三处都找不到 OpenCode 密钥 → 状态 `translate_failed`，原因写进 `message`。这两种失败不发请求也能判定，在开工前一次性报出，不让每个 chunk 各自重试到额度用尽。

## 翻译原语

翻译使用 `ask` 原语（API 直调，单次问答），不使用 `work`（多轮会话）。理由：翻译的输入输出形态固定（chunk 文本进、译文出），不需要读写文件或执行命令；validate 失败的重试循环是机械的——把校验错误附在提示词再 ask 一次，脚本驱动几行代码的事；`work` 每个 chunk 拉起一次进程的开销对小 chunk 不值。

- 默认模型：`muse-spark-1.2-contributor`（responses 端点家族），`--model` 可覆盖。
- 推理强度：`low`（实测 low 档格式遵循最好，medium 档空行被吞，详见 [models.md](../models.md)）。
- 重试：openai SDK 处理网络层重试（连接错误、429）；翻译层面的重试（validate 失败后带错误信息重新 ask）由驱动器驱动，至多 `MAX_RETRIES`（起步 2）次，仍不过则 fallback。
- 重试额度只由**产出了可评判译文**的尝试消耗。`ask` 报错与返回空译文的那次不算：模型没有给出可评判的东西，把「四层全挂」回灌给它既不成立也白费一次额度。实测 deepseek-v4-pro 会在 `finish_reason` 正常、推理 token 照常消耗的情况下返回空字符串，三轮试跑各出现一次，与 chunk 大小无关。这类调用另由 `MAX_ASK_CALLS`（`MAX_RETRIES + 3`）兜住上限，避免模型持续返回空译文时循环不停。

## 翻译循环

```
chunk 循环 → 跳过判定（translatable_chars == 0 → 原文即译文）
           → 组装上下文 → ask(hook⑤)
                            │
                脚本跑 validate
                  ├ 通过 → 写译文与 manifest
                  └ 不通过 → 带错误信息重新 ask（至多 MAX_RETRIES 次）
                              └ 仍不通过 → 回退原文，记 status="fallback"
```

与 ARCHITECTURE 原设计的差异：原用 `work` 原语，agent 在会话内自跑重试。改为 `ask` 后重试由脚本驱动——翻译的输入输出形态固定，validate 失败的重试是机械循环，不需要会话运行时的能力。决策见 ARCHITECTURE 附录 A.32。

## 并发

chunk 之间没有数据依赖：neighbors 取的是源文本，前一个 chunk 的译文不进后一个 chunk 的提示词（ARCHITECTURE 附录 A.11）。整个 chunk 循环因此可以直接并发，不需要拓扑排序，也不需要分批。

```
主线程  读 chunk manifest → 跳过判定 → 要翻的进任务队列
          │
          ThreadPoolExecutor(max_workers=JOBS)
          ├ worker  组装上下文 → ask → validate → 重试（至多 MAX_RETRIES 次）→ 返回结果
          ├ worker  ...
          └ worker  ...
          │
主线程  按文档序收集结果 → 统一写译文文件与 manifest
```

`ask` 是一次 HTTP 调用，等待时间远大于本地计算，用标准库的线程池即可，不引入进程池或 asyncio。

| 项 | 取值 | 理由 |
|---|---|---|
| 并发度 | `JOBS` 默认 4，`--jobs` 覆盖 | 上限由 API 的速率限制决定，不由本地核数决定，所以给可覆盖的默认值而不是按核数推算 |
| 写盘 | 只在主线程 | worker 只返回结果，不碰 `build/translated/`，不需要加锁 |
| 重试 | 在 worker 内环 | 一个 chunk 的重试不阻塞其他 chunk |
| 结果顺序 | 按文档序落盘 | 产物与串行执行逐字节相同，并发度不影响产物 |
| worker 异常 | 记为该 chunk 的 fallback | 网络错误与重试耗尽同等对待，不取消其余任务 |

默认取 4 的依据：实测单 chunk 47–89 秒，一篇论文 40 余个 chunk 串行要跑 40 分钟以上，并发 4 降到 10 分钟量级。再往上调收益递减且更容易撞速率限制，需要时用 `--jobs` 调。

## 出口判定

单个 chunk validate 不通过仍然回退原文继续跑，「保证总能产出 PDF」这条规则不变。但回退的数量本身是质量信号：回退掉一半 chunk 的译文拿去编译，出来的 PDF 大半仍是英文，这种情况判成功没有意义。

```
fallback_ratio = fallback 的 chunk 数 / 参与翻译的 chunk 数
                 （跳过判定命中的 chunk 不计入分母）

fallback_ratio > FALLBACK_RATIO_MAX（默认 0.2，--max-fallback-ratio 覆盖）
    → translate 阶段判失败，退出码非零，不进入 compile
```

判定在全部 chunk 跑完之后做，不提前中断：中断只会丢掉已经付过钱的译文，而剩下的 chunk 本来也要翻。

译文文件与 manifest 无论判定结果如何都照常落盘——判失败是给退出码看的，不是不落产物；`fallback_ratio` 与逐 chunk 的 status 都在 manifest 里，供排查用。

## 上下文组装

六个组件，按 chunk 的 `part` 组合不同。核心原则：**不给 chunk 重复它自己已经包含的信息**。

| 组件 | 来源 | front chunk | body / appendix chunk |
|---|---|---|---|
| `chunk` | `build/chunks/<id>.tex`，去首尾空白 | 有 | 有 |
| `heading_tree` | `build/brief.json` 的 `heading_tree` 字段 | 有（展开缩写） | **不带** |
| `brief` | `build/brief.json` 的 `abstract` 字段 | **不带**（自身即摘要） | 有（全局语境） |
| `neighbors` | 前一 chunk 末 N 段 + 后一 chunk 首 N 段，源文本 | **不带**（首个 chunk） | 有（局部连贯） |
| `terms` | `build/glossary.json` 按 chunk 命中过滤 | 有 | 有 |
| `style` | `build/glossary.json` 的 `style` 字段 | 有 | 有 |

### heading_tree 放进 brief.json

heading_tree 是论文结构的全局信息，由 survey 阶段扫 `masked.tex` 得出后写入 `build/brief.json`（来源与深度口径见 [stages/survey.md](survey.md) heading_tree 节）。每条标题带 `depth`（相对层级深度）、`level`（标题命令名）与 `argument`（标题参数原文）：

```json
{
  "abstract": "...",
  "heading_tree": [
    {"depth": 1, "level": "section", "argument": "Introduction"},
    {"depth": 1, "level": "section", "argument": "Architecture"},
    {"depth": 2, "level": "subsection", "argument": "Basic Architecture"},
    ...
  ]
}
```

提示词里它排成缩进列表，每级两个空格，只出 `argument`——层级由缩进表达，命令名对模型没有额外信息。

只在 front chunk 的提示词里引用它。正文 chunk 不引用——实测证明正文 chunk 带标题树要么引发 5.5 倍推理膨胀，要么白花输入 token 但无质量收益。

### neighbors

前一 chunk 末 `NEIGHBOR_PARAGRAPHS`（3）段 + 后一 chunk 首 3 段，源文本。不取译文——避免级联失效（ARCHITECTURE 附录 A.11）。首个 chunk 无前邻，末个 chunk 无后邻。跨 part 边界允许。

### 空白管理

送进 ask 的是去掉首尾空白的 chunk 正文，写回时由驱动器把首尾空白原样接上。validate 只比对正文，段落数这一层不被首尾换行搅混。

### 跳过翻译

chunk 的 `translatable_chars` 为 0 时（纯 placeholder chunk），不调 ask，原文直接作为译文写入。

## validate 四层

全部机械，不含判断。层名与 report 的 `validation.failures_by_check` 键一一对应：

| 层 | 判定 |
|---|---|
| `placeholders` | `⟦BLK-n⟧` / `⟦CAP-n⟧` multiset 相等 + 残缺自检 |
| `control_sequences` | `\cmd`（含星号变体）与 `\符号` multiset 相等 |
| `braces_and_math` | 未转义的 `{` `}` `$` `%` 计数分别相等 |
| `paragraph_count` | 含可译文本的段落数相等（口径定义见 [stages/chunk.md](chunk.md) 段落计数的两个口径节） |

「含可译文本」的判定是逐段剥四类结构标记后看还有没有非空白字符：placeholder、`\begin{X}` / `\end{X}` 连同 `\begin` 后面的可选参数与花括号参数组、`NON_TEXT_ARGUMENT_COMMANDS` 里那些命令连同它们的参数、其余控制序列的命令名（参数保留）。第三类是一份清单而不是启发式规则：`\section{Introduction}` 的参数要译、`\vspace{-0.4in}` 的不要，两者机械上无从分辨，只能把后一类列成数据。清单只收实际在语料里单独成段的命令，未列出的命令仍按「参数是正文」处理——那个方向的误差只多一次重试，反方向会让漏译静默通过。

`%` 与前三个字符一同数，理由与它们不同：它是 LaTeX 的注释符，译文里多一个未转义的 `%` 会把那一行剩下的内容连同后面的命令一起注释掉，编译不报错、正文安静地少一截。这是模型想写百分号却忘了转义时的默认后果，而 `%` 不是控制序列、也不进花括号计数，此前四层一层都不响。掩码阶段已把注释全换成 placeholder，因此原文侧这个计数恒为 0。

四层管不到的一类污染是**整段包在代码围栏里**：反引号不是控制序列，也不改花括号与 `$` 的计数，四层一层都不响。驱动器因此在跑 validate 之前先剥一次围栏（首行 ``` 开头、末行 ``` 结尾即整体去掉）。提示词里写了「只输出译文本身」，但那是判断，不是判据。

validate 同时是 CLI 子命令 `tongtu validate`，供开发者手工排查，与驱动器走同一份实现。

## 复用粒度

**零期不做 chunk 级翻译记忆**，复用的粒度就是整个阶段：跳过判定的六个值全都对得上才跳过，任一变了就整篇重翻。

```
跳过 ⟺ manifest 状态 ok
      ∧ chunks_sha256 / glossary_sha256 / brief_sha256 三个输入 hash 一致
      ∧ model_id 与 prompt_version 一致
      ∧ 上次的 fallback_ratio 仍在本次阈值之内
      ∧ 全部译文文件存在
```

阈值也进判定：上次按 20% 判 ok 的结论，在这次给出 `--max-fallback-ratio 0.1` 时不再成立，跳过它就等于让一次本该失败的执行静默退 0。

chunk 级缓存（改一个术语只失效命中它的 chunk）的设计留在 ARCHITECTURE §4，实现推迟：那套机制要维护 key 的构成、要素快照、`key_version` 与它的迁移路径，而换来的只是重翻一篇论文的调用费——实测一篇 10 个 chunk 的论文全量翻译不到五分钟。真正需要增量重翻时与 `retranslate` 子命令一起做，决策见 ARCHITECTURE 附录 A.33。

因此 `--force` 就是整篇重翻，不存在「跨过阶段判定但仍命中记忆」的中间状态。

## 关键取舍

- **翻译用 `ask`，重试由脚本驱动**。翻译的输入输出形态固定，不需要会话运行时。validate 失败的重试是机械循环。实测 8 次调用 validate 全部通过。决策见 ARCHITECTURE 附录 A.32。
- **heading_tree 只给 front chunk**。正文 chunk 带标题树引发推理膨胀（5.5 倍）或白花输入 token，翻译质量无可见差异。摘要 chunk 需要标题树展开缩写（1.3 倍成本即有收益）。
- **heading_tree 放进 brief.json，由 survey 直接扫掩码文本得出**。它是论文结构的全局信息，与 abstract 同属 survey 产出的全局语境；放在 brief 里结构更干净，且随 `brief_sha256` 自然参与跳过判定。不从 chunk manifest 汇总：标题结构是分块的输入而不是分块的产物，survey 与 chunk 共用同一份扫描实现、各自直接读 `masked.tex`，两个阶段因此不互相依赖，阶段序不变。
- **chunk 级并发，标准库线程池，默认 4**。chunk 之间无数据依赖，直接并发即可。`ask` 是 IO 等待，线程池够用；写盘收在主线程，不需要加锁。并发度由 API 速率限制而非核数决定，因此是可覆盖的默认值。
- **单 chunk 不通过不终止，回退超阈值整体判失败**。单个 chunk 回退原文并记入 report，与 compile 里 fallback 是同一条规则：保证总能产出 PDF。但回退比例超过 `FALLBACK_RATIO_MAX`（默认 20%）时 translate 判失败、不进入 compile——大面积回退产出的 PDF 大半仍是英文。
- **默认模型 muse-spark，可选 deepseek-v4-pro**。两者 validate 通过率相同。muse-spark 输出 token 更少、成本更低。`--model` 参数可切换。
- **零期不做 chunk 级翻译记忆**。复用粒度是整个阶段，`--force` 即整篇重翻。理由见上文复用粒度节与 ARCHITECTURE 附录 A.33。
- **neighbors 只用原文**。前一个 chunk 的译文不进后一个 chunk 的提示词，理由见 ARCHITECTURE 附录 A.11。
- **术语一致性零期不进 validate**。四层校验只管结构。将来若加，方向是不译词的保留作硬判据、canonical translation 作 report warning。
- **compile 阶段不重译，也不回退**。编译修复会话遇到译文引起的编译错误时由 agent 就地改 `zh.tex`，改不动就判失败；原设计里的 `tex retranslate` 与 `tex fallback` 零期都不实现，理由见 [stages/compile.md](compile.md)「零期不实现 fallback」节。

## 实测数据

论文 2412.19437（DeepSeek-V3），方案 B（heading_tree 只给 front），reasoning_effort=low，validate 四层全部通过。

| 模型 | chunk | 输入 token | 输出 token | 耗时 | validate |
|---|---|---|---|---|---|
| deepseek-v4-pro | c000 (front) | 2,420 | 937 | 13.9s | PASS |
| deepseek-v4-pro | c001 (body) | 4,208 | 4,912 | 70.8s | PASS |
| deepseek-v4-pro | c002 (body) | 5,693 | 9,526 | 88.8s | PASS |
| deepseek-v4-pro | c003 (body) | 4,925 | 7,457 | 77.8s | PASS |
| muse-spark | c000 (front) | 2,442 | 1,511 | 66.9s | PASS |
| muse-spark | c001 (body) | 4,235 | 2,965 | 61.7s | PASS |
| muse-spark | c002 (body) | 5,687 | 4,556 | 77.9s | PASS |
| muse-spark | c003 (body) | 4,910 | 2,945 | 47.8s | PASS |

muse-spark 的输出 token 是 deepseek 的一半（推理 token 少），总耗时相当，翻译质量与 validate 通过率相同。deepseek 的输出 token 波动大（c001: 4,912 vs c003: 7,457，译文长度相近），推理开销不稳定。

三种上下文方案的对比实验结论：方案 A（full_tree）在正文 chunk 上 output token 5.5 倍、耗时 4.5 倍；方案 C（tree_in_brief）不引发膨胀但多花输入 token 且无质量收益；方案 B 成本最低且 validate 全通过。

## 状态与退出码

`TranslateStatus`：`ok` / `chunk_missing` / `chunk_not_ok` / `survey_missing` / `survey_not_ok` / `translate_failed`（fallback 比例超过阈值或 ask 原语连续失败导致无法产出任何译文）。退出码：`ok` → 0；上游 pdf_only 沿链 → 3；其余 → 1。

存在 fallback chunk 但比例未超阈值时状态仍是 `ok`，fallback 记入 manifest 的 `fallback_chunks` 清单与 report。

## 重跑语义

- **输入 hash**：`chunks_sha256`（从 chunk manifest 转录）、`glossary_sha256` 与 `brief_sha256`（从 survey manifest 转录）。
- **输出 hash**：`translated_sha256`——按文档序连接各译文文件的 sha256 再取 sha256。
- **跳过判定**：六个值全都对得上才跳过，清单见上文复用粒度节。
- 失败状态不跳过；`--force` 无视已有结论整篇重翻。每次非跳过的执行开始先整目录删除 `build/translated/`。

## 产物模型

manifest 即 `TranslateManifest`。承担契约职责的字段：`status`；输入 hash 三个；输出 hash `translated_sha256`；`model_id`（本次使用的模型标识）；`prompt_version`（SKILL 版本）；`jobs`（本次使用的并发度）；`chunks` 列表（每条含 `id`、`status`（translated / fallback / skipped）、`sha256`、`attempts`、`failures`）；`fallback_chunks`（回退的 chunk id 清单）；`fallback_ratio` 与 `max_fallback_ratio`；`skipped_chunks`；上游状态转录。

`attempts` 是本次执行对该 chunk 的 `ask` 调用次数，1 即一次通过，跳过翻译的 chunk 记 0；`failures` 是回退时最后一次 validate 未通过的层名与差异说明，或 `ask` 的失败现场（后者不附进重试的提示词——超时与 429 对模型没有意义）。翻译次数是排查提示词与模型的第一个观察值，逐次调用的完整请求与返回落在 `logs/translate-<chunk-id>-<第几次>.json`。

## 验收与试跑对象

translate 是零期第一个花钱的阶段，因此验收分两层：不调 `ask` 的那部分全部机械覆盖，调 `ask` 的部分靠真实论文人工核对。

**文本层（无外部依赖，合并必过）**：四层 validate 的层边界、段落口径与标题树扫描在 `tests/text/test_validation.py`；驱动器不调 `ask` 的四组逻辑在 `tests/text/test_translate_driver.py`——前置条件的四条分流与各自的状态（chunk 两条排在 survey 两条之前，两者同时不满足时报 chunk 那条，否则 pdf_only 沿链的退出码会从 3 掉成 1）、跳过判定的六个值各自变一个都不跳过、上下文组装按 part 分派（front 拿标题树不拿摘要与邻段，body 与 appendix 反过来）、`fallback_ratio` 的分母不含跳过翻译的 chunk。

**LLM 层（需要密钥，手动执行）**：真实论文按 [examples/README.md](../../examples/README.md) 的顺序，`2002.05202`（2 个 chunk，结构最简单）先跑通，再上 `2412.19437`（10 个 chunk，多级 `\input` 加自定义类）。判据是状态 ok、回退比例 0、译文与原文的四层 validate 全绿，以及人工读一遍译文确认没有中英混杂与漏译。`attempts` 大于 1 的 chunk 要翻 `logs/translate-<chunk-id>-<第几次>.json` 看第一次挂在哪一层，那是提示词的改进线索。

三篇自造论文在 mock 运行时接线之后进文本层；在此之前它们不跑 translate。
