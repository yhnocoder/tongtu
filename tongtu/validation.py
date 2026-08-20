r"""译文与原文的结构一致性校验：四层机械比对，不含判断。

本模块是核心文本层，只用标准库与同层的 `masking`，不做文件读写。四层的层名即
`CHECK_NAMES`，与 report 的 `validation.failures_by_check` 键一一对应；设计是
`docs/stages/translate.md` 的 validate 四层节。三个调用方走同一份实现：translate 驱动器的
出口终审、CLI 子命令 `tongtu validate`、compile 终审对正文控制序列的比对（只用第 2 层）。

四层各自的判定：

- `placeholders`：`⟦BLK-n⟧` / `⟦CAP-n⟧` 的 multiset 相等，另加残缺自检——译文里 `⟦` 与 `⟧`
  的出现次数必须与完整 placeholder 数吻合，拦截 `⟦BLK-3⟧⟧` 这类碎片。
- `control_sequences`：控制序列的 multiset 相等。名字取 `\cmd` 的命令名并带上紧随的星号
  （`\section*` 与 `\section` 是两个不同的项），`\%` 一类符号控制序列取那个符号本身。
- `braces_and_math`：未转义的 `{`、`}`、`$` 三者的计数分别相等。转义形态（`\{`、`\$`）是
  控制序列，由上一层管，不计入本层。
- `paragraph_count`：含可译文本的段落数相等。一段剥除 placeholder、`\begin{X}` 与
  `\end{X}` 整体、其余控制序列的命令名（参数保留）之后仍有非空白字符才计入；只含
  placeholder 或只含 `\maketitle`、`\newpage` 这类命令的段落不计入——模型在这些位置合并
  空行对排版没有影响，口径依据见 docs/models.md 空行为什么会被吞节。

失败信息写成人读的差异说明，既进 manifest 与 report，也原样附进重试的提示词：驱动器带着
它重新 ask，模型据此知道自己漏了哪个 placeholder、多了哪个控制序列。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from . import chunking, masking

#: 四层的层名，按执行顺序；report 的 `validation.failures_by_check` 用同一组键。
CHECK_PLACEHOLDERS = "placeholders"
CHECK_CONTROL_SEQUENCES = "control_sequences"
CHECK_BRACES_AND_MATH = "braces_and_math"
CHECK_PARAGRAPH_COUNT = "paragraph_count"
CHECK_NAMES: tuple[str, ...] = (
    CHECK_PLACEHOLDERS,
    CHECK_CONTROL_SEQUENCES,
    CHECK_BRACES_AND_MATH,
    CHECK_PARAGRAPH_COUNT,
)

#: 段落里剥除的环境定界符：`\begin{X}` / `\end{X}` 整体不算可译文本，`\begin` 后面跟着的
#: 可选参数与花括号参数组一并剥除（`\begin{tabular}{ll}` 的列格式、`\begin{CJK*}{UTF8}{gbsn}`
#: 的编码与字体都不是正文）。环境名的字符集与 `masking.ENVIRONMENT_NAME_RE` 一致。
ENVIRONMENT_DELIMITER_RE = re.compile(
    r"\\begin\s*\{" + masking.ENVIRONMENT_NAME_RE.pattern + r"\}(?:\[[^\]]*\])*(?:\{[^{}]*\})*"
    r"|\\end\s*\{" + masking.ENVIRONMENT_NAME_RE.pattern + r"\}"
)

#: 段落里剥除的控制序列命令名（参数保留）：`\cmd`（可带星号）或 `\符号`。
CONTROL_SEQUENCE_NAME_RE = re.compile(r"\\(?:[A-Za-z]+\*?|.)", re.DOTALL)

#: 参数不是正文的命令：判定段落是否含可译文本时连参数一起剥。`\section{Introduction}` 的参数
#: 要译，`\vspace{-0.4in}` 的不要，两者机械上无从分辨，故把后一类列成数据。清单只收实际在
#: 语料里单独成段的命令，未列出的命令仍按「参数是正文」处理——那个方向的误差只多一次重试，
#: 反方向会让漏译静默通过。
NON_TEXT_ARGUMENT_COMMANDS: tuple[str, ...] = (
    "vspace",
    "hspace",
    "label",
    "bibliography",
    "bibliographystyle",
    "definecolor",
    "setcounter",
    "setlength",
    "input",
    "include",
    "includegraphics",
    "usepackage",
    "ref",
    "eqref",
    "cite",
    "citep",
    "citet",
)

#: 上面这些命令连同它们的星号、可选参数与花括号参数组的匹配式（参数组不含嵌套花括号）。
#: 命令名后断言不再跟字母，否则 `\include` 会匹配掉 `\includegraphics` 的前半截、`\ref`
#: 会匹配掉 `\refstepcounter` 的前半截，剩下的尾巴被当成可译文本。
NON_TEXT_COMMAND_RE = re.compile(
    r"\\(?:" + "|".join(NON_TEXT_ARGUMENT_COMMANDS) + r")(?![A-Za-z])\*?(?:\[[^\]]*\])*(?:\{[^{}]*\})*"
)

#: 差异说明里逐项列出的上限；超出的以「等 N 项」收尾，避免一条 message 撑爆 manifest。
DIFFERENCE_ITEMS_MAX = 8


#: `braces_and_math` 层逐个比对计数的未转义字符，也是 `scan` 产出的计数字典的键。
COUNTED_CHARACTERS: tuple[str, ...] = ("{", "}", "$", "%")


@dataclass(frozen=True)
class Scan:
    """一次词法扫描的产出：控制序列清单与未转义的 `{`、`}`、`$` 计数。"""

    control_sequences: tuple[str, ...]
    counts: dict[str, int]


@dataclass(frozen=True)
class Failure:
    """一层校验的失败：层名与人读的差异说明。"""

    check: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """一次四层校验的结果。`failures` 为空即全绿，按 `CHECK_NAMES` 的顺序排列。"""

    failures: tuple[Failure, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def validate(source: str, translation: str) -> ValidationResult:
    """比对原文与译文的结构，返回四层校验结果。

    两侧都按正文比对：首尾空白由驱动器保管，不进本函数的判定（段落数这一层才不会被首尾
    换行搅混）。
    """
    scanned_source = scan(source)
    scanned_translation = scan(translation)
    failures = [
        failure
        for failure in (
            _check_placeholders(source, translation),
            _check_control_sequences(scanned_source, scanned_translation),
            _check_braces_and_math(scanned_source, scanned_translation),
            _check_paragraph_count(source, translation),
        )
        if failure is not None
    ]
    return ValidationResult(failures=tuple(failures))


# ------------------------------------------------------------------ 四层


def _check_placeholders(source: str, translation: str) -> Failure | None:
    """placeholder 的 multiset 相等，另加译文的残缺自检。"""
    expected = Counter(match.group(0) for match in masking.TOKEN_RE.finditer(source))
    actual = Counter(match.group(0) for match in masking.TOKEN_RE.finditer(translation))
    if expected != actual:
        return Failure(check=CHECK_PLACEHOLDERS, message=_describe_multiset(expected, actual))
    complete = sum(actual.values())
    opens = translation.count(masking.SENTINEL_OPEN)
    closes = translation.count(masking.SENTINEL_CLOSE)
    if opens != complete or closes != complete:
        return Failure(
            check=CHECK_PLACEHOLDERS,
            message=(
                f"译文有 {complete} 个完整 placeholder，却出现 {opens} 个 {masking.SENTINEL_OPEN} 与 "
                f"{closes} 个 {masking.SENTINEL_CLOSE}：有残缺的 placeholder 碎片，"
                f"{masking.SENTINEL_OPEN} 与 {masking.SENTINEL_CLOSE} 只允许出现在完整 placeholder 里"
            ),
        )
    return None


def _check_control_sequences(source: Scan, translation: Scan) -> Failure | None:
    """控制序列的 multiset 相等。"""
    expected = Counter(source.control_sequences)
    actual = Counter(translation.control_sequences)
    if expected == actual:
        return None
    return Failure(check=CHECK_CONTROL_SEQUENCES, message=_describe_multiset(expected, actual))


def _check_braces_and_math(source: Scan, translation: Scan) -> Failure | None:
    """未转义的 `{`、`}`、`$`、`%` 计数分别相等。

    `%` 与前三个一同数：它是 LaTeX 的注释符，译文里多一个未转义的 `%` 会把那一行剩下的内容
    连同它后面的命令一起注释掉，编译不报错、正文安静地少一截。掩码阶段已把注释全换成
    placeholder，所以原文侧这个计数恒为 0，译文侧也必须是 0。
    """
    expected = source.counts
    actual = translation.counts
    differing = [name for name in COUNTED_CHARACTERS if expected[name] != actual[name]]
    if not differing:
        return None
    listed = "；".join(f"未转义的 {name} 原文 {expected[name]} 个、译文 {actual[name]} 个" for name in differing)
    return Failure(check=CHECK_BRACES_AND_MATH, message=listed)


def _check_paragraph_count(source: str, translation: str) -> Failure | None:
    """含可译文本的段落数相等。"""
    expected = translatable_paragraphs(source)
    actual = translatable_paragraphs(translation)
    if expected == actual:
        return None
    return Failure(
        check=CHECK_PARAGRAPH_COUNT,
        message=(
            f"含可译文本的段落数：原文 {expected} 段、译文 {actual} 段。空行是段落边界，不合并、不拆分、不跳过、不新增"
        ),
    )


# ------------------------------------------------------------------ 词法


def scan(text: str) -> Scan:
    r"""线性扫一遍文本，同时取出控制序列清单与未转义的 `{`、`}`、`$`、`%` 计数。

    两层各扫一遍会把同一段文本走两次，也会让「什么算转义」有两处定义，故一次扫描出两样
    结果：转义形态（`\{`、`\$`）在这一遍里被当成控制序列读走，因而不进计数。控制序列的读取
    复用 `masking.read_control_sequence`，与 mask、chunk 两层走同一条词法规则。
    """
    sequences: list[str] = []
    counts = dict.fromkeys(COUNTED_CHARACTERS, 0)
    position = 0
    length = len(text)
    while position < length:
        character = text[position]
        if character == "\\":
            name, after = masking.read_control_sequence(text, position)
            if text[after : after + 1] == "*" and name.isalpha():
                name, after = name + "*", after + 1
            sequences.append(name)
            position = after
            continue
        if character in counts:
            counts[character] += 1
        position += 1
    return Scan(control_sequences=tuple(sequences), counts=counts)


def translatable_paragraphs(text: str) -> int:
    r"""含可译文本的段落数：按空行切段，逐段判定剥除结构标记后是否还有非空白字符。

    与 `chunking.count_paragraphs` 的口径不同：那个数的是全部非空段落（chunk manifest 的
    `paragraphs` 字段与「每个 chunk 段落数至少 1」的出口判据按它算），本函数数的是需要译文
    保持一一对应的段落。两个口径的定义与实测依据见 docs/stages/chunk.md 段落计数的两个
    口径节。
    """
    return sum(1 for paragraph in chunking.paragraphs(text) if _has_translatable_text(paragraph))


def _has_translatable_text(paragraph: str) -> bool:
    r"""一段剥除四类结构标记之后是否还有非空白字符。

    四类依次是：placeholder、`\begin{X}` / `\end{X}` 整体、`NON_TEXT_ARGUMENT_COMMANDS` 里
    那些命令连同它们的参数、其余控制序列的命令名（参数保留）。第三类必须排在第四类之前，
    否则命令名先被剥掉，只剩下参数里的 `{-0.4in}` 被当成正文。
    """
    stripped = masking.TOKEN_RE.sub("", paragraph)
    stripped = ENVIRONMENT_DELIMITER_RE.sub("", stripped)
    stripped = NON_TEXT_COMMAND_RE.sub("", stripped)
    stripped = CONTROL_SEQUENCE_NAME_RE.sub("", stripped)
    return bool(stripped.strip())


# ------------------------------------------------------------------ 差异说明


def _describe_multiset(expected: Counter[str], actual: Counter[str]) -> str:
    """两个 multiset 的差异说明：译文少了什么、多了什么，各逐项列出。

    说明里不重复层名（缺的是 placeholder 还是控制序列），调用处已经把层名写在前面了。
    """
    missing = expected - actual
    extra = actual - expected
    parts = []
    if missing:
        parts.append(f"译文缺 {_describe_counter(missing)}")
    if extra:
        parts.append(f"译文多出 {_describe_counter(extra)}")
    return "；".join(parts)


def _describe_counter(counter: Counter[str]) -> str:
    """一个 multiset 的人读列举，超过上限的以「等 N 项」收尾。"""
    items = sorted(counter.items())
    listed = "、".join(f"{item}" + (f"×{count}" if count > 1 else "") for item, count in items[:DIFFERENCE_ITEMS_MAX])
    if len(items) > DIFFERENCE_ITEMS_MAX:
        return f"{listed} 等 {len(items)} 项"
    return listed
