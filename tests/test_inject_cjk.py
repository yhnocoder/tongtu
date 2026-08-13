"""inject_cjk：中文排版适配（架构 §3 compile 行、§10 字体探测链、决策 13）。

golden 文件是 `tests/data/inject_cjk/golden/<name>.injected.tex`，用
`python -m tests.regen_inject_cjk` 之类的一次性脚本重生成没有意义——注入块是排版口径，
改它必须人眼过一遍 diff，故 golden 由人工确认后入库。
"""

import pytest

from pathlib import Path

from tongtu.stages import inject_cjk as ij

DATA = Path(__file__).parent / "data" / "inject_cjk"
GOLDEN = DATA / "golden"

#: 全部 fixture（含两个「原样通过」的），幂等性质测试跑遍。
FIXTURES = ("article_basic", "revtex", "cjkutf8", "existing_xecjk", "existing_ctex")


def load(name: str) -> str:
    return (DATA / f"{name}.tex").read_text(encoding="utf-8")


def golden(name: str) -> str:
    return (GOLDEN / f"{name}.injected.tex").read_text(encoding="utf-8")


def wrap(preamble: str, body: str = "x") -> str:
    return f"{preamble}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"


# --------------------------------------------------------------------------- #
# 三分支 golden
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,branch,documentclass",
    [
        ("article_basic", "inject", "article"),
        ("revtex", "inject", "revtex4-2"),
        ("cjkutf8", "replace", "article"),
    ],
)
def test_golden_injected(name, branch, documentclass):
    result = ij.inject(load(name))
    assert result.branch == branch
    assert result.documentclass == documentclass
    assert result.engine == "xelatex"
    assert result.warnings == ()
    assert result.text == golden(name)


@pytest.mark.parametrize("name", ["existing_xecjk", "existing_ctex"])
def test_passthrough_branch(name):
    src = load(name)
    result = ij.inject(src)
    assert result.branch == "passthrough"
    assert result.text == src
    assert result.changed is False
    assert result.engine == "xelatex"


def test_engine_is_constant():
    assert ij.ENGINE == "xelatex"
    assert all(ij.inject(load(n)).engine == "xelatex" for n in FIXTURES)


# --------------------------------------------------------------------------- #
# 注入块内容（与 v2 等效：字体、探测链、断行、行距）
# --------------------------------------------------------------------------- #


def test_block_contents_match_v2():
    text = ij.inject(load("article_basic")).text
    for needle in (
        r"\usepackage{xeCJK}",
        "Path = {fonts/}",
        "BoldFont = LXGWWenKai-Medium.ttf",
        "]{LXGWWenKai-Light.ttf}",
        r"\setCJKmonofont[Path={fonts/}]{LXGWWenKai-Light.ttf}",
        '\\XeTeXlinebreaklocale "zh"',
        r"\XeTeXlinebreakskip = 0pt plus 1pt",
        r"\linespread{1.4}",
    ):
        assert needle in text, needle


def test_sans_font_probe_chain_order():
    """无衬线探测链：Hiragino → Noto Sans CJK SC → 霞鹜文楷兜底（架构 §10）。"""
    text = ij.inject(load("article_basic")).text
    hiragino = text.index(r"\IfFontExistsTF{Hiragino Sans GB}")
    noto = text.index(r"\IfFontExistsTF{Noto Sans CJK SC}")
    fallback = text.index(r"{\setCJKsansfont[Path={fonts/}")
    assert hiragino < noto < fallback


def test_font_paths_are_relative():
    """字体必须走相对路径——绝对路径的 zh.tex 出了产物包就编译不了（架构 §10）。"""
    text = ij.inject(load("article_basic")).text
    assert "Path = {fonts/}" in text
    assert "/home/" not in text and "Path = {/" not in text


# --------------------------------------------------------------------------- #
# 审计 1：注释 / verbatim 里的 \documentclass 与 \usepackage 不算数
# --------------------------------------------------------------------------- #


def test_commented_documentclass_not_injected_into():
    src = load("article_basic")
    out = ij.inject(src).text
    first_line = src.splitlines()[0]
    assert first_line.startswith("% \\documentclass{ctexart}")
    assert out.splitlines()[0] == first_line  # 注释行逐字节原样
    assert out.index(ij.BEGIN_MARK) > out.index("\\documentclass[11pt")


