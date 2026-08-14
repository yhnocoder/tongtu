"""机械校验：译块必须与原块在结构上完全一致（架构 §3 translate 行的出口判据）。

四层检查，全部机械，不含任何「判断」——这是 translate 阶段「谁说了算」的那个谁：

1. **占位符**（`placeholders`）：`⟦BLK-3⟧` / `⟦CAP-2⟧` multiset 相等，外加占位符
   残缺自检（`⟦` `⟧` 数量必须与完整占位符数吻合，拦截 `⟦BLK-3⟧⟧` 这类碎片）。
2. **控制序列**（`control_sequences`）：`\\cmd`（含星号变体）与 `\\符号` multiset 相等。
3. **括号与行内数学**（`braces_and_math`）：未转义 `{` `}` `$` 计数分别相等。
4. **段落数**（`paragraph_count`）：空行分段的段落数相等——防简译 / 跳段。

层名与 `docs/schemas/report.schema.json` 的 `validation.failures_by_check` 键一一对应。

本模块**不是流水线阶段**（故不在 `tongtu/stages/` 下）：translate 的内环拿它当重试判据、
把 `Error` 喂回 agent 提示词，compile 的坏段重译与 `tongtu retranslate` 也共用同一份实现。
纯函数、无 IO、无第三方依赖。

    >>> check("Hello ⟦BLK-1⟧", "你好 ⟦BLK-1⟧")
    []
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

# --- 层名（对应 report.json 的 validation.failures_by_check 键）-------------

PLACEHOLDERS = "placeholders"
CONTROL_SEQUENCES = "control_sequences"
BRACES_AND_MATH = "braces_and_math"
PARAGRAPH_COUNT = "paragraph_count"

#: 四层校验名，顺序即 `check()` 的报错顺序。
CHECKS: tuple[str, ...] = (
    PLACEHOLDERS,
    CONTROL_SEQUENCES,
    BRACES_AND_MATH,
    PARAGRAPH_COUNT,
)

# --- 词法常量 ---------------------------------------------------------------

#: 掩码占位符：`⟦BLK-3⟧`、`⟦CAP-2⟧`（见 docs/schemas/blocks.schema.json）。
PLACEHOLDER_RE = re.compile(r"⟦[A-Z]+-\d+⟧")

#: 占位符定界符。mask 保留这两个码位，正文（含中文译文）不得出现。
OPEN_BRACKET = "⟦"
CLOSE_BRACKET = "⟧"

#: 控制序列：控制词 `\cmd` / `\cmd*` 或控制符 `\\` `\{` `\%` `\,` 等单字符形式。
#: `\section*` 与 `\section` 计为不同 token（星号影响编号，不能被译文抹掉）。
CONTROL_RE = re.compile(r"\\(?:[A-Za-z@]+\*?|[^A-Za-z@])")

#: 段落分隔：一个或多个空行（`\s` 含 `\r`，CRLF 源码同样适用）。
PARAGRAPH_SEP_RE = re.compile(r"\n\s*\n")

#: 计数型校验覆盖的未转义字符及其人读名。
COUNTED_CHARS: tuple[tuple[str, str], ...] = (
    ("{", "左花括号 {"),
    ("}", "右花括号 }"),
    ("$", "行内数学定界符 $"),
)


# --- 结构化错误 -------------------------------------------------------------


@dataclass(frozen=True)
class Error:
    """一条校验失败。

    结构化是刚需：`message` 进 agent 重试提示词，`check` 进 report.json 的
    `failures_by_check` 统计，`missing` / `extra` / `*_count` 供驱动侧做更细的
    诊断（例如 compile 的坏段定位）。

    :param check: 层名，取自 :data:`CHECKS`。
    :param message: 人读（且 agent 可读）的一句话。
    :param missing: 原文有、译文缺的 token，按重数展开后排序。
    :param extra: 译文多出的 token，同上。
    :param orig_count: 计数型校验的原文计数。
    :param trans_count: 计数型校验的译文计数。
    :param detail: 同层内的子项标识，如 ``"{"`` / ``"$"`` / ``"brackets"``。
    """

    check: str
    message: str
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    orig_count: int | None = None
    trans_count: int | None = None
    detail: str | None = None

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, object]:
        """JSON 可序列化形式（落 logs/ 与 report.json 用）。空字段省略。"""
        data: dict[str, object] = {"check": self.check, "message": self.message}
        if self.detail is not None:
            data["detail"] = self.detail
        if self.missing:
            data["missing"] = list(self.missing)
        if self.extra:
            data["extra"] = list(self.extra)
        if self.orig_count is not None:
            data["orig_count"] = self.orig_count
        if self.trans_count is not None:
            data["trans_count"] = self.trans_count
        return data


# --- 词法辅助 ---------------------------------------------------------------


def placeholders(text: str) -> Counter[str]:
    """文本中的占位符 multiset。"""
    return Counter(PLACEHOLDER_RE.findall(text))


def control_sequences(text: str) -> Counter[str]:
    """文本中的控制序列 multiset。"""
    return Counter(CONTROL_RE.findall(text))


def unescaped_count(text: str, char: str) -> int:
    """统计未被反斜杠转义的 `char` 出现次数。

    反斜杠吃掉紧随其后的一个字符，故 `\\{` 不计入花括号（它已作为控制序列
    `\\{` 进第 2 层），而 `\\\\{`（换行命令后接真花括号）计入——转义链按 TeX
    的方式左起两两消解，不是「前一个字符是不是反斜杠」。
    """
    total = 0
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2  # 反斜杠与被它转义的那个字符一起跳过（末尾孤立反斜杠亦安全）
            continue
        if text[i] == char:
            total += 1
        i += 1
    return total


def paragraph_count(text: str) -> int:
    """空行分段的段落数（首尾空白与纯空白段不计）。"""
    return sum(1 for part in PARAGRAPH_SEP_RE.split(text) if part.strip())


def _render(tokens: Iterable[str]) -> str:
    """把展开的 multiset 渲染成 `\\alpha×3, \\beta` 形式，供人读消息用。"""
    counts = Counter(tokens)
    return ", ".join(
        token if count == 1 else f"{token}×{count}" for token, count in sorted(counts.items())
    )


def _diff(orig: Counter[str], trans: Counter[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """multiset 差集，展开并排序（`Counter` 减法天然丢弃非正计数）。"""
    return (
        tuple(sorted((orig - trans).elements())),
        tuple(sorted((trans - orig).elements())),
    )


def _multiset_error(
    check_name: str,
    orig: Counter[str],
    trans: Counter[str],
    subject: str,
    hint: str,
) -> Error | None:
    """比对两个 multiset，不等则造一条 :class:`Error`，相等返回 ``None``。"""
    missing, extra = _diff(orig, trans)
    if not missing and not extra:
        return None
    parts = []
    if missing:
        parts.append(f"缺失 {_render(missing)}")
    if extra:
        parts.append(f"多出 {_render(extra)}")
    return Error(
        check=check_name,
        message=f"{subject}不一致：" + "；".join(parts) + f"（{hint}）",
        missing=missing,
        extra=extra,
    )


def _bracket_debris(text: str) -> tuple[int, int, int]:
    """返回 (完整占位符数, `⟦` 数, `⟧` 数)——三者不等即有残缺占位符。"""
    return (
        len(PLACEHOLDER_RE.findall(text)),
        text.count(OPEN_BRACKET),
        text.count(CLOSE_BRACKET),
    )


# --- 主校验 -----------------------------------------------------------------


def check(orig: str, trans: str) -> list[Error]:
    """比对原块与译块，返回全部失败项；空列表 = 全绿（translate 的放行条件）。

    只做机械比对，不判断译文质量。四层互为补充，任何一层失败都足以打回重译。
    """
    errors: list[Error] = []

    # 层 1：占位符 multiset --------------------------------------------------
    error = _multiset_error(
        PLACEHOLDERS,
        placeholders(orig),
        placeholders(trans),
        subject="占位符",
        hint="占位符须原样逐个保留，不得增删改",
    )
    if error is not None:
        errors.append(error)

    # 层 1b：占位符残缺自检。multiset 相等挡不住 `⟦BLK-3⟧⟧` 这类碎片——它带着
    # 一个完整占位符，却把多余的定界符漏进回填后的 TeX。故两边各自内部自洽。
    debris = {"原文": _bracket_debris(orig), "译文": _bracket_debris(trans)}
    for label, (complete, opens, closes) in debris.items():
        if opens != complete or closes != complete:
            errors.append(
                Error(
                    check=PLACEHOLDERS,
                    detail="brackets",
                    message=(
                        f"{label}存在残缺占位符：完整占位符 {complete} 个，"
                        f"但 ⟦ 出现 {opens} 次、⟧ 出现 {closes} 次"
                        "（⟦⟧ 为掩码保留符号，只能成对出现在完整占位符里）"
                    ),
                    orig_count=sum(debris["原文"][1:]),
                    trans_count=sum(debris["译文"][1:]),
                )
            )

    # 层 2：控制序列 multiset ------------------------------------------------
    error = _multiset_error(
        CONTROL_SEQUENCES,
        control_sequences(orig),
        control_sequences(trans),
        subject="控制序列",
        hint="LaTeX 命令须原样保留，含星号变体",
    )
    if error is not None:
        errors.append(error)

    # 层 3：未转义 { } $ 计数 ------------------------------------------------
    for char, label in COUNTED_CHARS:
        a, b = unescaped_count(orig, char), unescaped_count(trans, char)
        if a != b:
            errors.append(
                Error(
                    check=BRACES_AND_MATH,
                    detail=char,
                    message=f"{label} 计数不一致：原文 {a} 个，译文 {b} 个",
                    orig_count=a,
                    trans_count=b,
                )
            )

    # 层 4：段落数 -----------------------------------------------------------
    a, b = paragraph_count(orig), paragraph_count(trans)
    if a != b:
        errors.append(
            Error(
                check=PARAGRAPH_COUNT,
                message=f"段落数不一致：原文 {a} 段，译文 {b} 段（禁止合并 / 拆分 / 跳过段落）",
                orig_count=a,
                trans_count=b,
            )
        )

    return errors


# --- 驱动侧辅助 -------------------------------------------------------------


def failed_checks(errors: Iterable[Error]) -> tuple[str, ...]:
    """一次 :func:`check` 结果中失败的层名，按 :data:`CHECKS` 顺序去重。"""
    names = {error.check for error in errors}
    return tuple(name for name in CHECKS if name in names)


def summarize(errors: Iterable[Error]) -> dict[str, int]:
    """把**单块**的校验结果折成 `{层名: 1}`，供驱动侧累加进 report.json。

    同层多条（例如 `{` 与 `}` 同时对不上）只记 1，因为
    `validation.failures_by_check` 统计的是「有多少块在这一层栽了」。
    """
    return {name: 1 for name in failed_checks(errors)}


def format_errors(errors: Iterable[Error]) -> str:
    """渲染成喂回 agent 的错误清单（每行一条，带序号）。"""
    return "\n".join(f"{i}. {error.message}" for i, error in enumerate(errors, 1))


__all__ = [
    "BRACES_AND_MATH",
    "CHECKS",
    "CONTROL_RE",
    "CONTROL_SEQUENCES",
    "Error",
    "PARAGRAPH_COUNT",
    "PLACEHOLDERS",
    "PLACEHOLDER_RE",
    "check",
    "control_sequences",
    "failed_checks",
    "format_errors",
    "paragraph_count",
    "placeholders",
    "summarize",
    "unescaped_count",
]
