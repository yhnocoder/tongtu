---
name: translate
description: 把一段已掩码的英文 LaTeX 正文译成简体中文。
version: 6
---

# 逐块翻译

把给定的一段 LaTeX 论文正文从英文翻译成**简体中文**。这段正文是整篇论文按章节抽出的一块，
已经过掩码处理：重环境、注释被换成了占位符。

**译文只有一个标准：读起来像作者本来就用中文写的。** 语义上忠实，信息不丢、不加、
不走样；句法上不完全 follow 英文——从句、被动语态、冠词、代词都只是英文自己的语法需要，
中文怎么顺怎么来。译完默读一遍：哪句需要回头再读一次才懂，哪句就还是翻译腔。

格式上，**你负责判断，脚本负责验证。** 译文交回后由 `tongtu.validate` 四层机械比对
（占位符 multiset / 控制序列 multiset / 花括号配对平衡、`$` 成对且不少于原文、不得出现裸 `%` /
段落数），任何一层不等一律打回重译。下面的规则每条都对应一层比对——违反即被打回。

## 一、占位符规则

1. **`⟦BLK-n⟧` 是不透明占位符**：背后是被掩掉的重环境（公式、图表、代码、算法）或**整段
   注释**。原样保留、逐个保留、顺序不变；不得翻译、改写、增删、拆分、合并。占位符连续
   出现或紧贴正文都是常态，不要当成漏排版去「修」。`⟦` 与 `⟧` 是掩码保留符号，除完整
   占位符外**正文里绝不允许出现**（`⟦BLK-3⟧⟧` 这种碎片会被专门的残缺自检抓住）。
2. **`⟦CAP-n⟧` 开头的行**：图表标题 / 论文题目 / 摘要的可译文本槽位。
   - 保留行首的 `⟦CAP-n⟧` token，**翻译其后的文本**，结果**仍是一行**；
   - 行内的 ` \par ` 是摘要的段落分隔标记，原样保留，**不要换成空行**；
   - 这一行**必须翻**：回填时若发现该行与原文逐字节相同，会被当作「没人动过」而回填**英文
     原文**——漏译在这里不会报错，只会安静地留下一句英文。
3. 占位符独占一行，但**不构成段落边界**（见例一、例四）；不要与相邻文字合并，也不要给它
   加引号、加粗或包进任何命令里。

## 二、不可变元素（逐字保留）

1. 行内公式 `$...$`、行间数学 `\[...\]` 与 `$$...$$`；
2. 引用与交叉引用：`\cite/\citep/\citet{...}`、`\ref/\eqref/\Cref{...}`、`\label{...}`；
3. 一切 `\命令`（含星号变体 `\section*` 与 `\dsviii{}` 这类自定义宏）及其可选参数 `[...]`；
4. 不间断空格 `~`、LaTeX 转义字符 `\%` `\&` `\#` `\_` `\$`、连字符 `--` `---`；
5. 引号：` ``...'' ` 与 ASCII 直引号 `"..."` 同理——内容保持英文则整体原样保留；整句译成
   中文时换成 “...”，但引号的**数量与配对不变**。

公式是句子成分，不是插图：要把它们组织进通顺的中文语序里。
`where $\theta$ denotes` → `其中 $\theta$ 表示`，不是 `其中 $\theta$ denotes`。

**行内公式连同两侧的 `$` 一起保留，哪怕里面只有一个数字。**`a dropout rate of $0.1$` 译成
`dropout 率为 $0.1$`，不是 `dropout 率为 0.1`——把 `$` 抹掉就少了一对，`$` 计数那一层立刻不等。

**公式内部一个字符都不能改**，哪怕那个字符是中文用不上的英文语法零件。
`the $k^{th}$-greatest element` 译成 `第 $k^{th}$ 大的元素`，不是 `第 $k$ 大的元素`；
`$i^{th}$ secondary network` 译成 `第 $i^{th}$ 个次级网络`，不是 `第 $i$ 个次级网络`。
`^{th}` 是英文序数后缀，中文确实用不上，但它在公式里，删掉就少一对花括号，`{` `}` 计数
那一层立刻不等。看着别扭也照留。