def test_find_documentclass_skips_comment():
    dc = ij.find_documentclass(load("article_basic"))
    assert dc is not None
    assert dc.name == "article"
    assert dc.options == ("11pt", "twocolumn")


def test_verbatim_usepackage_does_not_trigger_passthrough():
    """正文 verbatim 里贴的 `\\usepackage{xeCJK}` 不是加载（v2 的正则会中招）。"""
    result = ij.inject(load("article_basic"))
    assert result.branch == "inject"
    assert r"\begin{verbatim}" in result.text


def test_missing_documentclass_raises():
    src = "% \\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n"
    with pytest.raises(ij.InjectError):
        ij.inject(src)


# --------------------------------------------------------------------------- #
# 审计 3：ctex 判定是精确匹配，不是子串
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cls,branch",
    [
        ("ctexart", "passthrough"),
        ("ctexrep", "passthrough"),
        ("ctexbook", "passthrough"),
        ("ctexbeamer", "passthrough"),
        ("ctexart-plus", "inject"),
        ("myctexart", "inject"),
        ("article", "inject"),
    ],
)
def test_ctex_class_match_is_exact(cls, branch):
    assert ij.inject(wrap(f"\\documentclass{{{cls}}}")).branch == branch


def test_ctexart_in_prose_does_not_block_injection():
    src = wrap("\\documentclass{article}", "We compare ctexart with ctexrep in prose.")
    assert ij.inject(src).branch == "inject"


# --------------------------------------------------------------------------- #
# CJKutf8 分支：无残留、反斜杠不损坏
# --------------------------------------------------------------------------- #


def test_cjkutf8_package_removed():
    result = ij.inject(load("cjkutf8"))
    assert result.removed_packages == ("CJKutf8",)
    assert "CJKutf8" not in result.text
    assert r"\usepackage{graphicx}" in result.text  # 其余包不受牵连


def test_cjkutf8_no_cjk_environment_residue():
    """正文的 CJK\\* 包裹剥干净；verbatim 里展示的那一对原样留着。"""
    result = ij.inject(load("cjkutf8"))
    assert result.stripped_environments == ("CJK*",)
    text = result.text
    assert text.count(r"\begin{CJK*}") == 1
    assert text.count(r"\end{CJK*}") == 1
    verb_start = text.index(r"\begin{verbatim}")
    verb_end = text.index(r"\end{verbatim}")
    assert verb_start < text.index(r"\begin{CJK*}") < verb_end
    assert verb_start < text.index(r"\end{CJK*}") < verb_end


def test_cjkutf8_backslashes_survive():
    """v2 用 `BLOCK.replace("\\\\", "\\\\\\\\")` 喂 re.sub 模板；本实现纯切片，不解释转义。"""
    text = ij.inject(load("cjkutf8")).text
    assert r"\newcommand{\g}[1]{\mathrm{#1}}" in text
    assert "中文段落，含 \\\\ 强制换行、行内公式 $\\g{x}\\backslash y$ 与 \\emph{强调}。" in text


def test_comma_list_package_removal():
    src = wrap("\\documentclass{article}\n\\usepackage{CJKutf8,graphicx}")
    result = ij.inject(src)
    assert result.branch == "replace"
    assert r"\usepackage{graphicx}" in result.text
    assert "CJKutf8" not in result.text


def test_comma_list_removal_of_trailing_name():
    """删逗号列表里最后一个名字不能吃掉 `}`（逐名删相邻逗号的写法会）。"""
    src = wrap("\\documentclass{article}\n\\usepackage{graphicx,CJK,CJKutf8}")
    result = ij.inject(src)
    assert r"\usepackage{graphicx}" in result.text
    assert "CJK" not in result.text.replace(ij.XECJK_BODY, "")


def test_requirepackage_is_seen():
    src = wrap("\\documentclass{article}\n\\RequirePackage{xeCJK}")
    assert ij.inject(src).branch == "passthrough"


def test_inline_comments_inside_arguments():
    """参数里的行内注释：包名与文档类选项都要先剥注释再切逗号。"""
    src = wrap("\\documentclass[\n  11pt, % 字号\n  a4paper\n]{article}\n\\usepackage{graphicx, % 图\n  xeCJK}")
    dc = ij.find_documentclass(src)
    assert dc is not None and dc.options == ("11pt", "a4paper")
    assert [u.name for u in ij.preamble_packages(src)] == ["graphicx", "xeCJK"]
    assert ij.inject(src).branch == "passthrough"


