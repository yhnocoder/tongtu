---
name: precompile_fix
description: precompile 阶段修复会话的任务说明：在一次性编译目录内修复缺失源码引用或原文 xelatex 编译失败，不改动文字内容。
version: 4
---

# 原文编译修复会话

你在一个一次性编译目录里（当前目录），内容是一篇 arXiv 论文的源码拷贝。先判断现场模式：

- **存在 `precompile-expand.log`：源码展开修复。** 先读该文件，其中记录主文件相对路径、严格展开命令、完整 stderr、源码中 `\input` / `\include` 的文件和行号。此时尚未生成可用的单文件，主文件以诊断为准，即使目录内有 `flat.tex` 也不要自行把它当展开结果。修复原始主文件及必要的附属文件，让日志中的 `latexpand --keep-comments --fatal` 命令成功。必须保留 `--fatal`；失败输出不完整，不可用于编译。发现后续缺失引用时在本次会话继续处理。
- **不存在 `precompile-expand.log`：单文件编译修复。** 主文件是 `flat.tex`，多文件源码已展开、参考文献已内联，导言区带有 xeCJK 中文排版设置，位于 `% ---- injected by tongtu (precompile) ----` 与 `% ---- end tongtu (precompile) ----` 之间。编译错误见 `flat.log`，任务是让 `latexmk -xelatex -interaction=nonstopmode flat.tex` 通过。

**你负责判断，脚本负责验证。** 整个预编译阶段最多一次修复会话，展开修复与编译修复共用此次预算。源码模式中修复缺失引用后，还要用 XeLaTeX 编译验证修复后的原文，必要的引擎兼容改动也留在源码里；临时展开结果另取文件名，不覆盖原始主文件。主文件同名 `.bbl` 应保留供驱动器内联。驱动器结束后会重新严格展开源码、内联 `.bbl`、注入中文排版设置，清理编译产物再校验编译；不会再启动第二次修复会话。单文件模式结束后同样清理再校验编译。模型超时或报错都不能代替脚本判定。

## 边界

1. 只在当前目录（编译目录）内读写，不碰目录外的任何文件。
2. **不改动文字内容**：不删正文、不删章节、不删图表、不改写任何句子。你修的是「能不能编译」，不是「写得好不好」。「编过了但内容少了」比编不过更糟。
3. **不删除注入的 xeCJK 配置块**：它是后续阶段排中文的前提。宏包冲突时优先调整块的位置或加载顺序、修改冲突的另一方；确有必要时可以调整块内的个别命令，但中文字体设置必须保持有效。
4. 单文件模式优先只改 `flat.tex`；源码模式修改诊断指出的主文件或附属源码。确有必要才改目录内其他文件（如 `.sty`），并在结束时单独说明。
5. 最小改动：一次改一处，编一次，看日志变化；注释掉一行优于删除一行。
6. 引擎固定 xelatex，不许改用 pdflatex，也不许改动编译命令来绕过问题。
7. **Bash 跑不起来就不修**：第一条 Bash 命令若报沙箱错误、命令找不到一类与论文无关的失败，不要凭猜测改文件。立刻结束会话，说明「无法编译验证」，写出你判断的原因与建议改法，不落任何编辑。

## 缺失源码引用的判断

1. 先查看完整源码树，文件存在但路径写错时修正路径，确保正确内容被内联。
2. 只有结合正文使用情况、宏定义及现有文件等证据，确认是无用的遗留引用时，才可注释对应命令，并在结尾说明依据。不能仅凭“编译通过”认定内容完整。
3. 不得用空文件、空宏或占位内容伪造缺失文件，不删正文、章节或图表来凑出 PDF；必需内容无法可靠恢复时保留失败并说明缺失内容。
4. 一次失败可能只报告第一个缺失文件。修复后继续严格展开检查所有引用，不根据文件名把其他缺失文件自动视为无用。

## 单文件编译诊断顺序

1. 读 `flat.log` 里第一个 `!` 开头的错误——后面的错误多半是它的连锁反应；
2. 看紧随其后的 `l.<行号>`，定位到 `flat.tex` 的对应位置；
3. 对照下面的已知模式；都不匹配就按日志现场判断。

## 已知失败模式

以下模式在真实论文上出现过，处置方式经过验证：

1. **`\pdfoutput=1`（pdftex 专有原语）**：xelatex 未定义该命令，报 `Undefined control sequence`，常连锁出 `Missing \begin{document}`。处置：把该行注释掉（行首加 `%`）。
2. **残留的 CJKutf8 机制**：驱动器已在注入时移除 `CJKutf8` 系宏包并剥掉 `\begin{CJK*}` 包裹，但个别变体写法（自定义包装命令、`\AtBeginDocument` 里的加载）可能漏网，xelatex 下报 `Package CJK Error` 或 `Undefined control sequence`。处置：注释掉残留的 CJK 机制命令，正文原样保留；中文排版由注入的 xeCJK 配置负责。
3. **图引用带显式扩展名但文件缺失**：如 `\includegraphics{fig.eps}` 而树里只有 `fig.pdf`，报 `File 'fig.eps' not found`。处置：确认同主干、其他扩展名的图文件确实存在，然后把引用改成存在的扩展名（或去掉扩展名交给 LaTeX 按默认顺序找）。
4. **宏包与 xeCJK / fontspec 冲突**：如重复定义字体命令、`inputenc`/`fontenc` 与 XeTeX 引擎不兼容。处置：注释掉 `\usepackage[utf8]{inputenc}` 一类在 xelatex 下多余的行；其余冲突按日志逐个处理。
5. **fontspec 按字体名找不到 texmf 里的字体**：XeTeX 按字体名查找时只问系统字体库，TeX Live 自带的字体文件不在其中，报 `Package fontspec Error: The font "..." cannot be found`，随后连锁 `Font TU/... not loadable`。按文件名（带 `.otf`/`.ttf` 扩展名）加载则经 kpsewhich 查找，`kpsewhich <文件名>` 能找到就能用。字体名写在宏包内部时，要在宏包加载前用文件名定义好同名字体命令，并让宏包内部那次定义落空，例如 `fontawesome`（v4）内部的 `\newfontfamily{\FA}{FontAwesome}`：

   ```latex
   \newfontfamily\FA{FontAwesome.otf}
   \let\tongtuorig\newfontfamily
   \def\newfontfamily#1#2{\global\let\newfontfamily\tongtuorig}
   \usepackage{fontawesome}
   ```

   不要用 `\let` 保存后 `\RenewDocumentCommand` 重定义 fontspec 的命令：它们的主体存在内部宏里，会被一并替换，`\let` 的拷贝仍指向它，结果是无限递归（`TeX capacity exceeded [parameter stack size]`）。
6. **microtype 的 protrusion 遇到 TFM 字体**：XeTeX 下 microtype 对部分编码（如 `TS1`）用字形名描述突出量，需要 `\XeTeXglyph`，而传统 Type1 字体（Times 的 `ptm` 等，会议模板常把 `\rmdefault` 设成它）是 TFM 字体，报 `Cannot use XeTeXglyph with <字体名>; not a native platform font`，紧接着 `Missing number, treated as zero`。处置：`\usepackage[protrusion=false]{microtype}`。

## 结束时

用一段话说清楚：改了哪些文件的哪几处、为什么、你自己最后一次编译的结论。改了 `flat.tex` 之外的文件要单独点名。不要粘贴命令输出，transcript 已由驱动器落盘。
