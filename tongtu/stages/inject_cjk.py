"""inject_cjk：让英文 preamble 的论文能排中文（架构 §3 compile 行、§10、决策 13）。

不是独立阶段——决策 13 把 assemble 并进了 compile：本模块是 compile 驱动器在
「unmask 回填 → **注入** → latexmk -xelatex 回环」中间调用的那一步纯文本变换。
和 mask/unmask/chunk 一样：**纯函数，不碰文件系统、不接 CLI**，落盘（`build/zh-raw.tex`）
与回环由 compile 阶段驱动器负责。

引擎恒为 xelatex（架构 §13 选型表），故 `InjectResult.engine` 是常量而非判断结果。

三分支（继承 v2 `scripts/inject_cjk.py`，自适应输入）
----------------------------------------------------

1. **已有 xeCJK / ctex**（含 ctex 系文档类）→ 原样通过，尊重文档自带的字体配置；
2. **CJKutf8 + CJK\\* 包裹**（DeepSeek 系列论文是典型）→ 换成 xeCJK 配置：CJKutf8 默认的
   gbsn 是文鼎简报宋，无真粗体、行距过密，观感差；
3. **其余（纯英文导言区）**→ 在 `\\documentclass` 之后注入 xeCJK 配置。

分支 1 保证**幂等**：本模块注入的块自带 `\\usepackage{xeCJK}`，对已注入文本再跑一次即
分支 1 原样通过。compile 回环反复重编译时不会叠加注入块。

字体
----

正文用随仓库分发的霞鹜文楷（`Path = {fonts/}` **相对路径**，编译时 compile 把
`fonts/` 链进 build 目录、export 把它拷进自包含产物包，故同一份 `zh.tex` 在开发机与
参考镜像里都能编译）；无衬线不分发，用 `\\IfFontExistsTF` 按平台探测
（Hiragino Sans GB → Noto Sans CJK SC → 霞鹜文楷兜底，架构 §10）。

适配表
------

`tongtu/data/documentclass.json` 是**叠加层**：按 documentclass 名（或导言区出现的包）
追加前导区补丁、调整注入位置、删包、剥环境。表为空不影响上面三分支——它的意义是让
关节⑥（适配与修复会话）把成功适配按促升规则沉淀为纯数据条目（架构 §2 原则 3、决策 13
末句），而不是把一次性 hack 塞进编排器。CJKutf8 分支本身就是一条这样的条目
（`BUILTIN_ADAPTATIONS`），既是主逻辑也是表达力的样例。

v2 审计（迁移时发现并修掉的三处）
--------------------------------

1. **正则会命中注释里的 `\\documentclass`**。v2 用 `re.search(r"\\\\documentclass…")`
   直扫全文：被注释掉的 `% \\documentclass{old}`（arXiv 源码里极常见——作者留着旧文档类
   备用）会先命中，注入块于是被塞进注释行内部，整块配置失效且下一行开始全乱。同理
   `\\usepackage{xeCJK}` 的探测也会被注释里的、以及正文 `verbatim`/`lstlisting` 里展示的
   同名代码骗到。本实现一律用 `tongtu.texlex` 词法扫描，且包探测只看导言区。
2. **`XECJK_BLOCK.replace("\\\\", "\\\\\\\\")` 是给 `re.sub` 模板打补丁**。`re.sub` 的
   replacement 会解释 `\\1`、`\\g<name>`、`\\n` 等转义，v2 靠「把每个反斜杠翻倍」绕开——
   对当前这段固定文本恰好成立，但它是个陷阱：块里一旦出现 `\\g<…>` 形状的内容，或者
   （本实现新增的）**适配表 `preamble_patch` 这类由 agent 写进数据文件的文本**被当模板
   代入，就会静默损坏输出。本实现完全不用 `re.sub` 模板：所有改写都是
   `(start, end, text)` 三元组切片（`_apply_edits`），插入文本按字面写入。
3. **`"ctexart" in src` 是子串判断**。正文里写了 "ctexart"、注释里提了一句、或文档类叫
   `ctexart-custom`，都会被判成「已有中文支持」而整篇不注入（症状是编译出来满页豆腐或
   缺字）；反过来 `ctexrep`/`ctexbook`/`ctexbeamer` 又漏判。本实现解析出 documentclass
   **名**再与 `CTEX_CLASSES` 精确比对。

另外两处一并修正的：`\\usepackage(\\[[^\\]]*\\])?\\{(xeCJK|ctex)\\}` 认不出逗号列表
（`\\usepackage{xeCJK,graphicx}`）也认不出 `\\RequirePackage`，且方括号里嵌套 `[]` 时
可选参数会截断——本实现按逗号列表解析、括号用 `find_bracket_arg` 配平；
`\\begin\\{CJK\\*?\\}\\{UTF8\\}\\{[^}]*\\}` 写死了 UTF8 编码参数，本实现按参数个数吃。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

from ..texlex import (
    Lexer,
    TexLexError,
    find_balanced,
    find_bracket_arg,
    strip_comments_inline,
)

__all__ = [
    "InjectError",
    "ENGINE",
    "XECJK_BLOCK",
    "BEGIN_MARK",
    "END_MARK",
    "CJK_PACKAGES",
    "CTEX_CLASSES",
    "POSITIONS",
    "OPS",
    "BRANCHES",
    "BUILTIN_ADAPTATIONS",
    "EnvStrip",
    "Action",
    "Adaptation",
    "AdaptationTable",
    "DocumentClass",
    "PackageUse",
    "InjectResult",
    "load_adaptation_table",
    "find_document_start",
    "find_documentclass",
    "preamble_packages",
    "inject",
]


class InjectError(ValueError):
    """无法注入（找不到 `\\documentclass`、适配表损坏、改写区间冲突）。

    源码本身的小毛病（环境参数不配平之类）不抛这个：降级为不做那一处改写并记
    `InjectResult.warnings`，注入块照样落地——compile 回环才是裁决者。
    """


#: 编译引擎恒为 xelatex（架构 §13）。
ENGINE = "xelatex"

_DATA_FILE = "data/documentclass.json"

#: 判「文档已自带中文支持」的包名（精确匹配，只看导言区）。
CJK_PACKAGES = frozenset({"xeCJK", "ctex", "ctexcap"})

#: 判「文档已自带中文支持」的文档类名（精确匹配——v2 的 `"ctexart" in src` 会误伤）。
CTEX_CLASSES = frozenset({"ctexart", "ctexrep", "ctexbook", "ctexbeamer"})

#: 词法扫描时视为 verbatim 的环境名（体内的 `%`、`\\begin{…}` 都只是字符）。
#: inject 运行在 compile 阶段、拿不到 mask 的分类表，故内置一份保守清单。
_VERBATIM_ENVS = frozenset(
    {
        "verbatim",
        "verbatim*",
        "Verbatim",
        "Verbatim*",
        "lstlisting",
        "lstlisting*",
        "minted",
        "minted*",
        "alltt",
        "alltt*",
        "comment",
        "listing",
        "listing*",
    }
)

_PACKAGE_CS = frozenset({"\\usepackage", "\\RequirePackage"})

#: 注入位置名（含义见 `tongtu/data/documentclass.json` 的 positions 段）。
POSITIONS = (
    "after_documentclass",
    "before_begin_document",
    "after_last_usepackage",
    "after_package",
    "at_removed_package",
)

#: 适配动作 op 名（含义见数据文件的 ops 段）。
OPS = ("insert_at", "preamble_patch", "remove_package", "strip_environment")

#: 分支名（进 report，供促升统计）。
BRANCHES = ("passthrough", "replace", "inject")


# --------------------------------------------------------------------- 注入块

BEGIN_MARK = "% ---- injected by tongtu (inject_cjk) ----"
END_MARK = "% ---- end tongtu (inject_cjk) ----"

#: xeCJK 配置本体。与 v2 逐命令等效：霞鹜文楷相对路径、BoldFont 用 Medium、
#: 无衬线 `\\IfFontExistsTF` 探测链、`\\XeTeXlinebreaklocale` + `\\XeTeXlinebreakskip`、
#: `\\linespread{1.4}`。改这里等于改所有译文的排版，动它要有实测依据。
XECJK_BODY = r"""\usepackage{xeCJK}
\setCJKmainfont[
  Path = {fonts/},
  BoldFont = LXGWWenKai-Medium.ttf
]{LXGWWenKai-Light.ttf}
% 无衬线取平台上第一个存在的黑体：mac → Hiragino，Linux 容器 → Noto；
% 都没有则退回仓库字体（非黑体，但保证不炸、不出豆腐）
\IfFontExistsTF{Hiragino Sans GB}
  {\setCJKsansfont{Hiragino Sans GB}}
  {\IfFontExistsTF{Noto Sans CJK SC}
    {\setCJKsansfont{Noto Sans CJK SC}}
    {\setCJKsansfont[Path={fonts/},BoldFont=LXGWWenKai-Medium.ttf]{LXGWWenKai-Light.ttf}}}