def test_preamble_packages_expands_comma_list():
    uses = ij.preamble_packages("\\usepackage[utf8]{inputenc}\n\\usepackage{amsmath, graphicx}\n")
    assert [u.name for u in uses] == ["inputenc", "amsmath", "graphicx"]
    assert uses[1].siblings == ("amsmath", "graphicx")
    assert uses[1].cs == "usepackage"


# --------------------------------------------------------------------------- #
# 适配表
# --------------------------------------------------------------------------- #


def test_shipped_table_loads():
    table = ij.load_adaptation_table()
    assert table.version >= 1
    # 表为空（或只有实测条目）都行，主逻辑不依赖它。
    assert isinstance(table.adaptations, tuple)


def test_shipped_examples_are_valid_entries():
    """数据文件 examples 段是给关节⑥抄的模板，必须能解析——否则模板会烂掉。"""
    names = [a.name for a in ij.load_adaptation_table().examples]
    assert "cjkutf8-to-xecjk" in names
    assert "revtex-inject-late" in names


def test_adaptation_entry_applies():
    """适配表条目生效一例：revtex 模板把注入推到 `\\begin{document}` 之前并加补丁。"""
    entry = next(
        a for a in ij.load_adaptation_table().examples if a.name == "revtex-inject-late"
    )
    table = ij.AdaptationTable(adaptations=(entry,))
    result = ij.inject(load("revtex"), adaptation=table)
    assert result.adaptations == ("revtex-inject-late",)
    assert result.position == "before_begin_document"
    assert result.text.index(ij.BEGIN_MARK) < result.text.index("\\begin{document}")
    assert result.text.index(r"\linespread{1.4}") < result.text.index(r"\linespread{1.3}")
    assert "% [adaptation: revtex-inject-late]" in result.text
    assert result.text == golden("revtex_adapted")


def test_adaptation_does_not_match_other_class():
    entry = next(
        a for a in ij.load_adaptation_table().examples if a.name == "revtex-inject-late"
    )
    table = ij.AdaptationTable(adaptations=(entry,))
    result = ij.inject(load("article_basic"), adaptation=table)
    assert result.adaptations == ()
    assert result.position == "after_documentclass"


def test_empty_table_keeps_three_branches():
    """「表为空也不影响三分支主逻辑」——CJKutf8 分支是内建条目。"""
    empty = ij.AdaptationTable()
    assert ij.inject(load("cjkutf8"), adaptation=empty).text == golden("cjkutf8")
    assert ij.inject(load("article_basic"), adaptation=empty).text == golden("article_basic")


def test_table_entry_overrides_builtin_by_name():
    entry = ij.Adaptation(
        name="cjkutf8-to-xecjk",
        packages=frozenset({"CJKutf8"}),
        actions=(
            ij.Action(op="remove_package", packages=("CJKutf8",)),
            ij.Action(op="insert_at", position="before_begin_document"),
        ),
    )
    result = ij.inject(load("cjkutf8"), adaptation=ij.AdaptationTable(adaptations=(entry,)))
    assert result.adaptations == ("cjkutf8-to-xecjk",)
    assert result.position == "before_begin_document"
    assert result.stripped_environments == ()  # 内建的剥环境动作被整条覆盖


def test_duplicate_actions_across_entries_are_deduped():
    """多条条目做同一件事（删同一个包、剥同一个环境）不该炸成区间冲突。"""
    twin = ij.Adaptation(
        name="twin",
        packages=frozenset({"CJKutf8"}),
        actions=(
            ij.Action(op="remove_package", packages=("CJKutf8",)),
            ij.Action(op="strip_environment", environments=(ij.EnvStrip("CJK", 2, True),)),
        ),
    )
    result = ij.inject(load("cjkutf8"), adaptation=ij.AdaptationTable(adaptations=(twin,)))
    assert result.adaptations == ("cjkutf8-to-xecjk", "twin")  # 内建 + 表条目叠加
    assert result.text == golden("cjkutf8")


