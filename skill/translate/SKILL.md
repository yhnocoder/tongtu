---
name: translate
description: 在工作目录里把一篇已掩码的英文 LaTeX 论文整篇译成简体中文，并用检查命令自查到通过。
---

# 整篇翻译会话

当前目录是一篇 arXiv 论文的翻译目录：

- `en.tex`：掩码后的英文全文，只读，不要改。重环境（公式、图表、代码、算法）、题目与作者信息、行尾的注释符 `%` 已换成占位符；原文的注释文字在这之前已经删除。
- `glossary.fixed.json`：用户写的术语表里本篇用到的条目，只读，不要改。其中的术语和不译词必须照用；`style` 字段是用户给译者的额外要求，必须遵守。
- `glossary.json`：本篇术语表。开始时可能已经有以前的论文学到的条目，可以按本篇调整这些条目，也可以追加新条目。
- `zh.tex`：译文，由你创建。它与 `en.tex` 逐段对应，只是可译文本换成简体中文。
- `parts/`：长论文的译文先按节写在这里，由你创建。
- `notes.json`：结束前由你写出的问题记录，格式见「结束时」。

你只写 `zh.tex`、`parts/`、`glossary.json` 和 `notes.json`。两个术语文件的格式都是 `{"terms": {"英文": "中文"}, "do_not_translate": ["保留原文的词"]}`。同一个词在两个文件里都有时，以 `glossary.fixed.json` 为准。

**译文只有一个标准：读起来像作者本来就用中文写的。** 语义上忠实，信息不丢、不加、不走样；句法上不照搬英文。

## 工作顺序

0. 开始前先看当前目录里有没有 `zh.tex` 或 `parts/`。有，说明上一次会话已经留下了译文，这次接着做，不要从头重写：
   - 有 `zh.tex`：先运行第 4 步的检查命令，看它报告哪些问题。
   - 只有 `parts/`、没有 `zh.tex`：先看 `parts/` 里已有哪些文件、各自对应 `en.tex` 的哪些节，确定还没有译出的部分。
   - 只处理检查报告的问题和还没有译出的部分；已有的译文不改，`glossary.json` 里已有的条目照用。补译的部分照第 3 步写成新的 `parts/NN.tex`，再合并成 `zh.tex`。
   - 补译缺失的部分之前，先在已有译文里查同一术语已经用过的写法，补译时沿用这个写法；已有译文里保留英文的词，补译部分也保留英文。
   - `zh.tex` 与 `parts/` 不一致时以 `zh.tex` 为准。修改 `zh.tex` 之后，把同样的修改同步到 `parts/` 里对应的文件。
   - 做完后照常运行检查命令，改到通过。第 1、2 步只做补译需要的部分，第 5 步的通读只针对这次改动和新译的部分。

   没有这两样时从第 1 步开始。
1. 先通读 `en.tex` 全文，弄清论文讲什么。
2. 定术语，写进 `glossary.json`：本篇反复出现、译法需要全文统一的词写进 `terms`；应当保留英文的词（模型名、数据集名、缩写、领域惯用英文如 token、kernel）写进 `do_not_translate`。方法名、模块名这类专名，全文只用一种写法，把选定的写法记下来：保留英文的写完整名字，译成中文的写进 `terms`。同一词根的词（形容词、名词、动名词）用同一个译法。只收本篇反复出现的词，通用词不收。`glossary.json` 里已有的条目如果不适合本篇，可以改；`glossary.fixed.json` 里的条目不能改。
3. 写译文。**不要用 Edit 逐段替换英文**，那样每段英文都要重复输出一遍。
   - `en.tex` 不超过 500 行：一次用 Write 写出完整的 `zh.tex`。
   - 更长：按 `\section` 把译文依次写成 `parts/01.tex`、`parts/02.tex`……，每个文件对应 `en.tex` 中连续的一段，合起来必须覆盖全文、不重不漏；写完后用 `cat parts/*.tex > zh.tex` 合并。
   摘要、正文、脚注、图表标题（`⟦CAP-n⟧` 行）、附录都要译。**论文题目和章节标题不译**：`\section`、`\subsection`、`\subsubsection`（含星号变体）花括号里的文字原样保留英文，一个字符都不改。`\paragraph{}` 的段首小标题照常翻译。段落与换行的要求见「四、段落结构」。