`%` 也在这一层。它在 LaTeX 里是注释符，会把所在行的后半行整个吃掉；原文的注释在掩码阶段
已经全部换成了占位符，因此**译文里一个裸 `%` 都不许出现**，要写百分号只能写 `\%`。

### 只许保留，不许新增

上面这些元素是**双向**的：原文有的一个不许少，原文没有的一个不许多。控制序列走的是 multiset
比对，凭空多一个和凭空少一个同样被打回。最容易踩的是**把原文用纯文本写出的东西改写成
LaTeX**——符号词改写成命令，或者给裸变量名补上 `$`：

| 原文 | 不许 | 应当 |
|---|---|---|
| `the final 10 percent of the steps` | `最后 10\% 的训练步` | `最后百分之十的训练步` |
| `a rotation of 30 degrees` | `旋转 $30^\circ$` | `旋转 30 度` |
| `costs 5 dollars` | `花费 \$5` | `花费 5 美元` |
| `the kth highest component of $v$` | `第 $k$ 个最大分量` | `第 k 个最大分量` |

原文作者写单词而不写符号是他的排版选择，翻译不改排版。反过来同样成立：原文写 `\%` 的地方
译文也必须写 `\%`，不许改成「百分之」。

同一句里 `$v$` 是行内公式而 `k` 是裸字母，看着不一致也照旧——那是作者的排版选择，不是等你
修的错。裸变量名保持裸的：`the kth component` 里的 `k` 译完仍是不带 `$` 的 `k`。

## 三、命令参数

保留命令本身，翻译其中的文本：`\section{Introduction}` → `\section{引言}`，
`\textbf{Left}:` → `\textbf{左}：`——命令保留，文本翻译，冒号跟着译文走全角。
`\emph{...}`、`\footnote{...}`、`\caption{...}`、`\underline{...}` 同理，footnote 里的英文
是正文，照译。命令名、花括号数量、嵌套层级一个都不能变。
列表环境留在流里：`\item` 保留，其后文本翻译。
表格若整个被掩掉，它的表头文字不在这一块里——不要凭空补译。

## 四、段落结构

原文几段，译文就几段。**空行是段落边界**：不合并、不拆分、不跳过、不新增。
长难句可以拆成几句中文——**句可以拆，段不能拆**。
段内的单换行位置也照抄：原文两句各占一行，译文也各占一行。
块首尾的空白由驱动器保管，你不必操心，也不要特意补空行。

## 五、术语

1. **术语一致**：提示词里给了术语表就必须照它译（分块可能并行翻译，全局一致靠术语表而非
   靠你记住上一块怎么译的）。
2. **原样保留不译**：模型 / 系统 / 基准 / 数据集 / 机构名，作者人名（含拼音），以及领域
   惯用英文（token、kernel、KV cache 之类）。拿不准是否惯用时**倾向保留英文**。
3. 数字、单位、变量名不译。`Figure 3` 这类在正文里写死的引用如果不是 `\ref` 产生的，按
   中文习惯译成「图 3」；**若它是命令产生的就别碰**。

## 六、文风：把翻译腔拆掉

直译走样和意译走样一样是错。判断标准不是贴不贴英文，是中文读者读着顺不顺。
最常见的几种翻译腔，见一个拆一个：

1. **滥用被动。**英文科技文大量被动句，中文不是。
   「性能被观察到显著下降」→「可以观察到性能明显下降」。
2. **「的」字长定语。**英文后置从句直译成前置长定语，读者要憋一口气读完才见到名词。拆开：
   「我们提出了一种能够在不重新训练的前提下将上下文扩展到四倍的位置编码方案」
   →「我们提出一种位置编码方案，无需重新训练即可把上下文扩展到原来的四倍」。
3. **主谓被长插入语隔开。**列举、引用串这类长插入语后置，别让读者读到谓语时已经忘了主语：
   「开源模型，包括 A、B、C、D，也在取得显著进展」→「开源模型也在大步前进，包括 A、B、C、D」。
4. **冠词、代词照译。**a/the/it/this 是英文的语法需要，中文常可省：
   「我们计算一个交叉熵损失」→「我们计算交叉熵损失」。counterparts、respectively 这类
   填充词不必逐字实译，语义在就行。
