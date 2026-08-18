# survey —— 合并术语表，照录全局语境

> 阶段定位见 [ARCHITECTURE.md](../ARCHITECTURE.md) §3 阶段表，本文是 survey 阶段设计的权威。实现分两处：`tongtu/glossary.py` 是术语表的解析、合并与命中匹配（核心文本层，translate 的逐 chunk 命中复用同一实现），`tongtu/stages/survey.py` 是阶段驱动器（前置条件、跳过判定、落盘）。manifest 与两个产物的字段级权威定义在 `tongtu/artifacts/survey.py`（pydantic model，文档不复述字段表）。

**要解决的问题**：逐 chunk 翻译时每个 chunk 独立进会话，翻译前需要两样全篇共享的输入：术语表（约束词的译法与保留原文的词）与全局语境（让模型知道这篇论文在讲什么）。survey 把三层用户术语表合并、按全文命中过滤成本篇的 resolved glossary，并照录论文摘要成 brief。零期本阶段不拉起 agent：模型读全文预扫术语（hook④）推迟（见「hook④ 推迟」节），术语全部来自用户输入。两个产物的消费方都是 translate（提示词组装与缓存 key）；chunk 阶段不消费 survey 的产物，二者只共享上游 `masked.tex`。

**输入 → 输出**：`build/masked.tex` + `build/blocks.json` + input glossary 三层 → `build/glossary.json`（resolved glossary，artifact contract 的一员）+ `build/brief.json`（同为 artifact contract 的一员）+ `build/manifests/survey.json`。survey 只读 `build/` 与术语表输入、只写 `build/`：纯文本变换，不访问网络、不编译、不拉起 agent。

## 前置条件

沿用 fetch 起确立的约定：前置失败也写 manifest，驱动器不抛栈，每次执行的结论都落盘。

- mask manifest 缺失或不可解析，或状态是 ok 但 `build/masked.tex` 与 `build/blocks.json` 有缺 → 状态 `mask_missing`，message 提示先跑 `tongtu stage mask`。
- mask 状态不是 ok → 状态 `mask_not_ok`，转录 mask 的状态与它记录的上游状态（pdf_only 沿链退 3，与上游各阶段同构）。
- 任一 input glossary 文件不可解析或不符合形状 → 状态 `glossary_invalid`，message 指出文件路径与首个错误。

## input glossary 与合并语义

三层输入，优先级从低到高，每层都可缺席：

1. 全局配置目录的 `glossary.json`（`~/.config/tongtu/`，见 ARCHITECTURE §5 目录约定节）；
2. 论文工作目录的 `glossary.json`（与 `src/`、`build/` 同级，用户手写，默认不存在）；
3. 命令行 `--glossary FILE`（可多次，靠后的优先；`run` 与 `stage survey` 同语义）。

文件形态三段，皆可缺省：`do_not_translate`（字符串列表）、`terms`（词到译法的映射）、`style`（一段写给译者的额外要求，字符串，原样进 translate 的提示词；定义与边界见 ARCHITECTURE §5 术语表节，决策见附录 A.27）。

合并规则：

- **词条按词覆盖，跨区段同样覆盖**。`do_not_translate` 视为「译法 = 保留原文」的词条：同一个词在多层的 `terms` 与 `do_not_translate` 中出现时，以最高层的记录为准——高层可以把低层的不译词改为给出译法，也可以反向。不做并集。
- 词的同一性按大小写不敏感比较，resolved 保留胜出层的原写法。
- 同一份文件内同一个词同时出现在 `terms` 与 `do_not_translate` → `glossary_invalid`（配置错误，不猜测意图）。合并单元是一份文件而不是一个层级：全局与论文目录各只有一份文件，两者等同；命令行可给多份，同一个词在两份命令行文件里分处两个区段由「靠后的优先」判定，不算冲突。
- **`style` 整段覆盖**，与词条无关：写了这一段的最高层整段胜出，不与低层拼接；最高层写的是空白即本篇不要低层配的那段要求，resolved 的 `style` 为 null。三层都没写同样为 null——survey 不产 style，也不造默认值，合并结果原样进 resolved。

## 全文命中过滤

合并后逐条在 `masked.tex` 全文查命中，未命中的词条不进 resolved：翻译只发生在掩码文本上，全文都不出现的词不会命中任何 chunk，留在 resolved 里没有消费方。过滤让 resolved 成为「这篇论文实际生效了什么」的可读记录。

- 命中判定与 translate 的 `relevant_terms(chunk)` 是同一份实现（`tongtu/glossary.py`），零期为大小写不敏感的子串查找；将来精细化（词边界、词形变化）只改这一处，过滤与逐 chunk 命中同步变化，不会出现「survey 滤掉了、translate 本可命中」的分歧。
- 被过滤的条目记入 manifest 的 `filtered` 清单（词 + 来源层），配置不静默消失。
- `style` 不参与过滤。