4. 写完后运行

   ```
   python3 -I .skills/translate/check.py
   ```

   `check.py` 与 `validate.py` 不需要阅读，直接运行并看输出；检查的规则以本文件的说明为准。它对比 `en.tex` 与 `zh.tex`，报告五类问题：结构不一致（`structure`）、行内公式被改动（`formula`）、章节标题被改动（`heading`）、还没翻译的段落（`untranslated`）、术语没按两个术语文件用（`glossary`）。`do_not_translate` 里的词按大小写原样匹配。只对它报告的位置用 Edit 修改，改到它输出 `check passed`。翻译途中新定的术语追加进 `glossary.json`。改动 `glossary.json` 之后必须再运行一次检查：新加的术语会被用来检查全文。改 `glossary.json` 用 Write 或 Edit，不用 Bash 的重定向。
5. 检查通过后，**必须**从头到尾通读一遍 `zh.tex`（对照 `en.tex`），只改这几类：误译与漏译、同一概念前后译法不一、专名写法不统一、翻译腔。改完再跑一次检查。
6. 会话结束后，通途在会话之外用原始的 `en.tex` 和术语表重跑同一套比对，以那次的结果为准。只在当前目录内读写。

## 格式与翻译规则

你负责判断，脚本负责验证。检查命令逐段比对的有这几样：占位符的种类与个数；控制序列（`\命令` 与 `\%` 这类转义字符）的种类与个数；花括号是否配对；`$` 的个数，不得少于原文，且必须成对；裸 `%` 的个数，不得多于原文；行内公式 `$...$` 的内容；`⟦CAP-n⟧` 行的位置与所在段落的行数；章节标题；段落数。下面的规则里有一部分没有比对，同样必须遵守：`~`、`--` 与 `---`、引号的数量与配对，`\cite`、`\ref`、`\label` 的参数与命令的可选参数，`\[...\]` 与 `$$...$$` 里的内容，占位符在段内的顺序与独占一行，不译词在连字符复合词里的出现。

## 一、占位符规则

1. **`⟦BLK-n⟧` 是不透明占位符**：背后是被掩掉的重环境（公式、图表、代码、算法）、题目与作者
   信息，或者行尾的注释符 `%`。原样保留、逐个保留、顺序不变；不得翻译、改写、增删、拆分、合并。占位符连续
   出现或紧贴正文都是正常情况，不要当成排版遗漏去修改。`⟦` 与 `⟧` 是掩码保留符号，除完整
   占位符外**正文里绝不允许出现**（`⟦BLK-3⟧⟧` 这种残缺片段会被专门的检查报告出来）。
2. **`⟦CAP-n⟧` 开头的行**：图表标题与摘要的可译文本。
   - 保留行首的 `⟦CAP-n⟧` token，**翻译其后的文本**，结果**仍是一行**，同一行不放其他占位符。
     检查命令会报告不在行首的 CAP token 和与其他占位符同行的 CAP token：回填时整行都被当成标题取走；
   - 标题写在一行里，不换行；含 CAP 的段落行数必须与原文相同，检查命令会报告行数不同的段落；
   - 行内的 ` \par ` 是摘要的段落分隔标记，原样保留，**不要换成空行**；
   - 这一行**必须翻**：回填时如果该行与原文逐字节相同，会按未改动处理，回填**英文
     原文**，结果是译文里留下一句英文。
3. 占位符独占一行，但**不构成段落边界**（见例一、例四）；不要与相邻文字合并，也不要给它
   加引号、加粗或包进任何命令里。

## 二、不可变元素（逐字保留）

