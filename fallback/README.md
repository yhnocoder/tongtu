# fallback/ — 非 arXiv PDF 的降级流水线（占位，零期不实现）

主线只走 LaTeX 源码：取 arXiv e-print，掩码 → 翻译 → 机械校验 → 编译回环（见 [ARCHITECTURE.md](../docs/ARCHITECTURE.md)）。
但有两类论文没有可用源码：

- arXiv 上只投了 PDF（PDF-only），或源码是 `pdfpages` 套壳；
- 根本不在 arXiv 上的 PDF。

这些走本目录的降级路线：PDF → markdown（v1 的 doc2x 路线）→ 翻译 → 重排版。参考实现是
[arxiv_translator_v1](https://github.com/yhnocoder/arxiv_translator_v1) 的 `download.py` 与 `translate_pdf.sh`。

**零期只做检测与标记，不实现降级流水线**（见 docs/PHASE0.md §5 边界）：`fetch` 阶段识别
PDF-only / `pdfpages` 套壳后报错并标记降级路线，到此为止。

已知的路线代价（v1 的经验，已内化为架构决策 9 与「不解析 PDF」的立项动机）：PDF/EPS 图无法直接用于产物、
排版信息丢失、公式与表格保真度差——所以它是降级路线，不是主线的备选。
