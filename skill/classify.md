<!--
用途：关节③（未知环境分类）的 prompt 资产。掩码的分类知识按优先级来自：文档自带声明
      （\newtheorem / \newenvironment）→ tongtu/data/environments.json 分类表 → **本关节** →
      保守默认。前两级都没结论时才轮到你（架构决策 10、§3.1）。
消费方：tongtu/stages/mask.py 的 `arbiter` 回调（EnvArbiter，输入 EnvQuery{name,count,sample}），
      经 agent.complete 原语拉起。回调只认三种回答，其余一律按「不知道」处理。
版本：改本文须 bump tongtu/prompts.py 的 PROMPT_VERSION。
-->

# 环境分类（散文 / 重环境）

给你一个 LaTeX 环境名和它在论文里首次出现处的源码片段。判断这个环境该被**掩掉**还是
**留在翻译流里**，只回答一个词。

## 判据

**prose（散文环境，留在流里）**——环境体主要是**要翻译的自然语言**，且它的 `\begin` /
`\end` 包裹对译文无害：

- 定理类：`theorem` `lemma` `proposition` `corollary` `definition` `remark` `example` `proof`；
- 列表类：`itemize` `enumerate` `description`（`\item` 后面是句子）；
- 摘要与致谢：`abstract` `acknowledgments`；
- 各类「一段话」的自定义环境（`highlight` `keypoint` `takeaway` 之类）。

**heavy（重环境，整块掩掉换成 `⟦BLK-n⟧`）**——环境体是**结构、代码或记号**，翻译它没有意义
且极易破坏编译：

- 数学：`equation` `align` `gather` `multline` `array` `cases` `split`；
- 图表：`figure` `table` `tabular` `tikzpicture` `subfigure` `wraptable`；
- 代码与逐字：`verbatim` `lstlisting` `minted` `algorithm` `algorithmic` `Verbatim`；
- 版式与元信息：`thebibliography` `titlepage` `keywords` `CJK`。

## 判断方法

1. **看环境体在装什么**，别看名字像什么——自定义环境的名字经常与内容无关；
2. 体内绝大部分是句子 → prose；绝大部分是 `&` `\\` 对齐、数字、命令、代码 → heavy；
3. 体内**混杂**（例如一个盒子里既有说明句又有公式）：看句子是否成段、是否值得翻译。
   成段的自然语言占主体才算 prose；
4. 参数复杂、对空白敏感、体内有 `%` 或 `\\` 排版技巧的（listing / verbatim 类），一律 heavy。

## 保守原则（重要）

**拿不准就答 unknown。** 后果是不对称的：

- 该 heavy 的判成 prose：公式 / 代码被送去翻译，占位符与控制序列比对会炸，回退与重试白烧
  token，最坏情况是编译不过；
- 该 prose 的判成 heavy：那一段留英文没被翻译，**覆盖率降低，但绝不损坏**。

答 unknown 时驱动器就按保守默认整块掩码（`category=unknown`），并把它记进报告——报告里
反复出现的环境名，将来会被促升进 `tongtu/data/environments.json` 分类表，变成确定性知识。
所以「不知道」是一个有用的答案，不是失败。

## 输出格式

只输出一个词，三选一，不要解释、不要标点、不要代码块：

```
prose
heavy
unknown
```
