r"""掩码往返恒等：`unmask(mask(x)) == x`。

这条恒等是 docs/stages/mask.md 的出口判据之一，判定实现 `masking.verify_roundtrip` 已在
生产路径上（每篇论文跑 mask 都执行）。本模块不新增判定，只换输入来源：hypothesis 生成的
随机输入打词法状态机的各条分支，三篇自造论文的源码文件作固定输入。

掩码丢字符不会当场报错——译文照常回填、照常编译通过、PDF 照常产出，只是少一段文本，
没有下游环节会报告。这是本层用例存在的理由。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tongtu import masking

from ..conftest import FIXTURE_PAPERS, tex_sources

# ---------------------------------------------------------------- 输入片段词表

#: 纯文本段落：掩码文本里原样保留的部分。
PLAIN_FRAGMENTS = [
    "This is a sentence.",
    "第二段文本，含中文标点。",
    "A line with numbers 1234 and symbols &~#.",
    "",
]

#: 注释：整行摘出成 block，行尾换行归属关系是往返恒等的常见失败点。
COMMENT_FRAGMENTS = [
    "% a trailing comment",
    "%% doubled percent",
    r"text before % comment after",
    r"escaped \% percent stays inline",
]

#: 行内与行间公式：分隔符成对识别，`$$` 与 `\[` 两种写法都要走到。
MATH_FRAGMENTS = [
    "inline $x^2 + y^2 = z^2$ math",
    r"inline \(a \neq b\) math",
    r"\[ E = mc^2 \]",
    "$$ \\sum_{i=1}^{n} i $$",
]

#: 分类表内的 non-translatable environment：整块摘出。
KNOWN_ENVIRONMENT_FRAGMENTS = [
    r"\begin{equation}E = mc^2\label{eq:mass}\end{equation}",
    "\\begin{align}a &= b \\\\ c &= d\\end{align}",
    r"\begin{tabular}{cc}1 & 2 \\ 3 & 4\end{tabular}",
    r"\begin{tikzpicture}\draw (0,0) -- (1,1);\end{tikzpicture}",
]

#: 分类表外的环境：走保守整体掩码这条默认路径。
UNKNOWN_ENVIRONMENT_FRAGMENTS = [
    r"\begin{widetext}wide content here\end{widetext}",
    r"\begin{sidenote}a note aside\end{sidenote}",
]

#: 逐字环境与 `\verb`：内部的 `$`、`%`、反斜杠都不按 TeX 语义解释。
VERBATIM_FRAGMENTS = [
    "\\begin{verbatim}raw $ % \\ text\\end{verbatim}",
    r"\begin{lstlisting}for i in range(3): print(i)\end{lstlisting}",
    r"inline \verb|x % $ y| verbatim",
]

#: 带 caption 槽位的浮动体：caption 必选参数被抽成 CAP 槽位，可选参数不参与。
CAPTION_FRAGMENTS = [
    r"\begin{figure}\includegraphics{a.png}\caption{A figure caption}\label{fig:a}\end{figure}",
    r"\begin{table}\caption[short]{A table caption}\begin{tabular}{c}1\end{tabular}\end{table}",
    r"\begin{figure*}\includegraphics{b.pdf}\caption{Spanning caption}\end{figure*}",
]

#: 文本环境：留在掩码文本里，内部可能含空行，分段器带环境深度计数。
TEXT_ENVIRONMENT_FRAGMENTS = [
    r"\begin{itemize}\item one \item two\end{itemize}",
    "\\begin{quote}quoted text\n\nwith a blank line\\end{quote}",
]

#: 控制序列与声明：`\newtheorem` / `\newenvironment` 驱动分类结论下沉。
COMMAND_FRAGMENTS = [
    r"\section{A Section}",
    r"\textbf{bold} and \emph{emphasis}",
    r"\newtheorem{thm}{Theorem}",
    r"\newenvironment{takeaway}{\begin{quote}}{\end{quote}}",
    r"\ref{eq:mass} and \cite{someone2020}",
]

ALL_FRAGMENTS = (
    PLAIN_FRAGMENTS
    + COMMENT_FRAGMENTS
    + MATH_FRAGMENTS
    + KNOWN_ENVIRONMENT_FRAGMENTS
    + UNKNOWN_ENVIRONMENT_FRAGMENTS
    + VERBATIM_FRAGMENTS
    + CAPTION_FRAGMENTS
    + TEXT_ENVIRONMENT_FRAGMENTS
    + COMMAND_FRAGMENTS
)


@st.composite
def latex_bodies(draw: st.DrawFn) -> str:
    """由片段词表拼出一段类 LaTeX 正文，段落之间以换行或空行相接。"""
    fragments = draw(st.lists(st.sampled_from(ALL_FRAGMENTS), min_size=0, max_size=12))
    separators = draw(
        st.lists(st.sampled_from(["\n", "\n\n", "\n \n"]), min_size=len(fragments), max_size=len(fragments))
    )
    return "".join(fragment + separator for fragment, separator in zip(fragments, separators, strict=True))


@st.composite
def latex_documents(draw: st.DrawFn) -> str:
    r"""把正文包进最小文档骨架。

    骨架不是可选的：`mask_document` 把文件头到注释外首个 `\begin{document}` 整体作前导区
    成块，找不到它即结构错误。掩码的输入在流水线里是 precompile.tex，那本就是完整文档。
    """
    body = draw(latex_bodies())
    documentclass = draw(st.sampled_from(["article", "revtex4-2", "IEEEtran"]))
    title = draw(st.sampled_from([r"\title{A Title}", r"\title{标题}", ""]))
    abstract = draw(st.sampled_from([r"\begin{abstract}An abstract.\end{abstract}", ""]))
    return f"\\documentclass{{{documentclass}}}\n{title}\n{abstract}\n\\begin{{document}}\n{body}\\end{{document}}\n"


#: 哨兵字符出现在原文里属于掩码无法处理的输入，由 `mask_document` 抛 `MaskError`
#: 拦下（见「哨兵冲突」用例），随机文本策略因此排除它们。
SENTINEL_CHARACTERS = (masking.SENTINEL_OPEN, masking.SENTINEL_CLOSE)


def assert_roundtrip(source: str, table) -> masking.MaskOutcome:
    """执行掩码与往返自检，返回掩码结果供调用方追加结构断言。"""
    outcome = masking.mask_document(source, table)
    masking.verify_roundtrip(source, outcome)
    return outcome


# ---------------------------------------------------------------- 随机输入


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(source=latex_documents())
def test_roundtrip_on_generated_documents(source: str, environments_table) -> None:
    """片段拼出的类 LaTeX 文档：掩码后 unmask 逐字符还原。"""
    assert_roundtrip(source, environments_table)


#: 刁钻但结构完好的正文样本，作为随机策略之外的确定性覆盖：每一种都要走完掩码与还原。
AWKWARD_BODIES: tuple[str, ...] = (
    "",
    "   ",
    "\n\n\n",
    "100% pure",
    r"\% escaped percent",
    "$x + y$ paired dollar",
    "a \\ backslash",
    "{unbalanced brace",
    "}closing first{",
    "中文正文与全角标点。",
    "emoji 🙂 与组合字符 e\u0301",
    "tab\tand\rcarriage",
    r"\cmd*[opt]{arg}",
    "%\n%\n% three comment lines",
)


#: 正文里可能未闭合、从而让掩码合法地判结构错误的标志：环境起止、逐字命令与数学分隔符。
#: 掩码对不含这些标志的正文报错即是缺陷，`test_roundtrip_on_arbitrary_body` 据此判定。
UNCLOSABLE_MARKERS: tuple[str, ...] = ("\\begin", "\\end", "\\verb", "$", "\\[", "\\]", "\\(", "\\)")


def wrap_in_skeleton(body: str) -> str:
    r"""把一段正文包进最小文档骨架，使它成为 `mask_document` 接受的完整文档。"""
    return f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"


@pytest.mark.parametrize(
    "body",
    ["$unclosed dollar", r"\[ unclosed display", r"\begin{equation}unclosed"],
    ids=["dollar", "display", "environment"],
)
def test_unclosed_structures_are_rejected(body: str, environments_table) -> None:
    """正文里的未闭合数学分隔符与环境判为结构错误，不静默产出对不上的掩码文本。"""
    with pytest.raises(masking.MaskError):
        masking.mask_document(wrap_in_skeleton(body), environments_table)


@pytest.mark.parametrize("body", [r"\verb|unclosed", r"\verb*|unclosed", r"text \verb|a| and \verb|b"], ids=range(3))
def test_unclosed_verb_falls_back_to_line_end(body: str, environments_table) -> None:
    r"""`\verb` 的定界符不在同一行内闭合时按行尾截断，不判失败——往返仍须恒等。

    这是 `_skip_verb` 的有意容错（见其文档字符串），与未闭合的数学分隔符、环境不同类：
    那两者判结构错误，这一条继续处理。
    """
    assert_roundtrip(wrap_in_skeleton(body), environments_table)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=st.text(max_size=300).filter(lambda text: not any(ch in text for ch in SENTINEL_CHARACTERS)))
def test_roundtrip_on_arbitrary_body(body: str, environments_table) -> None:
    r"""任意字符序列作正文：掩码不得丢字符。

    正文包进骨架才送进掩码——裸文本没有前导区，`mask_document` 一律判结构错误，那一条由
    `test_bare_text_is_rejected` 单独判定。掩码仍可能因正文里的未闭合结构判失败，那是合法
    结果；但正文里没有任何可能未闭合的结构却失败，就是缺陷。
    """
    source = wrap_in_skeleton(body)
    try:
        outcome = masking.mask_document(source, environments_table)
    except masking.MaskError:
        assert any(marker in body for marker in UNCLOSABLE_MARKERS), (
            f"正文里没有可能未闭合的结构，掩码却判失败：{body!r}"
        )
        return
    masking.verify_roundtrip(source, outcome)


@pytest.mark.parametrize("body", AWKWARD_BODIES, ids=lambda value: repr(value)[:24])
def test_roundtrip_on_awkward_bodies(body: str, environments_table) -> None:
    """刁钻正文的确定性覆盖：随机策略未必稳定生成这些形态，这里逐个钉住。"""
    assert_roundtrip(wrap_in_skeleton(body), environments_table)


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(text=st.text(max_size=200).filter(lambda value: r"\begin{document}" not in value))
def test_bare_text_is_rejected(text: str, environments_table) -> None:
    r"""没有 `\begin{document}` 的输入一律判结构错误，不静默产出残缺的掩码文本。

    驱动器把这个异常转成 mask_failed 状态。此前这条行为隐含在「捕获后返回」里，等于没有
    判定：实测 200 个随机样例全部走该分支，往返自检一次也没执行到。
    """
    stripped = "".join(ch for ch in text if ch not in SENTINEL_CHARACTERS)
    with pytest.raises(masking.MaskError):
        masking.mask_document(stripped, environments_table)


# ---------------------------------------------------------------- 固定输入


@pytest.mark.parametrize("paper", FIXTURE_PAPERS)
def test_roundtrip_on_fixture_papers(paper: str, environments_table) -> None:
    r"""三篇自造论文的完整文档源码掩码后往返恒等。

    取的是源码树里含 `\begin{document}` 的文件（三篇均为 main.tex），不取 `\input` 进来的
    章节片段：前导区成块要求输入是完整文档。经 latexpand 展开后的完整源码由编译层覆盖，
    那里跑的是真实流水线，文本层因此不必依赖 latexpand。
    """
    sources = [path for path in tex_sources(paper) if r"\begin{document}" in path.read_text(encoding="utf-8")]
    assert sources, f"{paper} 没有含 \\begin{{document}} 的 .tex 文件"
    for path in sources:
        source = path.read_text(encoding="utf-8")
        outcome = assert_roundtrip(source, environments_table)
        assert_placeholders_consistent(outcome, path)


def assert_placeholders_consistent(outcome: masking.MaskOutcome, path: Path) -> None:
    """掩码文本里的 placeholder 与记录条数一一对应，不多不少。"""
    tokens = masking.TOKEN_RE.findall(outcome.masked)
    block_tokens = [kind for kind, _ in tokens if kind == masking.BLOCK_ID_PREFIX]
    assert len(block_tokens) == len(outcome.blocks), f"{path}：block placeholder 数与记录条数不符"
    assert outcome.masked.count(masking.SENTINEL_OPEN) == len(tokens), f"{path}：掩码文本里有残缺的 placeholder"
    assert outcome.masked.count(masking.SENTINEL_CLOSE) == len(tokens), f"{path}：掩码文本里有残缺的 placeholder"


# ---------------------------------------------------------------- 哨兵冲突


@pytest.mark.parametrize("character", SENTINEL_CHARACTERS)
def test_sentinel_in_source_is_rejected(character: str, environments_table) -> None:
    """原文含哨兵字符时抛 `MaskError`：placeholder 无从与原文区分，不能静默放行。"""
    with pytest.raises(masking.MaskError):
        masking.mask_document(f"text with {character} inside", environments_table)