1. 行内公式 `$...$`、行间数学 `\[...\]` 与 `$$...$$`；
2. 引用与交叉引用：`\cite/\citep/\citet{...}`、`\ref/\eqref/\Cref{...}`、`\label{...}`；
3. 一切 `\命令`（含星号变体 `\section*` 与 `\dsviii{}` 这类自定义宏）及其可选参数 `[...]`；
4. 不间断空格 `~`、LaTeX 转义字符 `\%` `\&` `\#` `\_` `\$`、连字符 `--` `---`；
5. 引号：` ``...'' ` 与 ASCII 直引号 `"..."` 同理。内容保持英文时整体原样保留；整句译成
   中文时换成 “...”，但引号的**数量与配对不变**。

公式在句子里充当句子成分，要把它们组织进通顺的中文语序里。
`where $\theta$ denotes` 译成 `其中 $\theta$ 表示`，不能译成 `其中 $\theta$ denotes`。

**行内公式连同两侧的 `$` 一起保留，哪怕里面只有一个数字。**`a dropout rate of $0.1$` 译成
`dropout 率为 $0.1$`，不能译成 `dropout 率为 0.1`。去掉 `$` 会少一对，`$` 的个数少于原文，检查命令会报告。

**公式内部一个字符都不能改**，哪怕那个字符是中文用不上的英文语法成分。
`the $k^{th}$-greatest element` 译成 `第 $k^{th}$ 大的元素`，不能译成 `第 $k$ 大的元素`；
`$i^{th}$ secondary network` 译成 `第 $i^{th}$ 个次级网络`，不能译成 `第 $i$ 个次级网络`。
`^{th}` 是英文序数后缀，中文确实用不上，但它在公式里，删掉后这个公式就与原文
不同，行内公式的比对会报告。读起来别扭也照样保留。

`%` 也在这一层。它在 LaTeX 里是注释符，会把所在行的后半行变成注释；原文的注释文字已经删除，
行尾的注释符 `%` 也换成了占位符，因此**译文里一个裸 `%` 都不许出现**，要写百分号只能写 `\%`。

### 只许保留，不许新增

上面这些元素，原文有的一个不许少，原文没有的一个不许多。检查命令在两个方向上比对的是
控制序列与行内公式：控制序列按 multiset 比对，多一个和少一个同样报告为问题。`~`、`--` 与
`---`、引号以及引用命令的参数没有比对，同样不许增减。最常见的错误是**把原文用纯文本写出的东西改写成
LaTeX**，例如把符号词改写成命令，或者给裸变量名补上 `$`：

| 原文 | 不许 | 应当 |
|---|---|---|
| `the final 10 percent of the steps` | `最后 10\% 的训练步` | `最后百分之十的训练步` |
| `a rotation of 30 degrees` | `旋转 $30^\circ$` | `旋转 30 度` |
| `costs 5 dollars` | `花费 \$5` | `花费 5 美元` |
| `the kth highest component of $v$` | `第 $k$ 个最大分量` | `第 k 个最大分量` |
| `Research \& Engineering` | `研究与工程` | `研究 \& 工程` |

原文作者写单词而不写符号是他的排版选择，翻译不改排版。反过来同样成立：原文写 `\%` 的地方
译文也必须写 `\%`，不许改成「百分之」。

同一句里 `$v$` 是行内公式而 `k` 是裸字母，看着不一致也照旧，这是作者的排版选择，不需要修正。
裸变量名保持裸的：`the kth component` 里的 `k` 译完仍是不带 `$` 的 `k`。

你翻译的是掩码后的正文，它本身不是可编译的完整文档，不要补全文档结构：
`\bibliography`、`\bibliographystyle`、`\end{document}`、新的 `\section` 这类原文没有的结构
命令，一个都不许补。

## 三、命令参数

保留命令本身，翻译其中的文本：`\paragraph{Training Setup}` 译成 `\paragraph{训练设置}`，
`\textbf{Left}:` 译成 `\textbf{左}：`。命令保留，文本翻译，冒号随译文改为全角。
`\emph{...}`、`\footnote{...}`、`\caption{...}`、`\underline{...}` 同理，footnote 里的英文
是正文，照译。命令名、花括号数量、嵌套层级一个都不能变。
列表环境保留在正文中：`\item` 保留，其后文本翻译。
表格如果整个被掩掉，它的表头文字不在正文里，不要补译。