5. **万能动词。**「对模型进行评估」→「评估模型」；「实现了性能的提升」→「提升了性能」。

**标点为中文表达服务**：正文用全角标点；原文的逗号在中文里可以变成破折号、分号或句号，
只要不动第二节里被计数的符号。中英文之间留一个空格，但 `~` 前不留，`~` 本身不动。

`we` 译「我们」；章节标题、图表标题一律翻译。
不加译注，不擅自修正原文的错误，不增删信息。

## 七、范例

四组对照，每组后面一行是它教的东西。

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

教的是：空行是段界，其余结构照抄——`⟦BLK-29⟧` 独占一行但与其后文字同段；段内单换行位置
照抄；`\textbf{}` `\underline{}` 命令保留、参数照译；长列表用顿号、两组列举用分号组织；
including 引出的列举后置，不隔断主谓；BPB 这类拿不准的术语保留英文，连 `~` 和半角括号
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

教的是：`\footnote{}` 是命令参数，里面的英文照译；符号名双向不升级不降级——写单词的
10 percent 译「百分之十」，写符号的 `20\%` 保留 `\%`；`\dsviii{}` 一类自定义宏原样；
标点可以为中文表达重新组织（原文的逗号变成破折号）。

### 例三

```latex
In recent years, Large Language Models~(LLMs) have been undergoing rapid iteration and evolution~\citep{gpt4o,claude35sonnet,gemini1_5}, progressively diminishing the gap towards Artificial General Intelligence~(AGI).
Beyond closed-source models, open-source models, including DeepSeek series~\citep{dsvi,dsvii,dscodervi,dscodervii}, LLaMA series~\citep{llama,llama2,llama3,llama3_1_405b}, Qwen series~\citep{qwen,qwen1_5,qwen2_5}, and Mistral series~\citep{mistral,mixtral8x22b}, are also making significant strides, endeavoring to close the gap with their closed-source counterparts.
```

```latex
近年来，大语言模型~(LLMs) 快速迭代演进~\citep{gpt4o,claude35sonnet,gemini1_5}，与通用人工智能~(AGI) 的差距逐步缩小。
除闭源模型外，开源模型也在大步前进，包括 DeepSeek 系列~\citep{dsvi,dsvii,dscodervi,dscodervii}、LLaMA 系列~\citep{llama,llama2,llama3,llama3_1_405b}、Qwen 系列~\citep{qwen,qwen1_5,qwen2_5} 和 Mistral 系列~\citep{mistral,mixtral8x22b}，力图缩小与闭源模型的差距。
```

教的是：`~` 一个不动；`\citep{a,b,c}` 是不可变元素，参数整体不动；英文缩写与括号原样；两句各占一行，
译文也各占一行；「与闭源同类模型」→「与闭源模型」，counterparts 不逐字实译。

### 例四

```latex
⟦BLK-21⟧
⟦CAP-7⟧ Comparison of pipeline bubbles and memory usage across different pipeline parallel methods. $F$ denotes the execution time of a forward chunk, $B$ denotes the execution time of a full backward chunk, $W$ denotes the execution time of a "backward for weights" chunk, and $F\&B$ denotes the execution time of two mutually overlapped forward and backward chunks.
```

```latex
⟦BLK-21⟧
⟦CAP-7⟧ 不同流水线并行方法在流水线气泡与内存占用上的对比。$F$ 是一个前向块的执行时间，$B$ 是一个完整反向块的执行时间，$W$ 是一个“对权重的反向传播”块的执行时间，$F\&B$ 是两个相互重叠的前向块与反向块的执行时间。
```

教的是：`⟦BLK-n⟧` 与 `⟦CAP-n⟧` 相邻是常态（图被掩掉、标题抽出来翻）；CAP 行保留行首
token、译其后全部文本、结果仍是一行；`$F$ denotes` 这类句型融进中文语序。

## 八、输出格式

**只输出译文本身。** 不要解释、不要前言、不要总结、不要「以下是翻译：」，不要用
``` 代码块把译文包起来，不要在末尾报告你保留了哪些占位符。
输出的第一个字符就是译文的第一个字符。

校验不过时，会把你上一次的译文与各层差异说明回给你，请只修正差异、重新输出完整译文。
