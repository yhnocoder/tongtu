# example 论文

`examples/papers/` 下的三篇**自造最小模板论文**，是可运行 example 的输入：零期交付
判据即「三篇不同模板论文本地出全套产物包」，mock 运行时（恒等翻译）下全流水线跑到底
就是最直接的回归检查。

## 约定：真实 arXiv 论文不入库

**仓库里只放自造论文。** 真实 arXiv 论文的 license 五花八门（相当一部分 e-print 根本
没有明确许可），把它们的 LaTeX 源码、图片与参考文献签进一个开源仓库会带来无法一次性
清理的分发义务。因此：

* **入库**：这三篇论文——标题、正文、公式、表格数字、参考文献条目、图片**全部现造**，
  与任何真实工作无关；技术名词一律泛化（"placeholder"、"synthetic"、"toy"）。
* **不入库**：任何真实 e-print 的源码或图。真实论文只在 LLM 层（手动触发，架构 §7 第 3 层）
  与开发者本机按需拉取，跑完即弃，不落仓库。

三篇的内容有意写得空洞而自洽：它们是**版式与语法的样本**，不是可读的论文。

## 三篇的定位

| 目录 | id | documentclass | 栏 | 侧重 |
|---|---|---|:--:|---|
| `article/` | `fixture-article` | `article`（11pt, a4paper） | 1 | **多文件源码树**：`main.tex` + `macros.tex` + `sections/×4` 经 `\input`，`refs.bib` 走 bibtex——练 flatten。`\title` 在前导区（与 revtex 篇互补）；`\newtheorem` 与 `\newenvironment` 声明驱动分类；verbatim + lstlisting；caption 可选参数 |
| `revtex/` | `fixture-revtex` | `revtex4-2`（aps, prd, twocolumn） | 2 | **物理双栏**：`\title`/abstract 在 `\begin{document}` 之后（与 article 篇互补）；`widetext` 是类自带、分类表外的环境——练**保守整块掩码**；`ruledtabular` 嵌套 `tabular`；`\caption{\label{...}...}` 的 revtex 惯用写法；手写 `thebibliography`，不走 bibtex |
| `conference/` | `fixture-conference` | `IEEEtran`（conference） | 2 | **双栏会议**：跨栏浮动体 `figure*`（练星号变体继承）；`IEEEkeywords`；`\appendices`；本地 `fixturestyle.sty` 提供的 `sidenote` 环境——latexpand 默认不展开 `\usepackage`，故它在 flat 视图里同样是分类表外的未知环境；`refs.bib` 与预编译 `main.bbl` 同时入库，bibtex 与 bbl 内联两条路径都走得通 |

规模均为 2–4 页。选 `IEEEtran` 而非 `acmart` 作会议版式：TeX Live full 里两者都有，但
`IEEEtran` 的必需命令集更小更稳（`acmart` 对 `\acmConference`、CCS 概念等有一串强制项）。

## 真实论文试跑对象（源码不入库）

自造论文覆盖版式与语法，真实论文覆盖真实 e-print 的下载形态与源码杂质。以下八篇是
各阶段重建过程中的固定试跑对象；按「真实 arXiv 论文不入库」的约定，源码不进仓库，
用 `tongtu stage fetch <arxiv_id>` 按需拉取即可（工作目录默认在 `~/.local/share/tongtu/`，
本就不在仓库内）：

| arXiv id | 定位 |
|---|---|
| `2002.05202` | 第一篇：结构简单（单个 `main.tex` 加预编译 `main.bbl`），v2 原型完整处理过，有既往结果可对照 |
| `1701.06538` | 第二篇：单个 `main.tex`，但图源格式多（PNG、PDF、EPS 并存）、随源码带多个本地 `.sty` |
| `2412.19437` | 多级 `\input`（`content/`、`tables/`）加自定义 `deepseek.cls`；`main.tex` 里同时有生效的与注释掉的 `\bibliography` 行，是 bbl 内联注释判定的实测用例 |
| `2106.04426` | 主文件名是 `neurips_2021.tex` 而非 `main.tex`；图形源码 `figs/model.tex` 经 `\input` 内联 |
| `2409.19606` | 主文件名是 `iclr2025_conference.tex`；根目录另有多个辅 `.tex`（`app.tex`、`math_commands.tex` 等），主文件判定不受干扰 |
| `2512.02556` | `sections/` 与 `tables/` 深层结构；有 `.bib` 无 `.bbl`，参考文献走 bibtex |
| `2512.24880` | 有 `.bib` 无 `.bbl`；正文直接使用 @-命令（`\c@figure`），latexpand 对此报 `--makeatletter` 警告，是「警告如实记录、不拦产出」的实测用例 |
| `2604.15804` | 主文件名是 `colm2024_conference.tex`；`content/contributor/` 两级嵌套 `\input`，无 `.bbl` |

顺序约定：每个阶段先在三篇自造论文上走通，再按表内自上而下的顺序上真实论文
（`2002.05202` 最先）。

## `MANIFEST.json`

每篇一份，是该篇覆盖点的机器可读清单。字段：

