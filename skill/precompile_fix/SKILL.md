---
name: precompile_fix
description: precompile 阶段修复会话的任务说明：在一次性编译目录内把带中文排版设置的原文修到 xelatex 编译通过，不改动文字内容。
version: 3
---

# 原文编译修复会话

你在一个一次性编译目录里（当前目录），内容是一篇 arXiv 论文的源码拷贝，主文件是 `flat.tex`（多文件源码已展开成单文件、参考文献已内联，导言区带有 tongtu 注入的 xeCJK 中文排版设置，标记注释 `% ---- injected by tongtu (precompile) ----` 与 `% ---- end tongtu (precompile) ----` 之间）。它在 xelatex 下编译失败，日志在目录内的 `flat.log`。

你的任务：把这棵树修到 `latexmk -xelatex -interaction=nonstopmode flat.tex` 编译通过。

**你负责判断，脚本负责验证。** 会话结束后驱动器会清理编译产物并自己重新编译一次，那次编译才是唯一裁决。改完自己编一遍确认即可，不要写长总结论证自己是对的。

## 边界

1. 只在当前目录（编译目录）内读写，不碰目录外的任何文件。
2. **不改动文字内容**：不删正文、不删章节、不删图表、不改写任何句子。你修的是「能不能编译」，不是「写得好不好」。「编过了但内容少了」比编不过更糟。
3. **不删除注入的 xeCJK 配置块**：它是后续阶段排中文的前提。宏包冲突时优先调整块的位置或加载顺序、修改冲突的另一方；确有必要时可以调整块内的个别命令，但中文字体设置必须保持有效。
4. 优先只改 `flat.tex`。确有必要才改目录内其他文件（如 `.sty`），并在结束时单独说明。
5. 最小改动：一次改一处，编一次，看日志变化；注释掉一行优于删除一行。
6. 引擎固定 xelatex，不许改用 pdflatex，也不许改动编译命令来绕过问题。
7. **Bash 跑不起来就不修**：第一条 Bash 命令若报沙箱错误、命令找不到一类与论文无关的失败，不要凭猜测改文件。立刻结束会话，说明「无法编译验证」，写出你判断的原因与建议改法，不落任何编辑。

## 诊断顺序

1. 读 `flat.log` 里第一个 `!` 开头的错误——后面的错误多半是它的连锁反应；
2. 看紧随其后的 `l.<行号>`，定位到 `flat.tex` 的对应位置；
3. 对照下面的已知模式；都不匹配就按日志现场判断。

## 已知失败模式

以下模式在真实论文上出现过，处置方式经过验证：

1. **`\pdfoutput=1`（pdftex 专有原语）**：xelatex 未定义该命令，报 `Undefined control sequence`，常连锁出 `Missing \begin{document}`。处置：把该行注释掉（行首加 `%`）。
2. **残留的 CJKutf8 机制**：驱动器已在注入时移除 `CJKutf8` 系宏包并剥掉 `\begin{CJK*}` 包裹，但个别变体写法（自定义包装命令、`\AtBeginDocument` 里的加载）可能漏网，xelatex 下报 `Package CJK Error` 或 `Undefined control sequence`。处置：注释掉残留的 CJK 机制命令，正文原样保留；中文排版由注入的 xeCJK 配置负责。
3. **图引用带显式扩展名但文件缺失**：如 `\includegraphics{fig.eps}` 而树里只有 `fig.pdf`，报 `File 'fig.eps' not found`。处置：确认同主干、其他扩展名的图文件确实存在，然后把引用改成存在的扩展名（或去掉扩展名交给 LaTeX 按默认顺序找）。
4. **宏包与 xeCJK / fontspec 冲突**：如重复定义字体命令、`inputenc`/`fontenc` 与 XeTeX 引擎不兼容。处置：注释掉 `\usepackage[utf8]{inputenc}` 一类在 xelatex 下多余的行；其余冲突按日志逐个处理。
5. **`fontawesome`（v4）在 xelatex 下按字体名找不到字体**：宏包内部写死 `\newfontfamily{\FA}{FontAwesome}`，XeTeX 按字体名走系统字体库，而 `FontAwesome.otf` 只在 TeX Live 的 texmf 里，报 `Package fontspec Error: The font "FontAwesome" cannot be found`，后续连锁 `Font TU/FontAwesome(0)/m/n/10 ... not loadable`。处置：按文件名加载，fontspec 会经 kpsewhich 找到文件。在 `\usepackage{fontawesome}` 之前先定义 `\FA`，并让宏包内部那一次 `\newfontfamily` 调用落空：

   ```latex
   \newfontfamily\FA{FontAwesome.otf}
   \let\tongtuorig\newfontfamily
   \def\newfontfamily#1#2{\global\let\newfontfamily\tongtuorig}
   \usepackage{fontawesome}
   ```

   不要用 `\let` 保存原命令再 `\RenewDocumentCommand` 重定义 `\newfontfamily`：ltcmd 定义的命令主体存在内部宏里，`\RenewDocumentCommand` 会连内部宏一起替换，`\let` 拷贝的外壳仍指向它，结果是无限递归，报 `TeX capacity exceeded, sorry [parameter stack size=...]`。
6. **`microtype` 的 protrusion 与 Times 的 TS1 编码在 xelatex 下冲突**：样式文件把 `\rmdefault` 设成 `ptm`（NeurIPS 等会议模板的默认），正文第一个列表圆点或 `\textbullet` 一类符号触发加载 `TS1/ptm`，microtype 对该编码的配置用字形名描述突出量，需要 `\XeTeXglyph`，而 `ptmr8c` 是 TFM 字体，报 `Cannot use XeTeXglyph with ptmr8c; not a native platform font`，紧接着 `Missing number, treated as zero`。处置：把 `\usepackage{microtype}` 改成 `\usepackage[protrusion=false]{microtype}`。

## 结束时

用一段话说清楚：改了哪些文件的哪几处、为什么、你自己最后一次编译的结论。改了 `flat.tex` 之外的文件要单独点名。不要粘贴命令输出，transcript 已由驱动器落盘。
