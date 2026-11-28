# 算法与代码块识别试跑

试跑对象是带 Algorithm 或 Listing 图注的区域。通途从 PDF 重建时，这些区域现在作为截图保留，中文 PDF 里无法复制。脚本把这些截图交给本地 OCR 服务识别，再把识别结果编译回去，与截图的 PDF 文字层逐字比对。重建与截图来自通途 main（cd50322），由 uv 自动安装。

需要：uv、TeX Live（xelatex）、已启动的 OpenAI 兼容服务（默认 oMLX，地址 http://127.0.0.1:11000/v1，模型 TeleOCR-oQ8-MLX）。

    git clone -b claude/project-thread-eyyilf https://github.com/yhnocoder/tongtu code-trial
    cd code-trial
    ./run.sh

默认跑两组：

- `textlayer`：不调用模型，直接按文字层的字符和 x 坐标拼出代码行与缩进，只用于 Listing。用来对照不用 OCR 能做到什么程度。
- `tele_mlx`：TeleOCR 的代码块提示词（`The image contains a code snippet, please output the parsing result.`）。

其他组合写成「结果名:提示词组」，提示词组是 tele、tele_text、mineru、paddle、glm、minicpm 之一：

    API_MODEL=MinerU2.5-Pro ./run.sh mineru_mlx:mineru
    ./run.sh tele_text:tele_text

服务地址与模型用 `API_BASE`、`API_MODEL` 修改；同时发 4 个请求，用 `CONCURRENCY` 修改。只跑部分论文：`PAPERS="flashattn:2205.14135 pytorch:1912.01703" ./run.sh`。已识别过的截图会跳过，加 `RERUN=1` 全部重跑。

## 判定

识别结果先编译回 PDF：Listing 放进 Verbatim 等宽环境；Algorithm 每行一段，`$...$` 内按公式编译，行号后的空格数换算成缩进，宽度与字号取原截图的值。然后把编译结果与原截图的 PDF 文字层比较。比较按「逻辑行」进行：Listing 每个文字行是一行；有行号的 Algorithm 以行号分行，折到下一行的部分并入上一行，所以两边折行位置不同不影响结果。五项检查：

- 文字一致：去掉空白后，两边的字符完全相同（按字符计数比较）。字符相似度是两边共有字符数乘 2 除以两边字符总数。
- 上下标字号一致：同一行里相同的字符，相对本行基线的高度差不超过 0.25 个字号，字号差不超过 0.15。只检查 Algorithm，行号不参与。
- 缩进一致：每个逻辑行的缩进层级相同。
- 逐行一致：逻辑行数相同，且每一行的文字都相同。
- 粗体不一致字符：只记录，不计入通过条件。TeleOCR 不输出 `\textbf`，算法里的 for、do 等关键字都会计入这一项。

通过要求前四项都满足。行号与公式里的大括号、取整符号等可伸缩符号不参与比较。

## 结果

终端打印汇总表，同时写到 `work/summary.tsv`。`work/report.html` 逐条列出原截图、编译回去的样子、问题和模型原始输出，用浏览器打开。每条耗时是一组截图的实际总耗时除以条数，识别与比对分开列出。
