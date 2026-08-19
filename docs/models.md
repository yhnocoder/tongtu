# 翻译模型与推理强度

> 本文件记录 `ask` 原语在翻译类调用上的选型结论与实测依据，供 translate 阶段设计与实现引用。
> 实验脚本与译文产物不入仓库，本文件保留可核对的数字与失败形态。

## 选定

| 项 | 取值 |
| --- | --- |
| 模型 | `deepseek-v4-pro`（chat/completions 端点家族）、`muse-spark-1.2-contributor`（responses 端点家族） |
| 推理强度 | low |
| 摘要 chunk 的附带上下文 | 章节标题树 |

三项都由实测定，依据见下。`deepseek-v4-flash` 不再作为翻译候选。

## 实测方式

两轮实验共 30 次真实调用，语料取自验收语料的三篇（`2412.19437`、`2409.19606`、`1701.06538`），
翻译对象是 chunk 阶段产出的 front chunk（摘要）。提示词一律取 `skill/translate/SKILL.md`，
各条件之间只差一个自变量。每次调用记录输入与输出 token、耗时，并对译文机械核对三项：
placeholder 与控制序列的 multiset、段落数、关键术语的译法。

- 第一轮（18 次）：自变量是附带上下文的档位（只给摘要 / 摘要加章节标题树 / 摘要加全文正文）
  与模型（`deepseek-v4-flash`、`deepseek-v4-pro`），不传推理强度。
- 第二轮（12 次）：附带上下文固定为章节标题树，自变量是推理强度（low、medium）与模型
  （`deepseek-v4-pro`、`deepseek-v4-flash`、`muse-spark-1.2-contributor`）。

## 排除 deepseek-v4-flash

它是两轮实验里格式遵循最差的一个，且失败不限于无害形态：

- 第一轮 `2409.19606` 附带全文正文那一次，它把整篇正文一并译了出来，译文比原文多出 51 个
  placeholder——附带上下文被当成待译文本处理。
- 第一轮另有一次丢掉 `\newpage` 控制序列。
- 第二轮 medium 档 2/2 失败，其中 `2412.19437` 那次把整块空行全部删光，7 个段落并成 1 个，
  同时是 30 次调用里最贵的一次（17,566 输出 token、134 秒）。

同价位下 `deepseek-v4-pro` 与 `muse-spark-1.2-contributor` 都没有出现过这类丢内容的失败。

## 推理强度取 low

第二轮 12 次调用的格式遵循，按含可译文本的段落口径判定：

| 模型 | 端点家族 | low | medium |
| --- | --- | --- | --- |
| `deepseek-v4-pro` | chat/completions | 2/2 通过 | 1/2 通过 |
| `deepseek-v4-flash` | chat/completions | 2/2 通过 | 0/2 通过 |
| `muse-spark-1.2-contributor` | responses | 2/2 通过 | 2/2 通过 |

low 档 6 次全部通过，3 次失败全部落在 medium 档。placeholder 与控制序列的 multiset 在 12 次里
一次都没有出错，失败形态清一色是空行被吞掉。

强度抬高的代价是输出 token 与耗时同向放大：两篇合计的输出 token，`deepseek-v4-pro` 由 2,307
涨到 10,748（4.7 倍），`deepseek-v4-flash` 由 8,721 涨到 23,023（2.6 倍），
`muse-spark-1.2-contributor` 由 2,264 涨到 4,709（2.1 倍）；`deepseek-v4-flash` 的耗时由 62 秒
涨到 174 秒。多花的这部分换来的是更多的空行被吞，没有可核对的质量收益。

**medium 是不传该参数时的默认档**，由输入 token 反推得出：同一段提示词，deepseek 两个模型在
low 档的输入 token 比 medium 档少 79 个（`2409.19606` 是 2,019 对 2,098，`2412.19437` 是
2,556 对 2,635），而第一轮完全不传该参数时这两篇的输入 token 正是 2,098 与 2,635，与 medium
逐字节相同。该参数会改动服务端注入的内容，不只是改采样行为。`muse-spark-1.2-contributor`
的输入 token 两档相同（2,048 与 2,585），只有输出量变。

