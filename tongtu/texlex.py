"""LaTeX 词法原语：注释 / `\\verb` / 转义 / 环境配对 / 花括号配平（架构 §13 选型）。

mask 与 unmask 共用。**只做词法、不做语义**——不展开宏、不求值 `\\if`、不理解包，
只回答「这些字节属于哪个词法结构」。掩码需要的正是且仅是这一层判断：

* 哪些 `%` 开启注释（`\\%` 不是，verbatim 里的 `%` 也不是）；
* 哪些 `\\begin{X}` 是真环境（`\\verb|\\begin{X}|` 与 lstlisting 体内的不是）；
* `\\end{X}` 与哪个 `\\begin{X}` 配对（同名嵌套要计数）；
* 花括号参数在哪里结束（注释与 `\\verb` 里的括号不计入配平）。

正则做不到这些（LaTeX 不是正则语言，架构 §3.1 第 1 条），故实现为可复用的
`Lexer` 状态机：它按需吐出「有词法意义的位置」，调用方自己处理 token 之间的纯文本
（gap），并可随时改写 `Lexer.pos` 跳过一整段（掩码块就是这样被整块吞掉的）。
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Container, Iterator

__all__ = [
    "TexLexError",
    "Token",
    "Lexer",
    "BEGIN_RE",
    "END_RE",
    "ENV_NAME_RE",
    "skip_comment",
    "skip_verb",
    "find_balanced",
    "find_bracket_arg",
    "find_env_end",
    "read_group",
    "skip_optionals",
    "skip_spaces",
    "strip_comments_inline",
    "line_starts",
    "line_number",
]


class TexLexError(ValueError):
    """词法结构不成立（未闭合环境、不配平花括号等）。

    mask 捕获它并降级为「不掩码这一段 + 记警告」，绝不因此损坏源码（架构 §3.1 第 2 条）。
    """


#: 环境名：字母/`@`/数字，可带一个星号后缀（`align*`、`figure*`、`algorithm2e`）。
ENV_NAME_RE = r"[A-Za-z@][A-Za-z@0-9]*\*?"

BEGIN_RE = re.compile(r"\\begin\s*\{(" + ENV_NAME_RE + r")\}")
END_RE = re.compile(r"\\end\s*\{(" + ENV_NAME_RE + r")\}")

#: 控制序列：`\` + 连续字母（控制字）或 `\` + 单个非字母（控制符，含 `\%` `\\` `\{`）。
CS_RE = re.compile(r"\\(?:[A-Za-z@]+|.|\Z)", re.DOTALL)

#: `\verb` / `\verb*` + 一个非字母非空白的定界符。定界符不能是字母——否则
#: `\verbatiminput{f}` 会被当成 `\verb` 且以 `a` 为定界符（v2 的真实缺陷）。
VERB_RE = re.compile(r"\\verb(\*?)([^A-Za-z\s])")


def skip_comment(s: str, i: int) -> int:
    """`s[i]` 是未转义的 `%`，返回注释的结束位置（该行 `\\n` 的下标，或文件尾）。

    注意返回值**不含**换行符本身——调用方按需决定换行归谁。
    """
    j = s.find("\n", i)
    return len(s) if j == -1 else j


def skip_verb(s: str, i: int) -> int | None:
    """`s[i]` 是 `\\`，若此处是完整的 `\\verb<d>...<d>` 则返回其结束位置，否则 None。

    `\\verb` 的内容不能跨行（TeX 如此规定），定界符必须是非字母；两条都不满足时返回
    None，交由调用方按普通控制序列处理。
    """
    m = VERB_RE.match(s, i)
    if m is None:
        return None
    delim = m.group(2)
    close = s.find(delim, m.end())
    if close == -1:
        return None
    newline = s.find("\n", m.end())
    if newline != -1 and close > newline:
        return None
    return close + 1


def find_balanced(s: str, i: int) -> int:
    """`s[i]` 是 `{`，返回配对 `}` 的下标。注释、`\\verb`、转义括号都不计入配平。"""
    if i >= len(s) or s[i] != "{":
        raise TexLexError(f"位置 {i} 不是 '{{'")
    depth = 0
    j = i
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            k = skip_verb(s, j)
            j = k if k is not None else min(j + 2, n)
            continue
        if c == "%":
            j = skip_comment(s, j)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise TexLexError(f"花括号不配平：起于位置 {i}")


def skip_spaces(s: str, i: int) -> int:
    """跳过空白（含换行），返回第一个非空白字符的下标。"""
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def skip_optionals(s: str, i: int) -> int:
    """跳过零个或多个可选参数 `[...]`（含其间空白）；不配平则原位返回。"""
    while True:
        j = skip_spaces(s, i)
        if j < len(s) and s[j] == "[":
            try:
                i = find_bracket_arg(s, j) + 1
            except TexLexError:
                return i
        else:
            return i


def read_group(s: str, i: int) -> tuple[str, int] | None:
    """读一个必选参数 `{...}`，返回 `(内容, 之后的位置)`；不是 `{` 或不配平则 None。"""
    j = skip_spaces(s, i)
    if j >= len(s) or s[j] != "{":
        return None
    try:
        close = find_balanced(s, j)
    except TexLexError:
        return None
    return s[j + 1 : close], close + 1


def find_bracket_arg(s: str, i: int) -> int:
    """`s[i]` 是 `[`，返回配对 `]` 的下标。

    与 `find_balanced` 同样跳过注释 / `\\verb` / 转义；另外把 `{...}` 组整体跳过，
    使 `\\caption[Short {a]b}]{...}` 这类嵌套写法不会提前收尾（v2 的 `[^\\]]*` 会）。
    """
    if i >= len(s) or s[i] != "[":
        raise TexLexError(f"位置 {i} 不是 '['")
    depth = 0
    j = i
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            k = skip_verb(s, j)
            j = k if k is not None else min(j + 2, n)
            continue
        if c == "%":
            j = skip_comment(s, j)
            continue
        if c == "{":
            j = find_balanced(s, j) + 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise TexLexError(f"方括号不配平：起于位置 {i}")


@dataclass(frozen=True)
class Token:
    """一个有词法意义的位置。

    `kind`：

    * `comment`  —— `%` 起至行尾（不含换行）；
    * `verb`     —— 完整的 `\\verb<d>...<d>`；
    * `begin`    —— `\\begin{name}`；`env_end` 非 None 表示这是 verbatim 类环境且
      词法器已经把整个环境体（至 `\\end{name}` 之后）跳过；
    * `end`      —— `\\end{name}`；
    * `control`  —— 其余控制序列（`\\alpha`、`\\%`、`\\\\`）。

    `start`/`end` 是本 token 自身的半开区间；token 之间的字节是纯文本，由调用方处理。
    """

    kind: str
    start: int
    end: int
    name: str | None = None
    env_end: int | None = None


class Lexer:
    """可重入的词法游标：`for tok in lexer` 逐个吐出 token，中途可改写 `pos` 跳段。

    verbatim 类环境（`verbatim` / `lstlisting` / `minted` …）的**环境体不参与词法**：
    体内的 `%`、`\\begin{figure}`、不配平的花括号都只是字符。词法器遇到这类
    `\\begin` 会直接把整个环境跳过并在 token 上带出 `env_end`；找不到 `\\end` 时退化为
    普通 begin（`env_end is None`），由调用方决定如何降级。
    """

    def __init__(
        self,
        s: str,
        *,
        verbatim_envs: Container[str] = frozenset(),
        pos: int = 0,
        stop: int | None = None,
    ) -> None:
        self.s = s
        self.verbatim_envs = verbatim_envs
        self.pos = pos
        self.stop = len(s) if stop is None else stop

    def __iter__(self) -> Iterator[Token]:
        return self

    def __next__(self) -> Token:
        tok = self.next()
        if tok is None:
            raise StopIteration
        return tok

    def next(self) -> Token | None:
        """返回下一个 token 并把 `pos` 推进到它之后；到达终点返回 None。"""
        s, stop = self.s, self.stop
        i = self.pos
        while i < stop:
            c = s[i]
            if c == "%":
                end = min(skip_comment(s, i), stop)
                self.pos = end
                return Token("comment", i, end)
            if c == "\\":
                verb = skip_verb(s, i)
                if verb is not None and verb <= stop:
                    self.pos = verb
                    return Token("verb", i, verb)
                mb = BEGIN_RE.match(s, i)
                if mb is not None and mb.end() <= stop:
                    name = mb.group(1)
                    env_end = None
                    if name in self.verbatim_envs:
                        env_end = _search_env_end(s, mb.end(), name, stop)
                    self.pos = env_end if env_end is not None else mb.end()
                    return Token("begin", i, mb.end(), name=name, env_end=env_end)
                me = END_RE.match(s, i)
                if me is not None and me.end() <= stop:
                    self.pos = me.end()
                    return Token("end", i, me.end(), name=me.group(1))
                mc = CS_RE.match(s, i)
                end = min(mc.end() if mc else i + 1, stop)
                end = max(end, i + 1)
                self.pos = end
                return Token("control", i, end)
            i += 1
        self.pos = stop
        return None


def _search_env_end(s: str, frm: int, name: str, stop: int) -> int | None:
    """在 [frm, stop) 内找 `\\end{name}`（verbatim 体：纯字符串搜索），返回其结束位置。"""
    pat = re.compile(r"\\end\s*\{" + re.escape(name) + r"\}")
    m = pat.search(s, frm, stop)
    return m.end() if m else None


def find_env_end(
    s: str,
    start: int,
    name: str,
    verbatim_envs: Container[str] = frozenset(),
) -> int:
    """`start` 指向 `\\begin{name}`，返回配对 `\\end{name}` 之后的位置。

    同名嵌套计数；注释、`\\verb`、verbatim 子环境体内的 `\\end{name}` 一律不算数。
    """
    m = BEGIN_RE.match(s, start)
    if m is None or m.group(1) != name:
        raise TexLexError(f"位置 {start} 不是 \\begin{{{name}}}")
    if name in verbatim_envs:
        end = _search_env_end(s, m.end(), name, len(s))
        if end is None:
            raise TexLexError(f"未闭合环境 {name}（起于位置 {start}）")
        return end
    depth = 1
    lexer = Lexer(s, verbatim_envs=verbatim_envs, pos=m.end())
    for tok in lexer:
        if tok.kind == "begin" and tok.name == name:
            depth += 1
        elif tok.kind == "end" and tok.name == name:
            depth -= 1
            if depth == 0:
                return tok.end
    raise TexLexError(f"未闭合环境 {name}（起于位置 {start}）")


def strip_comments_inline(text: str, verbatim_envs: Container[str] = frozenset()) -> str:
    """剥注释 + 折叠空白，用于 caption/title 在掩码流里的**单行展示文本**。

    这是有损变换，只用于「给 LLM 看」的那一份；权威原文在 blocks.json 的
    `caption.text` 里逐字节保存，回填以原文为准（见 `stages/mask.py` 模块文档）。
    """
    out: list[str] = []
    pos = 0
    lexer = Lexer(text, verbatim_envs=verbatim_envs)
    for tok in lexer:
        out.append(text[pos : tok.start])
        if tok.kind != "comment":
            out.append(text[tok.start : tok.end])
        pos = tok.end
    out.append(text[pos:])
    return re.sub(r"\s+", " ", "".join(out)).strip()


def line_starts(s: str) -> list[int]:
    """各行起始偏移（供 span 的 1-based 行号换算）。"""
    starts = [0]
    start = s.find("\n")
    while start != -1:
        starts.append(start + 1)
        start = s.find("\n", start + 1)
    return starts


def line_number(starts: list[int], offset: int) -> int:
    """字符偏移 → 1-based 行号。"""
    return bisect_right(starts, offset)