## 四、段落结构

原文几段，译文就几段。**空行是段落边界**：不合并、不拆分、不跳过、不新增，段数与顺序都与原文相同。检查命令会报告段落结构不一致的地方。只含空格或制表符的行按空行处理，译文在这里写空行即可，不要求照抄行里的空白。
长难句可以拆成几句中文。**句可以拆，段不能拆**。
含 `⟦CAP-n⟧` 的段落行数必须与原文相同，标题写在一行里。检查命令会报告行数不同的段落。
其余段落的段内换行尽量照抄原文：原文两句各占一行，译文也各占一行。这一点不需要逐行核对，也不要为此写脚本比对。
文件首尾的空白照抄原文。

## 五、术语

1. **术语一致**：`glossary.fixed.json` 与 `glossary.json` 里有的词必须照它译；你新定的术语先写进 `glossary.json` 再用，全文一致靠这个文件，不靠记忆。
2. **原样保留不译**：模型、系统、基准、数据集、机构名，作者人名（含拼音），以及领域
   惯用英文（token、kernel、KV cache 之类）。拿不准是否惯用时**倾向保留英文**。术语表里
   列为不译的词，在复数形式、其他词形以及复合词内部出现时同样保留原文：`RL` 不译，那么
   `RL training` 里的 `RL` 也保留，写成「RL 训练」，不写成「强化学习训练」。一条术语的原词里含有不译词时，
   它的译法也必须原样保留这个词：`RL` 不译，`mixed RL training` 的译法写成「混合 RL 训练」。
   不译词出现在连字符复合词里时，按词义判断：复合词整体另有通行译法的，按整体翻译，`low-rank`
   译作「低秩」；不译词在复合词里仍指它自己的，保留原文，`RL-based` 译作「基于 RL 的」。
3. 数字、单位、变量名不译。`Figure 3` 这类在正文里直接写出的引用如果不是 `\ref` 产生的，按
   中文习惯译成「图 3」；**如果它是命令产生的，就保持不动**。
4. 数量级后缀 K/M/B/T 原样保留：`70K` 译作 `70K`、`1.5M` 译作 `1.5M`、`14.8T` 译作
   `14.8T`，**不换算**成「7 万」「150 万」「14.8 万亿」，避免读者自己换算。
5. 写成英文单词的数量词换算成中文数量词：`104 billion` 译作「1040 亿」，`3 trillion` 译作「3 万亿」，
   `2 million` 译作「200 万」。这与第 4 条不冲突：写成单词的换算成中文数量词，写成后缀的原样保留。

## 六、文风：去掉翻译腔

直译造成的偏差和意译造成的偏差同样是错误。判断标准是中文读者读起来是否通顺，与英文的贴近程度无关。
最常见的几种翻译腔，出现时逐一改掉：

1. **滥用被动。**英文科技文大量使用被动句，中文很少用。
   「性能被观察到显著下降」改为「可以观察到性能明显下降」。
2. **「的」字长定语。**英文后置从句直译成前置长定语，读者要读完整个定语才看到名词。拆开：
   「我们提出了一种能够在不重新训练的前提下将上下文扩展到四倍的位置编码方案」
   改为「我们提出一种位置编码方案，无需重新训练即可把上下文扩展到原来的四倍」。
3. **主谓被长插入语隔开。**列举、引用串这类长插入语放到后面，避免读者读到谓语时已经忘了主语：
   「开源模型，包括 A、B、C、D，也在取得显著进展」改为「开源模型也在大步前进，包括 A、B、C、D」。
4. **冠词、代词照译。**a、the、it、this 是英文的语法需要，中文常可省略：
   「我们计算一个交叉熵损失」改为「我们计算交叉熵损失」。counterparts、respectively 这类
   词不必逐字译出，语义保留即可。
5. **万能动词。**「对模型进行评估」改为「评估模型」；「实现了性能的提升」改为「提升了性能」。

**标点按中文表达的需要使用**：正文用全角标点；原文的逗号在中文里可以变成破折号、分号或句号，
只要不改动第二节里被计数的符号。中英文之间留一个空格，但 `~` 前不留，`~` 本身不动。