## 摘要只附章节标题树

原设想是给摘要翻译附上全文正文，理由是摘要术语与指代密集（摘要写 `we propose MLA`，正文才
展开成 `Multi-head Latent Attention`）。第一轮实测否决了这个设想：

- 附带全文正文的成本是只给摘要的 7.45 倍，附带章节标题树是 1.29 倍。
- 全文正文档位没有产出任何可核对的准确性收益：三篇的关键术语译法与标题树档位一致。
- 它引入了新的失败模式，即上文所述的「附带上下文被当成待译文本译出」。

章节标题树的信息量足以覆盖缩写展开这一类需求（标题里就有全称），成本只多三成，因此选它。

## 空行为什么会被吞

`skill/translate/SKILL.md` 第四节已经写明「空行是段落边界：不合并、不拆分、不跳过、不新增」，
模型在 medium 档仍然会删。被删的位置集中在不含可译文本的行周围——`\end{abstract}` 之前、
`\maketitle` 与 `\newpage` 前后。这类删除对排版没有影响（`\end{…}` 自身终止段落，`\newpage`
是命令），validate 第 4 层因此按含可译文本的段落计数，不按全部段落计数（口径定义见
[stages/chunk.md](stages/chunk.md) 段落计数的两个口径节，判据见 ARCHITECTURE §7）。

口径改动之后，两轮合计的段落失败由 11 次降到 2 次。被消掉的 9 次全是上述无害形态，留下的 2 次
是真的丢了内容（`deepseek-v4-flash` 把正文译出来那次，以及它把两个 `⟦CAP-n⟧` 段并进同一段那次），
新口径没有变成对任何译文都放行的判据。

## 两个端点家族的接入差异

`muse-spark-1.2-contributor` 走 responses 端点（`https://opencode.ai/zen/go/v1/responses`），
与现有 chat/completions 适配路径不兼容，`tongtu/agent/opencode.py` 需要按端点家族分派：

- 调用形态是 `client.responses.create(model=..., instructions=系统提示词, input=待译文本,
  reasoning={"effort": "low"})`。chat/completions 的 `reasoning_effort=` 关键字在 responses
  端点上直接 TypeError。
- 响应没有 `choices` 与 `finish_reason`，正文取 `response.output_text`，状态字段是 `status`。
- usage 的字段名是 `input_tokens` 与 `output_tokens`，与 chat/completions 的
  `prompt_tokens` / `completion_tokens` 不同名，日志与 report 汇总时要归一。
- 服务端的输出约束（`response_format`）在该端点上的形态未实测，翻译类调用不需要它，
  survey 的术语决策若要走这个模型需另行验证。
- 该模型要求工作区级的数据政策同意（提示词与补全可用于改进模型质量，以此换取额度折扣），
  未同意时请求返回 403 `DataPolicyError`。本机已同意；容器与 CI 走同一账号时同样成立，
  换账号需重新确认。

两个模型的 tokenizer 不同：同一段提示词 `muse-spark-1.2-contributor` 报 2,048 个输入 token，
deepseek 报 2,019（low）或 2,098（medium）。跨模型的 token 数不能直接相加或比价，report 的
用量汇总要按模型分列。

## 一处未定的译法倾向

`muse-spark-1.2-contributor` 在两篇里都倾向保留英文原文（`Mixture-of-Experts~(MoE)`、
`Multi-head Latent Attention~(MLA)`、`Supervised Fine-Tuning`、`checkpoint`、`loss` 尖峰），
deepseek 两个模型在同一条件下都译成中文（混合专家、多头潜在注意力）。`skill/translate/SKILL.md`
第五节第 2 条的倾向是拿不准时保留英文，因此前者更贴合现行标准，但这是一次两篇的观察，
不足以作为选型依据，也不排除是提示词遵循程度的差异而非稳定偏好。真实译文积累后再判断
是否需要在提示词里把这条写得更硬。