def test_patch_text_is_inserted_literally():
    """`preamble_patch` 由 agent 写进数据文件，绝不能被当成 re.sub 模板解释（审计 2）。"""
    tex = r"\newcommand{\gg}{\g<1> \1 \\ \n}"
    entry = ij.Adaptation(
        name="literal-patch",
        documentclasses=frozenset({"article"}),
        actions=(ij.Action(op="preamble_patch", tex=tex),),
    )
    text = ij.inject(load("article_basic"), adaptation=ij.AdaptationTable(adaptations=(entry,))).text
    assert tex in text


def test_after_package_position():
    entry = ij.Adaptation(
        name="after-hyperref",
        documentclasses=frozenset({"article"}),
        actions=(ij.Action(op="insert_at", position="after_package", package="graphicx"),),
    )
    src = wrap("\\documentclass{article}\n\\usepackage{graphicx}\n\\usepackage{amsmath}")
    text = ij.inject(src, adaptation=ij.AdaptationTable(adaptations=(entry,))).text
    assert text.index(r"\usepackage{graphicx}") < text.index(ij.BEGIN_MARK)
    assert text.index(ij.BEGIN_MARK) < text.index(r"\usepackage{amsmath}")


def test_unresolvable_position_falls_back_with_warning():
    entry = ij.Adaptation(
        name="after-missing",
        documentclasses=frozenset({"article"}),
        actions=(ij.Action(op="insert_at", position="after_package", package="nosuchpkg"),),
    )
    result = ij.inject(
        wrap("\\documentclass{article}"), adaptation=ij.AdaptationTable(adaptations=(entry,))
    )
    assert result.position == "after_documentclass"
    assert result.warnings and "nosuchpkg" in result.warnings[0]


@pytest.mark.parametrize(
    "raw",
    [
        {"adaptations": [{"name": "x", "actions": [{"op": "remove_package", "packages": ["a"]}]}]},
        {"adaptations": [{"documentclass": ["a"], "actions": []}]},
        {"adaptations": [{"name": "x", "documentclass": ["a"], "actions": [{"op": "nope"}]}]},
        {
            "adaptations": [
                {"name": "x", "documentclass": ["a"], "actions": [{"op": "insert_at"}]},
            ]
        },
        {
            "adaptations": [
                {
                    "name": "x",
                    "documentclass": ["a"],
                    "actions": [{"op": "insert_at", "position": "after_package"}],
                }
            ]
        },
        {"adaptations": [{"name": "x", "documentclass": ["a"], "actions": [{"op": "preamble_patch"}]}]},
        {
            "adaptations": [
                {"name": "d", "documentclass": ["a"], "actions": [{"op": "remove_package", "packages": ["p"]}]},
                {"name": "d", "documentclass": ["b"], "actions": [{"op": "remove_package", "packages": ["p"]}]},
            ]
        },
        {"version": 1},
    ],
)
def test_broken_table_rejected(raw):
    with pytest.raises(ij.InjectError):
        ij._parse_table(raw)


# --------------------------------------------------------------------------- #
# 幂等：compile 回环会反复调用，注入块绝不能叠加
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", FIXTURES)
def test_idempotent(name):
    once = ij.inject(load(name))
    twice = ij.inject(once.text)
    assert twice.branch == "passthrough"
    assert twice.text == once.text
    assert twice.changed is False
    assert once.text.count(ij.BEGIN_MARK) == twice.text.count(ij.BEGIN_MARK) <= 1


def test_idempotent_after_adaptation():
    entry = next(
        a for a in ij.load_adaptation_table().examples if a.name == "revtex-inject-late"
    )
    table = ij.AdaptationTable(adaptations=(entry,))
    once = ij.inject(load("revtex"), adaptation=table)
    twice = ij.inject(once.text, adaptation=table)
    assert twice.branch == "passthrough"
    assert twice.text == once.text


# --------------------------------------------------------------------------- #
# report 摘要
# --------------------------------------------------------------------------- #


def test_to_json_summary():
    data = ij.inject(load("cjkutf8")).to_json()
    assert data["engine"] == "xelatex"
    assert data["branch"] == "replace"
    assert data["changed"] is True
    assert data["documentclass"] == "article"
    assert data["adaptations"] == ["cjkutf8-to-xecjk"]
    assert data["removed_packages"] == ["CJKutf8"]
    assert data["stripped_environments"] == ["CJK*"]
    assert "warnings" not in data


def test_to_json_passthrough_summary():
    data = ij.inject(load("existing_xecjk")).to_json()
    assert data["branch"] == "passthrough"
    assert data["changed"] is False
    assert data["adaptations"] == []