`we` 译「我们」；图表标题一律翻译；论文题目与章节标题保留英文。
不加译注，不擅自修正原文的错误，不增删信息。**不要给译名加括注英文**（原文没有括号，译文就不写「中文译名（English name）」这样的形式）。

## 七、范例

四组对照，每组后面一行说明它示范的规则。

### 例一

```latex
\textbf{Standardized exams} include \underline{AGIEval} \citep{agieval}. 
Note that AGIEval includes both English and Chinese subsets.

⟦BLK-29⟧
Following our previous work~\citep{dsvi,dsvii}, we adopt perplexity-based evaluation for datasets including HellaSwag, PIQA, WinoGrande, RACE-Middle, RACE-High, MMLU, MMLU-Redux, MMLU-Pro, MMMLU, ARC-Easy, ARC-Challenge, C-Eval, CMMLU, C3, and CCPM, and adopt generation-based evaluation for TriviaQA, NaturalQuestions, DROP, MATH, GSM8K, MGSM, HumanEval, MBPP, LiveCodeBench-Base, CRUXEval, BBH, AGIEval, CLUEWSC, CMRC, and CMath. 
In addition, we perform language-modeling-based evaluation for Pile-test and use Bits-Per-Byte~(BPB) as the metric to guarantee fair comparison among models using different tokenizers.
```

```latex
\textbf{标准化考试}包括 \underline{AGIEval} \citep{agieval}。
注意 AGIEval 同时含英文与中文子集。

⟦BLK-29⟧
沿用先前工作~\citep{dsvi,dsvii}的做法，我们对一批数据集采用基于困惑度的评测，包括 HellaSwag、PIQA、WinoGrande、RACE-Middle、RACE-High、MMLU、MMLU-Redux、MMLU-Pro、MMMLU、ARC-Easy、ARC-Challenge、C-Eval、CMMLU、C3 和 CCPM；对 TriviaQA、NaturalQuestions、DROP、MATH、GSM8K、MGSM、HumanEval、MBPP、LiveCodeBench-Base、CRUXEval、BBH、AGIEval、CLUEWSC、CMRC 和 CMath 则采用基于生成的评测。
此外，我们对 Pile-test 采用基于语言建模的评测，并以 Bits-Per-Byte~(BPB) 作为指标，保证使用不同分词器的模型之间比较公平。
```

示范的规则：空行是段界，段落结构照抄，`⟦BLK-29⟧` 独占一行但与其后文字同段；段内单换行位置
尽量照抄；`\textbf{}` `\underline{}` 命令保留、参数照译；长列表用顿号、两组列举用分号组织；
including 引出的列举放到后面，不隔开主谓；BPB 这类拿不准的术语保留英文，`~` 和半角括号
一起不动。

### 例二

两处摘录，各自成段。

```latex
Each training batch consists of 128 examples, each of which has an input of 512 tokens and an output of 114 tokens, the output containing multiple spans of tokens which were deleted from the input\footnote{Each training step took approximately 0.15 seconds on a 32-core TPUv2 cluster.}.  Similarly to \citep{raffel2019exploring}, we use the Adafactor optimizer \citep{shazeer2018adafactor} and an inverse-square-root learning-rate schedule.  We also decay the learning rate linearly for the final 10 percent of the training steps.

On the factual benchmark Chinese SimpleQA, \dsviii{} surpasses Qwen2.5-72B by 16.4 points, despite Qwen2.5 being trained on a larger corpus comprising 18T tokens, which are 20\% more than the 14.8T tokens that \dsviii{} is pre-trained on.
```

```latex
每个训练批次由 128 个样本组成，每个样本的输入为 512 个 token、输出为 114 个 token，输出中包含若干段从输入里删去的 token\footnote{在 32 核 TPUv2 集群上，每个训练步大约耗时 0.15 秒。}。与 \citep{raffel2019exploring} 类似，我们使用 Adafactor 优化器 \citep{shazeer2018adafactor} 与逆平方根学习率调度。我们还在最后百分之十的训练步中让学习率线性衰减。

在事实性基准 Chinese SimpleQA 上，\dsviii{} 领先 Qwen2.5-72B 达 16.4 分——尽管 Qwen2.5 的训练语料更大，有 18T token，比 \dsviii{} 预训练所用的 14.8T token 多 20\%。
```

