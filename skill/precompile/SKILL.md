---
name: precompile
description: precompile 阶段修复会话的任务说明：在一次性编译树内把英文原文修到 xelatex 编译通过，不改动文字内容。
version: 1
---

# 原文编译修复会话

你在一棵一次性编译树里（当前目录），内容是一篇 arXiv 论文的源码拷贝，主文件是 `flat.tex`（多文件源码已展开成单文件、参考文献已内联）。它在 xelatex 下编译失败，驱动器把首次编译的错误行附在本说明之后。

你的任务：把这棵树修到 `latexmk -xelatex -interaction=nonstopmode flat.tex` 编译通过。

**你负责判断，脚本负责验证。** 会话结束后驱动器会清理编译产物并自己重新编译一次，那次编译才是唯一裁决。改完自己编一遍确认即可，不要写长总结论证自己是对的。

## 边界

1. 只在当前目录（编译树）内读写，不碰目录外的任何文件。
2. **不改动文字内容**：不删正文、不删章节、不删图表、不改写任何句子。你修的是「能不能编译」，不是「写得好不好」。「编过了但内容少了」比编不过更糟。
3. 优先只改 `flat.tex`。确有必要才改树内其他文件（如 `.sty`），并在结束时单独说明。
4. 最小改动：一次改一处，编一次，看日志变化；注释掉一行优于删除一行。
5. 引擎固定 xelatex，不许改用 pdflatex，也不许改动编译命令来绕过问题。

## 诊断顺序

1. 读 `flat.log` 里第一个 `!` 开头的错误——后面的错误多半是它的连锁反应；
2. 看紧随其后的 `l.<行号>`，定位到 `flat.tex` 的对应位置；
3. 对照下面的已知模式；都不匹配就按日志现场判断。

## 已知失败模式

以下模式在真实论文上出现过，处置方式经过验证：

1. **`\pdfoutput=1`（pdftex 专有原语）**：xelatex 未定义该命令，报 `Undefined control sequence`，常连锁出 `Missing \begin{document}`。处置：把该行注释掉（行首加 `%`）。
2. **CJKutf8 机制**：`\usepackage{CJKutf8}` 加 `\begin{CJK*}{UTF8}{…}` 包裹正文，是 pdflatex 的中文机制，xelatex 下遇非 ASCII 字符报 `Package CJK Error: Invalid character code`。处置：注释掉 `\usepackage{CJKutf8}`，成对注释 `\begin{CJK*}{…}{…}` 与 `\end{CJK*}`（留意 `\CJKfamily` 等同族命令与文档末尾的变体写法，一并注释），正文原样保留。此后正文里的中文字符在日志报 `Missing character` 属预期——那是缺字形不是错误，不是你要修的问题，后续阶段会注入中文字体支持。
3. **图引用带显式扩展名但文件缺失**：如 `\includegraphics{fig.eps}` 而树里只有 `fig.pdf`，报 `File 'fig.eps' not found`。处置：确认同主干、其他扩展名的图文件确实存在，然后把引用改成存在的扩展名（或去掉扩展名交给 LaTeX 按默认顺序找）。

## 结束时

用一段话说清楚：改了哪些文件的哪几处、为什么、你自己最后一次编译的结论。改了 `flat.tex` 之外的文件要单独点名。不要粘贴命令输出，transcript 已由驱动器落盘。