## brief 照录

brief 零期只有一个字段：`abstract`，论文原文摘要的照录，由程序提取，不经模型。提取两条来路，按序尝试：

1. `blocks.json` 中 kind 为 `abstract` 的 caption 槽位（摘要写在前导区的文档类，mask 阶段已抽出），取其原始文本；
2. 槽位不存在时，在 `masked.tex` 中扫描 `abstract` 环境（正文形态，多数论文如此），照录环境体，仅去除首尾空白。

两条都落空 → `abstract` 为 null，不是失败；来路记入 manifest 的 `abstract_source`（`preamble_slot` / `body_environment` / `absent`）。

将来的全局语境扩展（章节结构树、模型生成的字段）都落在这个文件里，`brief_sha256` 的语义不变。

## 出口判据

三条同时成立才是 ok：合并与过滤完成且不变量成立（resolved 的每条词目都能追溯到某输入层且在全文命中；每个未进 resolved 的输入词目都出现在 `filtered` 清单）；abstract 照录完成（null 也算完成）；两个产物与 manifest 落盘并通过 artifact model 校验。

## 状态与退出码

`SurveyStatus`：`ok` / `mask_missing` / `mask_not_ok` / `glossary_invalid`。退出码：`ok` → 0；`mask_not_ok` 且沿链 fetch 状态是 `pdf_only` → 3（跨子命令同码同义）；其余 → 1。

## 重跑语义

- **输入 hash 是三个值**：`masked_sha256` 与 `blocks_sha256`（从 mask manifest 转录，上游输出 hash 的权威），加 `glossary_input_sha256`——三层输入按层序规范化序列后的 sha256，缺席层以空占位，全局配置文件的内容也参与（它在工作目录外，改动它必须触发重跑）。
- **输出 hash**：`glossary_sha256` 与 `brief_sha256`，下游判定「输入未变不重算」的权威；`brief_sha256` 即 ARCHITECTURE §4 缓存 key 中 `brief_hash` 的权威。
- **跳过判定**：survey manifest 存在、可解析、状态 ok、三个输入 hash 与当前值一致、两个产物都存在 → 跳过。不校验产物内容与 manifest 是否一致（初期简化，同上游各阶段）。
- 失败状态不跳过；`--force` 无视已有结论。每次非跳过的执行开始先删除已有的 `glossary.json` 与 `brief.json`，失败时不留上次的产物误导下游。

## hook④ 推迟

模型读全文预扫术语并对未覆盖的词做译法决策（hook④，`ask` 原语）推迟实现。理由：术语不一致不是不可逆损伤——发现后往 input glossary 补一条，`retranslate --term` 只重翻命中该词的 chunk，修正回路已在架构中；先拿真实译文确认不一致的实际频率与严重度，再决定是否值得这次模型调用。重启条件 = 真实译文反复出现同词不同译、手工补表跟不上。接线形态已定：合并层最底端增加一层「survey 决策」（优先级低于全部用户层），模型调用按 `masked_sha256` + `prompt_version` + `model_id` 缓存，产物与下游形态不变。

## 产物模型

manifest 即 `SurveyManifest`。承担契约职责的字段：`status` 是唯一分流依据；`masked_sha256` / `blocks_sha256` / `glossary_input_sha256` 是输入 hash；`glossary_sha256` / `brief_sha256` 是输出 hash；`terms_total` / `do_not_translate_total` 是 resolved 两类词条的计数；`filtered` 是未命中被过滤的条目清单（词 + 来源层）；`abstract_source` 是照录来路；`mask_status` 与 `fetch_status` 转录本次看到的上游状态（退出码映射与排查用）。

`build/glossary.json` 即 `GlossaryFile`（resolved glossary）：`terms` 列表（词、译法、`decided_by`）、`do_not_translate` 列表（词、`decided_by`）、`style`（字符串或 null）。`decided_by` 取值 `global` / `paper` / `cli`，记胜出层；hook④ 接线后增 `survey`。

`build/brief.json` 即 `BriefFile`：`abstract`（字符串或 null）。

## 验收与试跑对象

十一篇（三篇自造论文 + [examples/README.md](../../examples/README.md) 真实论文表八篇）全部要求：状态 ok、重跑命中跳过、`--force` 重算。`1412.6980`（pdf_only 套壳）走 `mask_not_ok`、退出码 3。专项核对：无任何输入表时 resolved 为空词表、`style` 为 null，brief 照常产出；有摘要的论文 `abstract` 非空且与源码摘要一致，`abstract_source` 与 blocks.json 的实际形态相符；构造三层输入验证按词覆盖（高层 `terms` 推翻低层 `do_not_translate`）与 `style` 整段覆盖（含高层写空白即清空）；构造全文不出现的词验证过滤与 `filtered` 清单；构造同一份文件内的冲突验证 `glossary_invalid`。真实论文源码不入库。