示范的规则：`\footnote{}` 是命令参数，里面的英文照译；符号与单词不互相改写，写单词的
10 percent 译「百分之十」，写符号的 `20\%` 保留 `\%`；`\dsviii{}` 一类自定义宏原样保留；
标点可以按中文表达重新组织（原文的逗号变成破折号）。

### 例三

```latex
In recent years, Large Language Models~(LLMs) have been undergoing rapid iteration and evolution~\citep{gpt4o,claude35sonnet,gemini1_5}, progressively diminishing the gap towards Artificial General Intelligence~(AGI).
Beyond closed-source models, open-source models, including DeepSeek series~\citep{dsvi,dsvii,dscodervi,dscodervii}, LLaMA series~\citep{llama,llama2,llama3,llama3_1_405b}, Qwen series~\citep{qwen,qwen1_5,qwen2_5}, and Mistral series~\citep{mistral,mixtral8x22b}, are also making significant strides, endeavoring to close the gap with their closed-source counterparts.
```

```latex
近年来，大语言模型~(LLMs) 快速迭代演进~\citep{gpt4o,claude35sonnet,gemini1_5}，与通用人工智能~(AGI) 的差距逐步缩小。
除闭源模型外，开源模型也在大步前进，包括 DeepSeek 系列~\citep{dsvi,dsvii,dscodervi,dscodervii}、LLaMA 系列~\citep{llama,llama2,llama3,llama3_1_405b}、Qwen 系列~\citep{qwen,qwen1_5,qwen2_5} 和 Mistral 系列~\citep{mistral,mixtral8x22b}，力图缩小与闭源模型的差距。
```

示范的规则：`~` 一个不动；`\citep{a,b,c}` 是不可变元素，参数整体不动；英文缩写与括号原样；两句各占一行，
译文尽量也各占一行；「与闭源同类模型」简化为「与闭源模型」，counterparts 不逐字译出。

### 例四

```latex
⟦BLK-21⟧
⟦CAP-7⟧ Comparison of pipeline bubbles and memory usage across different pipeline parallel methods. $F$ denotes the execution time of a forward chunk, $B$ denotes the execution time of a full backward chunk, $W$ denotes the execution time of a "backward for weights" chunk, and $F\&B$ denotes the execution time of two mutually overlapped forward and backward chunks.
```

```latex
⟦BLK-21⟧
⟦CAP-7⟧ 不同流水线并行方法在流水线气泡与内存占用上的对比。$F$ 是一个前向块的执行时间，$B$ 是一个完整反向块的执行时间，$W$ 是一个“对权重的反向传播”块的执行时间，$F\&B$ 是两个相互重叠的前向块与反向块的执行时间。
```

示范的规则：`⟦BLK-n⟧` 与 `⟦CAP-n⟧` 相邻是正常情况（图被掩掉，标题单独抽出来翻译）；CAP 行保留行首
token、译其后全部文本、结果仍是一行；`$F$ denotes` 这类句型融进中文语序。


## 结束时

结束前在当前目录写 `notes.json`，逐条记下这次遇到的问题：

```json
{"notes": [{"where": "...", "problem": "...", "resolution": "...", "suggestion": "..."}]}
```

- `where`：问题的位置，写文件名与行号，或者一段原文。
- `problem`：遇到的问题，例如规则没有覆盖的情况、检查命令的误报、拿不准的译法、原文的错误。
- `resolution`：这次怎样处理的。
- `suggestion`：预期的解决方法，写明应该修改 skill、规则或校验脚本的哪一处；没有时写空串。

没有遇到问题时写 `{"notes": []}`。

最后用一段话说明：全文是否译完、检查是否通过、`glossary.json` 里新增了哪些术语、有哪些拿不准的地方。
