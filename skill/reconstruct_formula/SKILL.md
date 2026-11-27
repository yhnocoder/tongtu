---
name: reconstruct_formula
description: reconstruct 阶段公式识别会话的任务说明：对照截图与文字层，把行间公式和行内公式的截图写成 LaTeX，并用 check.py 自查到通过。
version: 1
---

# 把公式截图写成 LaTeX

当前目录里是从一篇论文的 PDF 截出的公式：

- `img/<编号>.png`：每个公式一张图。编号以 `e-` 开头的是行间公式，以 `m-` 开头的是行内公式。
- `hints.json`：每个编号的类别（`display` 或 `inline`）与截图的文字层，每段写成 `字体名#颜色:文字`。字符以文字层为准。字体名含 MIB、BX、Bold 的字形是粗体，BlackboardBold 不是粗体。颜色不是 `000000` 的字形带颜色。
- `crops/<编号>.pdf`：截图原件，check.py 用它比对，不要改动。

你只写 `rec.json` 和 `notes.json`。`rec.json` 的格式是 `{"编号": "LaTeX"}`，覆盖 `hints.json` 里的每个编号。通过校验的公式会替换论文里的截图，没通过的保留截图。

## 要求

用 Read 工具看图，一次可以看几张；也可以用 PyMuPDF 把多张图拼成一张总览图再看。照图写出 LaTeX：

- 只写数学正文，不带 `$` 或 `\[ \]`。
- 符号与顺序和图一致。
- 粗体只加在粗体字形上：写 `\bm{k}_t`，不写 `\bm{k_t}`；直立粗体用 `\mathbf`。
- 带颜色的部分写成 `{\color[HTML]{RRGGBB} ...}`。
- 字号样式与图一致：小号分式写 `\tfrac` 或 `a/b`，小号矩阵用 `smallmatrix`。
- 行间公式右侧有编号 (n) 时，在末尾写 ` \quad (n)`。
- 只用 amsmath、amssymb、bm、mathtools、xcolor。
- 禁止使用排版调整命令：`\raisebox` `\hspace` `\vspace` `\kern` `\mkern` `\phantom` `\rlap` `\llap` `\mathrlap` `\mathllap` `\lower` `\raise`，以及 `\tiny` 到 `\Huge` 的字号命令。出现即判为不通过。不要为了通过校验去挪动字形，按公式本来的样子写。
- 行间公式只能用 aligned、split、array、matrix、pmatrix、bmatrix、cases、gathered 这几个环境。
- 不是数学的截图（代码、表格、图、纯文字），以及以 `\text{...}` 写出的一行文字为主的行间公式，写 `SKIP`。

公式多时每写完约 50 个就保存一次 `rec.json`。

## 自查

运行 `python3 .skills/reconstruct_formula/check.py`。它把 `rec.json` 里的每一条单独编译，与截图的文字层逐个字形比较字符序列、粗体、颜色、每个字形相对首字形的基线位置与字号，第一行输出 `passed 通过数/总数`，后面每个没通过的编号一行，写出问题和你的 LaTeX。只想检查几条时，把编号作为参数：`python3 .skills/reconstruct_formula/check.py e-0003 m-0010`。通途在会话结束后用同一套校验重新判定。

读差异的方法：

- `Glyph sequence differs`：列出原文与你的结果不同的片段。按 `hints.json` 改正字符，例如 `'` 与 `\prime`、`-` 与 `−`、`\ell` 与 `l`。
- `bold pattern differs`：字符一致而粗体不一致，对照 `hints.json` 的字体名看哪些字形是粗体。
- `colour differs`：字符一致而颜色不一致，对照 `hints.json` 的颜色。
- `vertical placement or size`：字符一致，而某些字形的基线位置或字号不同。检查上下标、分式样式（`\frac`、`\tfrac` 或 `a/b`）、`\scriptstyle`、文字与数学符号的区别、大型运算符上下限的位置。
- `LaTeX failed to compile`：编译失败，后面是 TeX 的第一条错误。

按差异修改，再运行 check.py，直到全部通过。确认改不对的条目写 `SKIP`。

## 结束时

结束前在当前目录写 `notes.json`，逐条记下这次遇到的问题：

```json
{"notes": [{"where": "...", "problem": "...", "resolution": "...", "suggestion": "..."}]}
```

`where` 写编号，`problem` 写识别或校验中遇到的困难，`resolution` 写你怎样处理，`suggestion` 写规则脚本或校验可以怎样改进。没有遇到问题时写 `{"notes": []}`。
