---
name: reconstruct_table
description: reconstruct 阶段表格识别会话的任务说明：对照截图与文字层，把表格截图写成 LaTeX tabular，并用 check.py 自查到通过。
version: 1
---

# 把表格截图写成 LaTeX

当前目录里是从一篇论文的 PDF 截出的表格：

- `img/<编号>.png`：每张表一张图。
- `hints.json`：每个编号的截图宽度（`width_bp`）与截图的文字层。文字层按基线 y、再按 x 排序，每段写成 `y=.. x=.. 字体名#颜色:文字`。用它确定确切的字符、粗体（字体名含 Bold、Medi、BX、MIB，BlackboardBold 除外）、斜体、颜色，以及哪些文字在同一行（y 相同）。
- `crops/<编号>.pdf`：截图原件，check.py 用它比对，不要改动。

你只写 `rec.json` 和 `notes.json`。`rec.json` 的格式是 `{"编号": "LaTeX"}`，覆盖 `hints.json` 里的每个编号。通过校验的表格按截图原宽度缩放后替换论文里的截图，没通过的保留截图。

## 要求

用 Read 工具逐张看图，每个编号写一段 `\begin{tabular}{...} ... \end{tabular}`，不带 table 或 figure 环境，不带图注：

- 图中每一行可见的文字都是输出里的一行，字符与顺序相同。单元格里的文字在图中折成两行时写 `\makecell{第一行\\第二行}`，折行处与图中相同。跨行跨列的单元格用 `\multicolumn`、`\multirow`。
- 粗体、斜体、下划线、颜色与图一致：文字颜色用 `\textcolor[HTML]{RRGGBB}{..}`，单元格底色用 `\cellcolor[HTML]{..}`。
- 表线与图中可见的线一致：booktabs（`\toprule` `\midrule` `\bottomrule` `\cmidrule`），或 `\hline` 与 `|`。
- 单元格里的数学写成 `$...$`。
- 字符必须与文字层完全一致：✗ 写 `\ding{55}`，✓ 写 `\ding{51}`（pifont），不用 `\times` 或 `$\checkmark$`；间隔点、连字符与其他符号用与文字层相同的 Unicode 字符。
- 旋转的文字用 `\rotatebox{90}{..}`。
- 可用的宏包：amsmath、amssymb、bm、booktabs、multirow、makecell、array、graphicx（只用 `\rotatebox`）、pifont、xcolor（table 选项）。可以用 `\textsc`、`\underline`、`\textit`、`\textbf`。只有图中某个单元格的字明显比表格其余部分小时，才在这个单元格里用 `\small`、`\footnotesize`、`\scriptsize`、`\tiny`。
- 禁止使用：`\resizebox` `\scalebox` `\hspace` `\vspace` `\raisebox` `\kern` `\phantom` `\rlap` `\llap` `\lower` `\raise`。出现即判为不通过。表格会在写回时按截图宽度整体缩放。不要为了通过校验去挪动文字，按表格本来的样子写。
- 截图不是表格（图、曲线、文字框）时写 `SKIP`。截图里除了表格还有表格边框外的无关文字行时，边框内的写成 `\multicolumn` 行；边框外的无法作为表格行放进去时，整个编号写 `SKIP`。

## 自查

运行 `python3 .skills/reconstruct_table/check.py`。它用 xelatex 编译 `rec.json` 里的每一条，与截图的文字层比较，第一行输出 `passed 通过数/总数`，后面每个没通过的编号一行，写出问题。只想检查几条时，把编号作为参数：`python3 .skills/reconstruct_table/check.py r-0012`。通途在会话结束后用同一套校验重新判定。

校验的方法：先把两边的文字切成单元格里的词，按多重集比较（不分大小写）；词的边界不同时改为逐字比较。然后比较相对位置（行的上下次序、同一行里的左右次序），最后逐字比较粗体与颜色。读差异的方法：

- `Characters in the original but missing from yours` 与 `Words differing`：字符或词不一致，按 `hints.json` 改正。
- `Layout differences`：某两段文字的上下或左右次序与原文不同，或者原文在同一行的文字在你的结果里相距很远。检查行的划分、`\makecell` 的折行与 `\multirow`。
- `Bold differs for`、`Colour differs for`：列出粗体或颜色不一致的单元格。
- `Your LaTeX failed`：编译失败或用了禁止的命令。

按差异修改，再运行 check.py，直到全部通过。确认改不对的条目写 `SKIP`。

## 结束时

结束前在当前目录写 `notes.json`，逐条记下这次遇到的问题：

```json
{"notes": [{"where": "...", "problem": "...", "resolution": "...", "suggestion": "..."}]}
```

`where` 写编号，`problem` 写识别或校验中遇到的困难，`resolution` 写你怎样处理，`suggestion` 写规则脚本或校验可以怎样改进。没有遇到问题时写 `{"notes": []}`。
