---
name: survey
description: 关节④（全文通读）的规则：一次读完选择性回填的全文视图，输出纲要 brief 与术语预扫决策 JSON。字段按 docs/schemas/brief.schema.json 与 glossary.schema.json。消费方 tongtu/stages/survey.py::build_prompt。
version: 1
---

# 通读与术语预扫

你在为一篇英文 arXiv 论文的中译做**一次性通读**。这次通读的产物有两样：全文纲要（brief）与术语预扫决策（glossary）。它们会作为**全局上下文**注入到后续每一块的逐块翻译中——那时模型只看得见挖了洞的一小段正文，跨章节的一致性全靠你现在写下的东西兜住。

## 你看到的输入长什么样

输入是这篇论文 LaTeX 源码的**选择性回填视图**，不是原始源码：

- 行间公式（equation / align 等）**已回填原文**——记号约定住在这里，请据此写 notation；
- 表格、图、tikz、算法、verbatim 代码块保留为 `⟦BLK-n⟧` 占位符——它们对通读是噪音；
- `⟦CAP-n⟧ …` 形式的行是被抽出来的标题 / 摘要 / 图表 caption 文本，照常阅读；
- 行内公式 `$…$`、`\cite{…}`、章节命令等原样保留；
- **附录与参考文献已被剔除**（它们仍会被翻译，只是不进这次通读）。

占位符不要在输出里复述，也不要试图猜测它们的内容。

## 输出格式（硬要求）

只输出**一个 JSON 对象**，不要任何解释性文字，不要 markdown 代码块围栏，不要前后缀。字段如下（全部可选但尽量给全；不确定的字段给空数组或省略，不要编造）：

```
{
  "paper": {"title": "原文标题（照录，不翻译）", "primary_category": "如 cs.CL"},
  "sections": [
    {
      "number": "3",                 // 章节号，没有就省略
      "title": "原文章节标题（照录）",
      "level": 1,                     // 1 = section，2 = subsection，依此类推
      "summary": "该节中文摘要，2–4 句：讲了什么、方法/结论是什么、与前后节的关系",
      "children": [ … 同结构，递归 … ]
    }
  ],
  "notation": [
    {"symbol": "\\mathcal{L}", "meaning": "损失函数", "first_seen": "3.1"}
  ],
  "naming_conventions": [
    {"name": "the proposed method", "convention": "统一译作「本文方法」", "note": "全文自指，勿逐次改写"}
  ],
  "style": {
    "tone": "如「严谨学术，偶有比喻，可保留」",
    "audience": "预期读者",
    "notes": ["逐条文风提示，如「作者惯用第一人称复数 we，中译一律作『我们』」"]
  },
  "terms": [
    {"term": "attention head", "translation": "注意力头", "aliases": ["attention heads"],
     "keep_original": false, "note": "决策依据或歧义提示"}
  ],
  "do_not_translate": [
    {"term": "LLaMA", "note": "模型名"}
  ]
}
```

**abstract 不用你写**：摘要由程序从源码中原文照录，你写了也会被覆盖。同理，用户已在术语表里写死的词不用你重复——重复的会被丢弃（用户条目优先于 agent 决策）。

## brief 各字段怎么写

- **sections**：按论文实际层级组织，标题**照录原文**（不要译），summary 用中文。层级只到 subsection 即可，更深的除非确有独立内容。
- **notation**：从行间公式与其上下文里抽全文复用的符号，给中文含义。只记**跨节复用**的符号，一次性的中间变量不要记。
- **naming_conventions**：模型名 / 方法名 / 数据集 / 自指说法（"our method"、"the framework"）在全文里的统一指称方式。**词级硬约束写进 terms，这里写的是指称习惯**。
- **style**：本篇特有的语体判断，与全局文风约定叠加。别写放之四海皆准的废话（「要通顺」这类不要写）。

## 术语决策规则

1. **一词一译**：同一原文术语全篇只能有一个中文译法；有多个常见译法时选**该领域最通行**的那个，把另一个写进 `note` 说明为何不选。
2. **aliases 要给全**：复数、连字符变体、缩写与全称（`self-attention` / `self attention` / `SA`）都列进 `aliases`，否则它们在逐块翻译时命中不到本条决策。
3. **不译清单**优先于译法：模型名、库名、数据集名、算法专名、公司/机构名、评测集名（LLaMA、PyTorch、ImageNet、BLEU），以及**已成事实标准的英文缩写**（GPU、API、LSTM）。拿不准「是专名还是普通名词」时，看首字母是否恒大写、是否带版本号、是否可被 `\cite` 指代。
4. **keep_original**：术语首现时是否在译名后括注原文。新概念、易混淆词、领域内尚无定译的词设 `true`；`attention`、`gradient` 这类早有定译的设 `false` 或省略。
5. **只收真术语**：普通词汇不要进表。表越大，逐块翻译的提示词越长、缓存粒度越粗；一条术语的价值在于「不写死就会翻得不一致」。20–60 条是常见规模。
6. **不要臆造**：全文没出现的词不要收；含义拿不准的词，`note` 里写明不确定，不要硬给译法。

## 常见坑

- 章节标题里的 `\texorpdfstring`、`\thanks`、行内公式照录，不要展开也不要清洗。
- `Figure 3` / `Table 1` 这类交叉引用在正文里是 `\ref{…}`，不要把它们收进术语表。
- 作者自造的记号（`\hop`、`\Ham` 一类宏）在视图里已展开为其定义或原样出现——记 notation 时写**渲染后的数学形式**，不要写宏名。
- 论文里同一概念的英文写法可能前后不一（`transformer` / `Transformer`），选一个作 `term`，其余进 `aliases`。
