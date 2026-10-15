---
name: translate_session
description: 在一次会话里按顺序翻译整篇论文的全部 chunk，同时建立并维护术语表。
version: 1
---

# 整篇翻译会话

你在一个工作目录里（当前目录），要把一篇 arXiv 论文的英文 LaTeX 正文完整译成简体中文。论文已经按章节切成若干块，每块一个文件。你按顺序一块一块翻译，全篇由你一个人完成，所以前后的术语、说法和语气都由你负责保持一致。

## 目录里有什么

- `chunks/cNNN.tex`：英文原文，已掩码。只读，不要修改。
- `paper.json`：摘要原文、章节标题树，以及 `chunks` 列表（每块的 id、所在部分、包含的标题、可译字符数）。只读。
- `tables.tex`：被掩码掉的表格原文，供参考。只读。
- `glossary.json`：术语表。开始时只有用户事先定好的条目（`source` 为 `user`），这些条目不许修改或删除。其余条目由你建立和维护。
- `translated/`：输出目录，开始时为空。每块译文写成 `translated/cNNN.tex`，文件名与原文相同。
- `progress.md`：进度记录，由你维护。
- `.skills/translate_session/rules.md`：逐块翻译的规则与范例。第一节到第七节全部适用；第八节「输出格式」不适用，输出方式以本文件为准。
- `.skills/translate_session/validate.py`：机械校验脚本。

开始工作前先读完 `rules.md`。

## 工作顺序

1. **通读。** 读 `paper.json`，再把 `chunks/` 下的全部文件从头到尾读一遍，弄清论文讲什么、有哪些反复出现的术语和作者自造的名词。也看一下 `tables.tex`。
2. **先译摘要。** 摘要在 `⟦CAP-n⟧` 开头的那一行（`paper.json` 的 `abstract` 是它的原文）。先把摘要所在的块译完并通过校验。摘要是作者认真写的全文概括，它的译法是后面所有块的参照。
3. **建立术语表。** 根据通读和摘要译文，把需要全篇统一的词写进 `glossary.json`，格式见下文。
4. **按 `paper.json` 的 `chunks` 顺序翻译其余各块。** 每译完一块：
   - 写入 `translated/cNNN.tex`；
   - 运行校验，不通过就修改这个文件，直到通过；
   - 在 `progress.md` 里记一行：块 id、是否通过、新增了哪些术语。
   翻译中遇到术语表没有的、需要统一的词，补进 `glossary.json`。
5. **收尾检查。** 全部译完后，用命令检查：每个 `chunks/` 文件在 `translated/` 都有对应文件；术语表里每条术语在全部译文中只用了一种写法；首次出现的括注（见下文）只出现一次、位置正确。发现问题就改，改完重新校验对应文件。

`translatable_chars` 为 0 的块全部是占位符与命令，原样复制原文即可。

会话可能很长，上下文可能被压缩。术语决定只以 `glossary.json` 为准，进度只以 `progress.md` 为准；继续工作前先读这两个文件。

## 校验

每写完或修改一个文件，立即运行：

```
python3 -I .skills/translate_session/validate.py chunks/cNNN.tex translated/cNNN.tex
```

四层机械校验（占位符、控制序列、花括号与 `$` `%`、段落数）全部通过才算完成。会话结束后驱动器会对每个文件重跑同一份校验，不通过的文件整块退回英文原文。

## 术语表格式

```
{
  "terms": [
    {"en": "large language model", "variants": ["LLM", "LLMs", "Large Language Models"],
     "zh": "大语言模型", "annotate_first": true, "source": "session"}
  ],
  "keep_english": [
    {"word": "token", "source": "session"}
  ]
}
```

- `terms`：需要统一译法的词。`variants` 列出原文里实际出现的其他写法（缩写、复数、大小写不同的写法）。
- `keep_english`：保留原文、不译的词。模型名、数据集名、基准名、表格里出现的指标名与方法名，以及领域惯用英文（token、kernel、KV cache 一类）。
- 表格不翻译。`tables.tex` 里出现的模型名、指标名、方法名，在正文里提到时保持表格中的写法，放进 `keep_english`。表格里的普通英文单词（Average、Accuracy 一类）在正文里照常翻译。
- `source` 为 `user` 的条目优先于你自己的判断。

## 首次出现加原文

`annotate_first` 为 true 的术语，在正文第一次出现时写成「中文（英文全称）」；原文同时给了缩写的，写成「中文（英文全称，缩写）」。之后的出现只写中文，或按原文写缩写。

- 摘要单独算一次，正文从头再算一次。
- 标题和图注（`⟦CAP-n⟧` 行）里不加括注，顺延到正文中的下一次出现。
- 原文已经在这里写出了英文（例如 `Large Language Models (LLMs)`），按原文译出即可，不要重复加。
- `keep_english` 里的词不加括注。
- 括注只加普通文字和全角括号，不加任何 LaTeX 命令，不影响校验。

## 规则

1. 只在当前目录读写。只写 `translated/`、`glossary.json`、`progress.md`，不改其他文件。
2. 每块都要完整翻译。不要概括、不要省略段落、不要因为内容重复或像模板而跳过。
3. 不要写脚本批量生成译文；译文由你逐块写出。脚本只用来检查。

## 结束时

用一段话说明：译完了多少块，有没有未通过校验的块，术语表有多少条，遇到了什么困难。
