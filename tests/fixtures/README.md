# fixture 论文

`tests/fixtures/papers/` 下的三篇**自造最小模板论文**，是编译层 e2e（架构 §12 层 2：
MockAgent 恒等翻译、三篇全流水线跑到底、产出 PDF + anchors 并过 schema 校验）的输入，
也是文本层冒烟测试 `tests/test_fixtures.py` 的对象。

## 约定：真实 arXiv 论文不入库

**仓库里只放自造论文。** 真实 arXiv 论文的 license 五花八门（相当一部分 e-print 根本
没有明确许可），把它们的 LaTeX 源码、图片与参考文献签进一个开源仓库会带来无法一次性
清理的分发义务。因此：

* **入库**：这三篇论文——标题、正文、公式、表格数字、参考文献条目、图片**全部现造**，
  与任何真实工作无关；技术名词一律泛化（"placeholder"、"synthetic"、"toy"）。
* **不入库**：任何真实 e-print 的源码或图。真实论文只在 LLM 层（手动触发，架构 §12 层 3）
  与开发者本机按需拉取，跑完即弃，不落仓库。

三篇的内容有意写得空洞而自洽：它们是**版式与语法的样本**，不是可读的论文。

## 三篇的定位

| 目录 | id | documentclass | 栏 | 侧重 |
|---|---|---|:--:|---|
| `article/` | `fixture-article` | `article`（11pt, a4paper） | 1 | **多文件源码树**：`main.tex` + `macros.tex` + `sections/×4` 经 `\input`，`refs.bib` 走 bibtex——练 flatten。`\title` 在前导区（唯一一篇产出 title CAP 槽位）；`\newtheorem` 与 `\newenvironment` 声明驱动分类；verbatim + lstlisting；caption 可选参数 |
| `revtex/` | `fixture-revtex` | `revtex4-2`（aps, prd, twocolumn） | 2 | **物理双栏**：`\title`/abstract 在 `\begin{document}` 之后（与 article 篇互补）；`widetext` 是类自带、分类表外的环境——练**保守整块掩码**；`ruledtabular` 嵌套 `tabular`；`\caption{\label{...}...}` 的 revtex 惯用写法；手写 `thebibliography`，不走 bibtex |
| `conference/` | `fixture-conference` | `IEEEtran`（conference） | 2 | **双栏会议**：跨栏浮动体 `figure*`（练星号变体继承）；`IEEEkeywords`；`\appendices`；本地 `fixturestyle.sty` 提供的 `sidenote` 环境——latexpand 默认不展开 `\usepackage`，故它在 flat 视图里同样是分类表外的未知环境；`refs.bib` 与预编译 `main.bbl` 同时入库，bibtex 与 `latexpand --expand-bbl` 两条路径都走得通 |

规模均为 2–4 页。选 `IEEEtran` 而非 `acmart` 作会议版式：TeX Live full 里两者都有，但
`IEEEtran` 的必需命令集更小更稳（`acmart` 对 `\acmConference`、CCS 概念等有一串强制项）。

## `MANIFEST.json`

每篇一份，供 e2e 断言「覆盖点没有缩水」。字段：

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

`coverage` 的每个键在 `tests/test_fixtures.py` 的 `PROBES` 里有**探针**：正则或 mask 的
分类结论。`test_claimed_coverage_is_real` 保证「声明了就必须真的在源码里」（MANIFEST 不许
说谎），`test_coverage_matrix_is_complete` 保证「三篇的并集 == 全部词表」（谁删了覆盖点
而没改 MANIFEST，当场红）。加新覆盖点 = 加探针 + 改 MANIFEST，两边缺一不可。

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
  `decided_by=default`。这条是「不确定就别翻，只降覆盖率不损坏」的活体样本。

## 图片资产的再生

`figures/` 下的 PNG 与 PDF 由 `tests/fixtures/gen_assets.py` 现场生成（零第三方依赖：
`zlib` + `struct` 手写 PNG chunk 与 CRC32、手写 PDF 1.4 的对象表 / xref / trailer）。
生成物**提交进仓库**（CI 不跑生成器），但随时可复跑：

```sh
uv run python tests/fixtures/gen_assets.py           # 重新写出全部资产
uv run python tests/fixtures/gen_assets.py --check   # 只比对，不写盘
```

比对口径：**PDF 逐字节**，**PNG 比结构**（IHDR + 解压后的像素流）——PNG 的 IDAT 是 zlib
压缩结果，理论上随 zlib 版本可变，比结构才是稳的。改图请改 `gen_assets.py` 的 `ASSETS`
清单再复跑，不要手工替换二进制。

## 已知问题（等编译层与后续里程碑处理）

1. **本机无 TeX，三篇均未真编译过。** 本文件与 `tests/test_fixtures.py` 只保证
   LaTeX 语法自查（配平、环境闭合、宏包限于 TeX Live full 必有）与文本层恒等。
   「编译通过」的裁决权在参考镜像（架构 §10），CI 编译层落地后红了再修。
2. **`\appendices` 不被 chunk 识别为附录。** conference 篇用的是 IEEEtran 的
   `\appendices` 命令，而 `tongtu/stages/chunk.py` 的 `_APPENDIX_RE` 写的是
   `\\appendix(?:es)?` —— 它匹配 `\appendix` 与（并不存在的）`\appendixes`，**不匹配**
   真实命令 `\appendices`。后果是 conference 篇的附录段落 `is_appendix` 为假、附录块会
   与正文块聚合。fixture 保留 `\appendices` 原样（这是 IEEEtran 的惯用写法，fixture 应当
   照实反映真实论文），修在 `chunk.py` 侧。`\begin{appendices}`（appendix 宏包）与
   `\appendix`（article / revtex）两条路径目前是好的。