\setCJKmonofont[Path={fonts/}]{LXGWWenKai-Light.ttf}
\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip = 0pt plus 1pt
\linespread{1.4}
"""

#: 无适配补丁时注入的完整块（含首尾标记注释，以换行收尾）。
XECJK_BLOCK = f"{BEGIN_MARK}\n{XECJK_BODY}{END_MARK}\n"


def _render_block(before: Sequence[tuple[str, str]], after: Sequence[tuple[str, str]]) -> str:
    """组装注入块：适配补丁夹在标记注释内，每条前面标出条目名便于人工与关节⑥定位。"""
    parts = [BEGIN_MARK, "\n"]
    for name, tex in before:
        parts.append(f"% [adaptation: {name}]\n{tex.rstrip()}\n")
    parts.append(XECJK_BODY)
    for name, tex in after:
        parts.append(f"% [adaptation: {name}]\n{tex.rstrip()}\n")
    parts.append(END_MARK)
    parts.append("\n")
    return "".join(parts)


# --------------------------------------------------------------------- 适配表


@dataclass(frozen=True)
class EnvStrip:
    """`strip_environment` 的一项：剥哪个环境、`\\begin` 后跟几个必选参数。"""

    name: str
    args: int = 0
    starred: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, self.name + "*") if self.starred else (self.name,)


@dataclass(frozen=True)
class Action:
    """适配表的一个动作。`op` 决定哪些字段有意义（见数据文件 ops 段）。"""

    op: str
    position: str | None = None  # insert_at
    package: str | None = None  # insert_at / after_package
    where: str = "after_block"  # preamble_patch
    tex: str = ""  # preamble_patch
    packages: tuple[str, ...] = ()  # remove_package
    environments: tuple[EnvStrip, ...] = ()  # strip_environment


@dataclass(frozen=True)
class Adaptation:
    """适配表的一条条目：命中条件 + 动作序列。"""

    name: str
    actions: tuple[Action, ...]
    documentclasses: frozenset[str] = frozenset()
    packages: frozenset[str] = frozenset()
    notes: str = ""
    source: str = ""

    def matches(self, documentclass: str | None, packages: Iterable[str]) -> bool:
        """条件同时给出则须都命中；同一条件内的列表是**任一命中**。"""
        if self.documentclasses and (documentclass not in self.documentclasses):
            return False
        if self.packages and self.packages.isdisjoint(set(packages)):
            return False
        return True


@dataclass(frozen=True)
class AdaptationTable:
    """`tongtu/data/documentclass.json` 的内存形态。"""

    adaptations: tuple[Adaptation, ...] = ()
    version: int = 0
    examples: tuple[Adaptation, ...] = ()

    def resolve(self, documentclass: str | None, packages: Iterable[str]) -> tuple[Adaptation, ...]:
        """内建条目 + 表条目里命中的那些，按「内建在前、表按数组序」叠加。

        表里与内建**同名**的条目覆盖内建（关节⑥据实测修正内建行为的唯一入口）。
        """
        names = set(pkg for pkg in packages)
        overridden = {a.name for a in self.adaptations}
        ordered = [a for a in BUILTIN_ADAPTATIONS if a.name not in overridden]
        ordered.extend(self.adaptations)
        return tuple(a for a in ordered if a.matches(documentclass, names))


#: CJKutf8 分支：写成数据就是适配表的一条（数据文件 examples 段有同样的 JSON）。
#: 内建保证「表为空时三分支主逻辑不变」；表里同名条目覆盖它。
BUILTIN_ADAPTATIONS: tuple[Adaptation, ...] = (
    Adaptation(
        name="cjkutf8-to-xecjk",
        packages=frozenset({"CJKutf8", "CJK"}),
        actions=(
            Action(op="remove_package", packages=("CJKutf8", "CJK", "CJKspace", "CJKpunct")),
            Action(op="strip_environment", environments=(EnvStrip("CJK", args=2, starred=True),)),
            Action(op="insert_at", position="at_removed_package"),
        ),
        notes="CJKutf8 默认的 gbsn（文鼎简报宋）无真粗体、行距过密，整体换成 xeCJK + 霞鹜文楷。",
        source="v2 scripts/inject_cjk.py",
    ),
)


def _parse_action(raw: Mapping, entry: str) -> Action:
    op = raw.get("op")
    if op not in OPS:
        raise InjectError(f"适配条目 {entry!r} 的 op 非法：{op!r}")
    if op == "insert_at":
        position = raw.get("position")
        if position not in POSITIONS:
            raise InjectError(f"适配条目 {entry!r} 的 position 非法：{position!r}")
        package = raw.get("package")
        if position == "after_package" and not package:
            raise InjectError(f"适配条目 {entry!r}：position=after_package 需要 package 字段")
        return Action(op=op, position=position, package=package)
    if op == "preamble_patch":
        where = raw.get("where", "after_block")
        if where not in ("before_block", "after_block"):
            raise InjectError(f"适配条目 {entry!r} 的 where 非法：{where!r}")
        tex = raw.get("tex")
        if not isinstance(tex, str) or not tex.strip():
            raise InjectError(f"适配条目 {entry!r}：preamble_patch 缺 tex")
        return Action(op=op, where=where, tex=tex)
    if op == "remove_package":
        packages = tuple(raw.get("packages") or ())
        if not packages or not all(isinstance(p, str) and p for p in packages):
            raise InjectError(f"适配条目 {entry!r}：remove_package 的 packages 非法")
        return Action(op=op, packages=packages)
    envs: list[EnvStrip] = []
    for item in raw.get("environments") or ():
        if not isinstance(item, dict) or not item.get("name"):
            raise InjectError(f"适配条目 {entry!r}：strip_environment 的 environments 非法")
        envs.append(
            EnvStrip(
                name=str(item["name"]),
                args=int(item.get("args", 0)),
                starred=bool(item.get("starred", False)),
            )
        )
    if not envs:
        raise InjectError(f"适配条目 {entry!r}：strip_environment 缺 environments")
    return Action(op=op, environments=tuple(envs))


def _parse_adaptation(raw: Mapping) -> Adaptation:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise InjectError("适配条目缺 name")
    documentclasses = frozenset(raw.get("documentclass") or ())
    packages = frozenset(raw.get("packages") or ())
    if not documentclasses and not packages:
        # 防呆：省略全部条件的条目会对每篇论文生效，多半是写漏了。
        raise InjectError(f"适配条目 {name!r}：documentclass 与 packages 不能同时省略")
    actions = tuple(_parse_action(a, name) for a in raw.get("actions") or ())
    if not actions:
        raise InjectError(f"适配条目 {name!r}：缺 actions")
    return Adaptation(
        name=name,
        actions=actions,
        documentclasses=documentclasses,
        packages=packages,
        notes=str(raw.get("notes", "")),
        source=str(raw.get("source", "")),
    )


def _parse_table(raw: Mapping) -> AdaptationTable:
    entries = raw.get("adaptations")
    if entries is None or not isinstance(entries, list):
        raise InjectError("适配表缺 adaptations 数组")
    adaptations = tuple(_parse_adaptation(e) for e in entries)
    names = [a.name for a in adaptations]
    if len(set(names)) != len(names):
        raise InjectError("适配表条目名重复")
    examples = tuple(_parse_adaptation(e) for e in raw.get("examples") or ())
    return AdaptationTable(
        adaptations=adaptations,
        version=int(raw.get("version", 0)),
        examples=examples,
    )


@lru_cache(maxsize=1)
def load_adaptation_table() -> AdaptationTable:
    """读打包进 wheel 的适配表（`tongtu/data/documentclass.json`）。"""
    text = files("tongtu").joinpath(_DATA_FILE).read_text(encoding="utf-8")
    return _parse_table(json.loads(text))


# ----------------------------------------------------------------- 词法小工具
#
# 与 `stages/mask.py` 里的同名私有函数同形。刻意不跨模块引用私有函数、也不为这三行往
# `texlex` 里加公开 API：M2 的改动面越小越好，且两处的语义各自独立演化。


def _skip_spaces(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _skip_optionals(s: str, i: int) -> int:
    """跳过零个或多个可选参数 `[...]`（含其间空白）。"""
    while True:
        j = _skip_spaces(s, i)
        if j < len(s) and s[j] == "[":
            try:
                i = find_bracket_arg(s, j) + 1
            except TexLexError:
                return i
        else:
            return i


def _read_group(s: str, i: int) -> tuple[int, int] | None:
    """读一个必选参数 `{...}`，返回 (内容起, 内容止)；不是 `{` 或不配平则 None。"""
    j = _skip_spaces(s, i)
    if j >= len(s) or s[j] != "{":
        return None
    try:
        close = find_balanced(s, j)
    except TexLexError:
        return None
    return j + 1, close


# ------------------------------------------------------------------- 导言区扫描


@dataclass(frozen=True)
class DocumentClass:
    """`\\documentclass[opts]{name}` 的解析结果（词法定位，注释掉的不算）。"""

    name: str
    options: tuple[str, ...]
    span: tuple[int, int]  # 整条命令（含可选参数与花括号）


@dataclass(frozen=True)
class PackageUse:
    """导言区里一个包的一次加载。逗号列表中每个名字各得一条。"""

    name: str
    cs: str  # usepackage / RequirePackage
    span: tuple[int, int]  # 整条命令
    group: tuple[int, int]  # 花括号内部（逗号列表整体）
    segment: tuple[int, int]  # 本名字在逗号列表里占的区间（含两侧空白）
    siblings: tuple[str, ...]  # 同一条命令里的全部包名


def find_document_start(src: str) -> int | None:
    """`\\begin{document}` 的**起始**偏移（词法判定：注释与 verbatim 里的不算）。"""
    lexer = Lexer(src, verbatim_envs=_VERBATIM_ENVS)
    for tok in lexer:
        if tok.kind == "begin" and tok.name == "document":
            return tok.start
    return None


def find_documentclass(src: str, *, stop: int | None = None) -> DocumentClass | None:
    """第一个真正生效的 `\\documentclass`。

    词法扫描而非 `re.search`：被注释掉的 `% \\documentclass{old}`（arXiv 源码里极常见）
    与 verbatim 里展示的都不算数——v2 的正则会命中它们并把注入块塞进注释行（审计 1）。
    """
    lexer = Lexer(src, verbatim_envs=_VERBATIM_ENVS, stop=stop)
    for tok in lexer:
        if tok.kind != "control" or src[tok.start : tok.end] != "\\documentclass":
            continue
        i = tok.end
        options: tuple[str, ...] = ()
        j = _skip_spaces(src, i)
        if j < len(src) and src[j] == "[":
            try:
                close = find_bracket_arg(src, j)
            except TexLexError:
                return None
            # 先剥注释再切逗号：`[11pt, % 字号\n a4paper]` 里的注释会带进选项名。
            raw = strip_comments_inline(src[j + 1 : close], _VERBATIM_ENVS)
            options = tuple(o.strip() for o in raw.split(",") if o.strip())
            i = close + 1
        group = _read_group(src, i)
        if group is None:
            return None
        start, end = group
        return DocumentClass(
            name=src[start:end].strip(),
            options=options,
            span=(tok.start, end + 1),
        )
    return None


def preamble_packages(src: str, *, stop: int | None = None) -> tuple[PackageUse, ...]:
    """扫 `\\usepackage` / `\\RequirePackage`（词法，逗号列表逐名展开）。

    v2 的 `\\usepackage(\\[[^\\]]*\\])?\\{(xeCJK|ctex)\\}` 认不出 `\\usepackage{xeCJK,graphicx}`、
    认不出 `\\RequirePackage`、可选参数里嵌套 `[]` 会截断，且扫的是全文（正文 verbatim
    里贴一段 `\\usepackage{xeCJK}` 就会误判成「已有中文支持」）。
    """
    uses: list[PackageUse] = []
    lexer = Lexer(src, verbatim_envs=_VERBATIM_ENVS, stop=stop)
    for tok in lexer:
        if tok.kind != "control" or src[tok.start : tok.end] not in _PACKAGE_CS:
            continue
        i = _skip_optionals(src, tok.end)
        group = _read_group(src, i)
        if group is None:
            continue
        start, end = group
        raw = src[start:end]
        names: list[tuple[str, int, int]] = []
        pos = start
        for piece in raw.split(","):
            seg_end = pos + len(piece)
            # 名字要剥注释（`\usepackage{graphicx, % 图\n xeCJK}` 里的 xeCJK 是真加载），
            # 但区间保持原样——删包时按区间重写，注释跟着它自己那个名字走。
            name = strip_comments_inline(piece, _VERBATIM_ENVS).strip()
            if name:
                names.append((name, pos, seg_end))
            pos = seg_end + 1  # 跳过逗号
        siblings = tuple(n for n, _, _ in names)
        span = (tok.start, end + 1)
        cs = src[tok.start + 1 : tok.end]
        for name, seg_start, seg_end in names:
            uses.append(
                PackageUse(
                    name=name,
                    cs=cs,
                    span=span,
                    group=(start, end),
                    segment=(seg_start, seg_end),
                    siblings=siblings,
                )
            )
        lexer.pos = end + 1
    return tuple(uses)


# --------------------------------------------------------------------- 结果


@dataclass(frozen=True)
class InjectResult:
    """注入结果。`text` 进 `build/zh-raw.tex`，其余进 report / 日志。"""

    text: str
    branch: str
    engine: str = ENGINE
    documentclass: str | None = None
    adaptations: tuple[str, ...] = ()
    position: str | None = None
    removed_packages: tuple[str, ...] = ()
    stripped_environments: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    edits: tuple[tuple[int, int, int], ...] = ()
    """应用过的改写 `(源起, 源止, 替换后长度)`，按起点升序（分支 1 恒为空）。

    只为 :meth:`map_offset` 而存在：调用方（compile）握着一份**源码侧**的块区间，
    注入之后要把它换算到 `text` 的坐标系里。不进 :meth:`to_json`——它是调用方当场用掉的
    中间量，不是报告内容。
    """

    def map_offset(self, offset: int) -> int:
        """源码字符偏移 → :attr:`text` 里的偏移。

        落在被删改区间内部的偏移退到该区间在新文本里的起点（那段内容已经不在了）；
        插入点上的偏移落在插入内容**之后**——注入块整体属于导言区，正文的区间不该被它
        向前吞掉。
        """
        delta = 0
        for start, end, length in self.edits:
            if offset >= end:
                delta += length - (end - start)
            elif offset > start:
                return start + delta
            else:
                break
        return offset + delta

    @property
    def changed(self) -> bool:
        """是否改写了源码（分支 1 恒为 False——幂等的机械判据）。"""
        return self.branch != "passthrough"

    def to_json(self) -> dict:
        """给 compile 驱动器写 report / 日志的结构化摘要。

        注意 `report.schema.json` 的 `compile` 段目前**没有**放这份摘要的字段
        （`additionalProperties: false`）；接 compile 驱动器时按「先改 schema 再改码」
        的流程给它加一个 `inject` 子对象。
        """
        data: dict = {
            "engine": self.engine,
            "branch": self.branch,
            "changed": self.changed,
            "documentclass": self.documentclass,
            "adaptations": list(self.adaptations),
        }
        if self.position is not None:
            data["position"] = self.position
        if self.removed_packages:
            data["removed_packages"] = list(self.removed_packages)
        if self.stripped_environments:
            data["stripped_environments"] = list(self.stripped_environments)
        if self.warnings:
            data["warnings"] = list(self.warnings)
        return data


# --------------------------------------------------------------------- 改写


@dataclass(frozen=True)
class _Edit:
    """一次切片改写：把 `[start, end)` 换成 `text`（插入即 start == end）。"""

    start: int
    end: int
    text: str


def _apply_edits(src: str, edits: Sequence[_Edit]) -> tuple[str, tuple[tuple[int, int, int], ...]]:
    """按偏移升序应用改写，并带出 `(源起, 源止, 替换后长度)` 供偏移换算。

    刻意不用 `re.sub`：其 replacement 会解释 `\\1` / `\\g<name>`，而注入块与适配表的
    `preamble_patch` 都是 LaTeX 文本（还可能由 agent 写进数据文件），当模板代入会静默
    损坏（审计 2）。切片拼接对反斜杠零解释。
    """
    # 两条适配条目删同一个包 / 剥同一个环境时会生成完全相同的改写，去重即可；
    # 真正冲突（区间交叠但内容不同）仍然报错——那是适配表写错了，不该猜。
    unique = {(e.start, e.end, e.text): e for e in edits}
    out: list[str] = []
    applied: list[tuple[int, int, int]] = []
    cursor = 0
    for edit in sorted(unique.values(), key=lambda e: (e.start, e.end)):
        if edit.start < cursor:
            raise InjectError(f"改写区间重叠：{edit.start} < {cursor}")
        out.append(src[cursor : edit.start])
        out.append(edit.text)
        applied.append((edit.start, edit.end, len(edit.text)))
        cursor = edit.end
    out.append(src[cursor:])
    return "".join(out), tuple(applied)


def _separator(src: str, pos: int) -> str:
    """注入块前的分隔：块必须自起一行，并与上文空一行（与 v2 的版式一致）。"""
    if pos == 0:
        return ""
    return "\n" if src[pos - 1] == "\n" else "\n\n"


def _remove_package_edits(
    src: str, uses: Sequence[PackageUse], targets: Iterable[str]
) -> tuple[list[_Edit], list[str], int | None]:
    """删包：整条命令的包名全在删除集里才整条删，否则重写逗号列表只摘掉命中的名字。

    **按命令**（而不是按名字）生成改写：`\\usepackage{CJK,CJKutf8,graphicx}` 里连着删两个
    名字时，逐名删「名字 + 相邻逗号」的写法会让两次改写抢同一个逗号（或在删最后一个名字
    时吃掉 `}`）。整组重写没有这个问题。
    """
    wanted = set(targets)
    edits: list[_Edit] = []
    removed: list[str] = []
    first: int | None = None
    by_command: dict[tuple[int, int], list[PackageUse]] = {}
    for use in uses:
        by_command.setdefault(use.span, []).append(use)
    for span, group_uses in by_command.items():
        hits = [u for u in group_uses if u.name in wanted]
        if not hits:
            continue
        removed.extend(u.name for u in hits)
        first = span[0] if first is None else min(first, span[0])
        if len(hits) == len(group_uses):
            edits.append(_Edit(span[0], span[1], ""))
            continue
        survivors = ",".join(src[u.segment[0] : u.segment[1]] for u in group_uses if u.name not in wanted)
        edits.append(_Edit(group_uses[0].group[0], group_uses[0].group[1], survivors))
    return edits, removed, first


def _strip_environment_edits(src: str, specs: Iterable[EnvStrip], warnings: list[str]) -> tuple[list[_Edit], list[str]]:
    """剥掉 `\\begin{X}{…}{…}` / `\\end{X}` 包裹（保留环境体），词法定位。

    同名嵌套无所谓：本操作是「删掉全部同名 begin/end 包裹」，不需要配对。参数不配平
    时只删 `\\begin{X}` 自身并记警告——绝不吞掉正文。
    """
    args_of: dict[str, int] = {}
    for spec in specs:
        for name in spec.names:
            args_of[name] = spec.args
    if not args_of:
        return [], []
    edits: list[_Edit] = []
    stripped: list[str] = []
    lexer = Lexer(src, verbatim_envs=_VERBATIM_ENVS)
    for tok in lexer:
        if tok.name not in args_of or tok.kind not in ("begin", "end"):
            continue
        end = tok.end
        if tok.kind == "begin":
            for _ in range(args_of[tok.name]):
                group = _read_group(src, end)
                if group is None:
                    warnings.append(f"偏移 {tok.start}：\\begin{{{tok.name}}} 的参数不配平，只删环境头")
                    break
                end = group[1] + 1
        if end < len(src) and src[end] == "\n":
            end += 1
        edits.append(_Edit(tok.start, end, ""))
        stripped.append(tok.name)
        lexer.pos = end
    return edits, sorted(set(stripped))


def _resolve_position(
    src: str,
    position: str,
    *,
    package: str | None,
    documentclass: DocumentClass | None,
    uses: Sequence[PackageUse],
    document_start: int | None,
    removed_at: int | None,
    warnings: list[str],
) -> tuple[int, str]:
    """位置名 → 偏移。解析不出就退回 `after_documentclass` 并记警告。"""

    def fallback(reason: str) -> tuple[int, str]:
        warnings.append(f"注入位置 {position} 无法解析（{reason}），退回 after_documentclass")
        return _resolve_position(
            src,
            "after_documentclass",
            package=None,
            documentclass=documentclass,
            uses=uses,
            document_start=document_start,
            removed_at=None,
            warnings=warnings,
        )

    if position == "after_documentclass":
        if documentclass is None:
            raise InjectError("未找到 \\documentclass（注释与 verbatim 里的不算）")
        return documentclass.span[1], position
    if position == "before_begin_document":
        if document_start is None:
            return fallback("未找到 \\begin{document}")
        return document_start, position
    if position == "after_last_usepackage":
        if not uses:
            return fallback("导言区没有 \\usepackage")
        return max(u.span[1] for u in uses), position
    if position == "after_package":
        hits = [u for u in uses if u.name == package]
        if not hits:
            return fallback(f"导言区没有 {package}")
        return hits[0].span[1], position
    if removed_at is None:
        return fallback("没有包被删除")
    return removed_at, position


# --------------------------------------------------------------------- 入口


def inject(src: str, *, adaptation: AdaptationTable | None = None) -> InjectResult:
    """给 LaTeX 源码装上中文排版能力（三分支见模块文档）。

    `adaptation` 是 documentclass 适配表（默认读 `tongtu/data/documentclass.json`）——
    表为空时三分支主逻辑不变。返回改写后的文本、恒为 xelatex 的引擎名，以及分支名与
    命中的适配条目名（供 report 统计促升）。

    找不到 `\\documentclass` 且文档也没有自带 CJK 支持时抛 `InjectError`：这不是可以
    「保守降级」的情形——不注入就是整篇中文排不出来。
    """
    table = adaptation if adaptation is not None else load_adaptation_table()
    warnings: list[str] = []

    document_start = find_document_start(src)
    dc = find_documentclass(src, stop=document_start)
    uses = preamble_packages(src, stop=document_start)
    names = {u.name for u in uses}

    # 分支 1：文档已自带中文支持 → 原样通过（也是本模块的幂等保证）。
    if names & CJK_PACKAGES or (dc is not None and dc.name in CTEX_CLASSES):
        return InjectResult(
            text=src,
            branch="passthrough",
            documentclass=dc.name if dc else None,
            warnings=tuple(warnings),
        )

    matched = table.resolve(dc.name if dc else None, names)

    edits: list[_Edit] = []
    removed: list[str] = []
    stripped: list[str] = []
    before: list[tuple[str, str]] = []
    after: list[tuple[str, str]] = []
    position, position_pkg = "after_documentclass", None
    removed_at: int | None = None

    for entry in matched:
        for action in entry.actions:
            if action.op == "insert_at":
                position, position_pkg = action.position or position, action.package
            elif action.op == "preamble_patch":
                (before if action.where == "before_block" else after).append((entry.name, action.tex))
            elif action.op == "remove_package":
                pkg_edits, pkg_names, first = _remove_package_edits(src, uses, action.packages)
                edits.extend(pkg_edits)
                removed.extend(pkg_names)
                if first is not None:
                    removed_at = first if removed_at is None else min(removed_at, first)
            else:
                env_edits, env_names = _strip_environment_edits(src, action.environments, warnings)
                edits.extend(env_edits)
                stripped.extend(env_names)

    offset, position = _resolve_position(
        src,
        position,
        package=position_pkg,
        documentclass=dc,
        uses=uses,
        document_start=document_start,
        removed_at=removed_at,
        warnings=warnings,
    )
    block = _separator(src, offset) + _render_block(before, after)
    edits.append(_Edit(offset, offset, block))

    text, applied = _apply_edits(src, edits)
    return InjectResult(
        text=text,
        # 分支 2 与分支 3 的区别只在「有没有拆掉原有的 CJK 方案」。
        branch="replace" if removed else "inject",
        documentclass=dc.name if dc else None,
        adaptations=tuple(e.name for e in matched),
        position=position,
        removed_packages=tuple(dict.fromkeys(removed)),
        stripped_environments=tuple(dict.fromkeys(stripped)),
        warnings=tuple(warnings),
        edits=applied,
    )