| 字段 | 说明 |
|---|---|
| `id` | 假 arXiv id，形如 `fixture-<目录名>`；工作目录用它 |
| `title` / `layout` / `documentclass` / `class_options` / `columns` | 版式身份，测试与 `main.tex` 的 `\documentclass` 逐项比对 |
| `main` | 主文件名 |
| `inputs` | `\input` 展开顺序（flatten 的期望结果） |
| `aux_files` | `.bib` / `.sty` / `.bbl` 等非 `.tex` 附属文件 |
| `generated_assets` | 由 `gen_assets.py` 生成的图，路径相对论文目录 |
| `pages_estimate` | 目测页数，只用于「规模没跑偏」的粗判 |
| `coverage` | **机器可读的覆盖点清单**（排序、去重），词表见下 |
| `notes` | 人话说明，包括已知问题 |

覆盖点与源码的一致性此前由测试探针保证：声明的覆盖点必须真在源码里、三篇的并集等于
全部词表。测试套件随重构移除，重建测试时恢复这两端校验；在那之前改动论文源码需同步
维护 MANIFEST。

## 覆盖矩阵

| 覆盖点 | article | revtex | conference |
|---|:---:|:---:|:---:|
| `abstract` | ✓ | ✓ | ✓ |
| `align_env` | ✓ | ✓ | |
| `appendix` | ✓ | ✓ | ✓ |
| `asset_pdf` | ✓ | ✓ | |
| `asset_png` | ✓ | | ✓ |
| `bibtex_database` | ✓ | | ✓ |
| `caption_label_inline` | | ✓ | |
| `caption_optional_arg` | ✓ | | |
| `cite` | ✓ | ✓ | ✓ |
| `comment_run` | ✓ | | |
| `custom_env_declared` | ✓ | | |
| `custom_env_unknown` | | ✓ | ✓ |
| `custom_macro` | ✓ | ✓ | ✓ |
| `enumerate` | ✓ | | ✓ |
| `equation_env` | ✓ | ✓ | ✓ |
| `escaped_ampersand` | ✓ | ✓ | ✓ |
| `escaped_hash` | ✓ | ✓ | ✓ |
| `escaped_percent` | ✓ | ✓ | ✓ |
| `figure_env` | ✓ | ✓ | ✓ |
| `figure_starred` | | | ✓ |
| `footnote` | ✓ | ✓ | ✓ |
| `includegraphics` | ✓ | ✓ | ✓ |
| `inline_math` | ✓ | ✓ | ✓ |
| `itemize` | ✓ | | ✓ |
| `label` | ✓ | ✓ | ✓ |
| `local_sty_package` | | | ✓ |
| `lstlisting_env` | ✓ | | |
| `multi_file_input` | ✓ | | |
| `nested_env` | ✓ | ✓ | ✓ |
| `newtheorem` | ✓ | | |
| `precompiled_bbl` | | | ✓ |
| `ref` | ✓ | ✓ | ✓ |
| `section` | ✓ | ✓ | ✓ |
| `subsection` | ✓ | ✓ | ✓ |
| `subsubsection` | ✓ | | |
| `table_env` | ✓ | ✓ | ✓ |
| `tabular_env` | ✓ | ✓ | ✓ |
| `thebibliography_env` | | ✓ | |
| `theorem_env_usage` | ✓ | | |
| `title` | ✓ | ✓ | ✓ |
| `title_in_preamble` | ✓ | | |
| `two_column` | | ✓ | ✓ |
| `verbatim_env` | ✓ | | |

三条与 mask 分类来源一一对应的覆盖点值得单独点名（架构 §3.1 第 2 条的优先级链）：

* `theorem_env_usage` / `custom_env_declared` —— 文档**自带声明**（`\newtheorem` /
  `\newenvironment`）判出的散文环境，`decided_by` 为 `newtheorem` / `newenvironment`；
* `custom_env_unknown` —— 分类表里没有、文档里也没声明（revtex 的 `widetext`、
  conference 的 `sidenote`），走**保守默认**：整块掩码、`category=unknown`、
  `decided_by=default`。这条是「不确定就别翻，只降覆盖率不损坏」的实际用例。

## 图片资产的再生

`figures/` 下的 PNG 与 PDF 由 `examples/gen_assets.py` 现场生成（零第三方依赖：
`zlib` + `struct` 手写 PNG chunk 与 CRC32、手写 PDF 1.4 的对象表 / xref / trailer）。
生成物**提交进仓库**（CI 不跑生成器），但随时可复跑：

```sh
uv run python examples/gen_assets.py           # 重新写出全部资产
uv run python examples/gen_assets.py --check   # 只比对，不写盘
```

比对口径：**PDF 逐字节**，**PNG 比结构**（IHDR + 解压后的像素流）——PNG 的 IDAT 是 zlib
压缩结果，理论上随 zlib 版本可变，比结构才是稳的。改图请改 `gen_assets.py` 的 `ASSETS`
清单再复跑，不要手工替换二进制。

## 已知问题（等后续重建处理）

1. **`\appendices` 的识别是 chunk 阶段重建时的注意项。** conference 篇用的是 IEEEtran
   的 `\appendices` 命令；此前实现的附录正则 `\\appendix(?:es)?` 匹配 `\appendix` 与
   （并不存在的）`\appendixes`，**不匹配**真实命令 `\appendices`，导致该篇附录段落被
   当作正文聚合。论文源码保留 `\appendices` 原样（IEEEtran 的惯用写法，example 应当
   照实反映真实论文），识别要覆盖 `\appendices`、`\begin{appendices}`（appendix 宏包）
   与 `\appendix`（article / revtex）三条路径。
