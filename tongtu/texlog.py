r"""xelatex 与 latexmk 编译日志的读取与解析：基线计数与错误行提取。

消费方是 precompile 阶段（出口判据的页数、四类警告计数，以及修复会话 prompt 里的错误行
摘录）与将来的 compile 阶段（同一套出口判据，并拿计数与 precompile 记下的基线比对）。本
模块只处理日志文本，不执行编译、不写工作目录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: 页数所在的行：`Output written on flat.xdv (9 pages, 271052 bytes).`。文件名可能是
#: `.pdf` 也可能是中转的 `.xdv`，故不匹配扩展名。
PAGES_RE = re.compile(r"Output written on .*?\((\d+) pages?")

#: 三类警告的行首前缀与一类的行内标记，逐行计数用。
OVERFULL_HBOX_PREFIX = "Overfull \\hbox"
UNDEFINED_REFERENCE_PREFIX = "LaTeX Warning: Reference"
UNDEFINED_CITATION_PREFIX = "LaTeX Warning: Citation"
MISSING_CHARACTER_MARKER = "Missing character"

#: 错误行的行首前缀：TeX 把每条错误写成以 `!` 开头的一行。
ERROR_LINE_PREFIX = "!"


@dataclass(frozen=True)
class LogCounts:
    """从编译日志解析出的基线数据。"""

    pages: int = 0
    overfull_hboxes: int = 0
    undefined_references: int = 0
    undefined_citations: int = 0
    missing_characters: int = 0


def read_log(log_path: Path) -> str | None:
    """读编译日志；读不到返回 None（调用方改摘编译命令的 stderr）。

    TeX 的 log 混杂多种编码（宏包名、字体名、原文片段），解码错误一律替换，不中断解析。
    """
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def parse_counts(log_text: str | None) -> LogCounts:
    """从日志文本解析页数与四类计数；日志读不到时全部为 0。

    页数取最后一次匹配：多 pass 编译会写多行 `Output written on`。三类警告按行首前缀
    匹配（log 默认在 79 列折行，`undefined` 未必与前缀同行），一类按行内标记匹配。
    """
    if log_text is None:
        return LogCounts()
    pages_matches = PAGES_RE.findall(log_text)
    lines = log_text.splitlines()
    return LogCounts(
        pages=int(pages_matches[-1]) if pages_matches else 0,  # 多 pass 会写多行，取最后一次
        overfull_hboxes=sum(1 for line in lines if line.startswith(OVERFULL_HBOX_PREFIX)),
        undefined_references=sum(1 for line in lines if line.startswith(UNDEFINED_REFERENCE_PREFIX)),
        undefined_citations=sum(1 for line in lines if line.startswith(UNDEFINED_CITATION_PREFIX)),
        missing_characters=sum(1 for line in lines if MISSING_CHARACTER_MARKER in line),
    )


def error_lines(log_text: str, limit: int) -> list[str]:
    """取日志里以 `!` 开头的错误行，至多 `limit` 条；没有这样的行返回空列表。

    条数上限由调用方按用途给出：摘进 manifest 的 message 取较小的值，摘进修复会话 prompt
    的取较大的值。
    """
    return [line for line in log_text.splitlines() if line.startswith(ERROR_LINE_PREFIX)][:limit]
