"""survey 阶段驱动器：一次通读产出 brief + 术语预扫决策（架构 §3、决策 11、关节④）。

survey 补的是**掩码与分块留下的上下文缺口**：掩码 + 分块之后，翻译时模型只见「挖了洞的一小段」，
跨章节的记号约定、指称方式、语体是结构性风险。survey 花**一次**全文 token，产出两样
稳定共享上下文——`brief.json`（全文纲要）与术语决策表——供后续每一块复用；块间一致性
靠它们，不靠把前块译文链式传给后块（那会让缓存失效沿块链级联，架构 §3 末）。

    survey_view(masked, blocks) → 剔附录与参考文献 → 一次 complete → 防御性解析 JSON
                                                             │解析/校验失败
                                                             ↓ 重试一次（喂回错误）
                                                             ↓ 仍失败
                                                        确定性骨架（降级）

## 通读输入怎么来

`tongtu.stages.unmask.survey_view` 已经按块类型参数化回填：数学类块回填原文（记号约定
住在行间公式里），表格 / 图 / tikz / 代码保持占位符（对通读是纯噪音且是 token 大头），
注释与前导区删掉。本模块只多做一件事——**剔除附录与参考文献**（架构决策 11）。

剔法：先在**掩码流**上定位切点（附录起点 / 参考文献起点，取更靠前者），截断后再做
`survey_view`。切点两个来源：

1. `tongtu.stages.chunk.split_paragraphs` 标出的第一个 `is_appendix` 段落的起点——它认
   得 `\\appendix`、`\\appendices` 与 `appendices` 环境，且带环境深度计数（宏定义体里的
   假 `\\appendix` 不算数）；
2. 参考文献标记（`\\begin{thebibliography}` / `\\bibliography{…}` / `\\printbibliography`）
   与 `\\end{document}` 的正则位置。

**刻意不依赖 chunk 阶段**：`split_paragraphs` 是纯函数（无 IO、不认识工作目录），于是
survey 仍能坐在架构表规定的位置上——mask → survey → chunk，不必为了剔附录把 chunk 提前。
附录不进通读，但**仍正常翻译**。

## 失败不阻塞流水线

brief 是**增益不是门禁**：模型输出的 JSON 截断了、裹了 markdown 代码块、掺了解释性文
字，都先防御性解析（:func:`parse_json_object`），失败则把解析错误喂回去重试一次；再失
败就降级为**确定性骨架**——章节树从掩码流的标题命令扫出来、abstract 原文照录、其余留
空、术语零增补，记 warnings 并把 `degraded=True` 摆在结果里。流水线照常往下走。

## abstract 由程序填，不由模型抄

架构 §3 要求 abstract **原文照录**（避免全部块对摘要译文形成级联依赖）。让模型抄一遍
原文既费 token 又可能被改写，故本模块从源码里**程序侧**取：mask 抽出的
`kind="abstract"` caption 槽位优先，没有则在完整回填的原文里按词法定位
`\\begin{abstract}…\\end{abstract}` 或 `\\abstract{…}`。标题同理。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from .. import CONTRACT_VERSION, prompts
from ..glossary import Glossary, empty as empty_glossary, with_agent_decisions
from ..prompts import PromptError
from ..schema_check import SchemaError, check as schema_check
from ..texlex import Lexer, TexLexError, find_env_end, read_group, skip_optionals
from .chunk import split_paragraphs
from .mask import Block, Caption, MaskResult, load_environment_table
from .unmask import survey_view, unmask

__all__ = [
    "BRIEF_NAME",
    "DEFAULT_MAX_RETRIES",
    "DEGRADED",
    "FAILED",
    "GLOSSARY_NAME",
    "JOINT",
    "OK",
    "PROMPT_NAME",
    "SurveyResult",
    "brief_hash",
    "build_prompt",
    "cut_offset",
    "load_prompt",
    "paper_facts",
    "parse_json_object",
    "prompt_version",
    "reading_view",
    "render_brief",
    "skeleton_brief",
    "survey",
]


class SurveyParseError(ValueError):
    """模型输出解析不出一个 JSON 对象（截断、非 JSON、结构不对）。"""


# ------------------------------------------------------------------ 常量

#: 本阶段的 agent 关节（`tongtu.agent.JOINTS` 的 ④）。
JOINT = "survey"

#: 本阶段的 prompt 资产名（`skill/survey/SKILL.md`）。
PROMPT_NAME = prompts.SURVEY


def prompt_version() -> str:
    """关节④规则的版本号 = `skill/survey/SKILL.md` frontmatter 的 `version`。

    **逐技能**而不是全局一个（见 :mod:`tongtu.prompts` 的「版本号」一节）；它进 brief 的
    `generated_by` 与 survey 的阶段 manifest。
    """
    return prompts.version_of(PROMPT_NAME)


#: 产物名（本期落 `build/`；export 阶段 M4 搬进 `out/`）。
BRIEF_NAME = "brief.json"
GLOSSARY_NAME = "glossary.json"

#: 解析失败后的重试上限（总调用数 = 1 + 本值）。一次足够——同一个模型连错两次，
#: 第三次多半还是错，而 survey 吃的是全文 token，重试很贵。
DEFAULT_MAX_RETRIES = 1

# 阶段状态。
OK = "ok"
DEGRADED = "degraded"
FAILED = "failed"

#: 参考文献 / 文末标记：命中即从此处截断通读输入。
_TAIL_RE = re.compile(
    r"\\begin\s*\{thebibliography\}"
    r"|\\bibliography\s*\{"
    r"|\\printbibliography"
    r"|\\begin\s*\{references\}"
    r"|\\end\s*\{document\}"
)

#: 关节④：`complete(prompt, text, model) -> text`（见 `tongtu.agent.Complete`）。
CompleteFn = Callable[..., str]


# ------------------------------------------------------------------ prompt 资产


def load_prompt(skill_dir: str | os.PathLike[str] | None = None) -> str:
    """读通读 prompt 文本（`skill/survey/SKILL.md`，经 :mod:`tongtu.prompts` 装载）。

    代码里不硬编码长串规则：改文案不必动代码，改代码不必重述文案（架构决策 1——SKILL
    降级为 prompt 资产）。查找链见 :func:`tongtu.prompts.find_skill_dir`。
    """
    return prompts.load(PROMPT_NAME, skill_dir=skill_dir)


def build_prompt(prompt: str, errors: Sequence[str] = ()) -> str:
    """组装提示词；`errors` 非空时把上一轮的解析错误喂回去（重试用）。"""
    if not errors:
        return prompt
    return (
        prompt
        + "\n\n---\n\n上一次的输出没能解析成 JSON，原因如下。请**只**输出一个完整的 "
        "JSON 对象，不要代码块围栏、不要解释文字，注意闭合所有括号：\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n"
    )


# ------------------------------------------------------------------ 通读输入


def _normalize_blocks(blocks) -> tuple[list[Block], list[Caption]]:
    """接受 `MaskResult` 或 blocks.json 的 dict（与 unmask 同一套入参约定）。"""
    if isinstance(blocks, MaskResult):
        return list(blocks.blocks), list(blocks.captions)
    if isinstance(blocks, Mapping):
        return (
            [b if isinstance(b, Block) else Block.from_json(b) for b in blocks.get("blocks", ())],
            [
                c if isinstance(c, Caption) else Caption.from_json(c)
                for c in blocks.get("captions", ())
            ],
        )
    raise TypeError(f"无法识别的块清单类型：{type(blocks).__name__}")


def cut_offset(masked: str) -> int:
    """通读输入的切点：附录起点与参考文献起点中更靠前的那个（都没有则全文）。"""
    limit = len(masked)
    for para in split_paragraphs(masked):
        if para.is_appendix:
            limit = min(limit, para.start)
            break
    match = _TAIL_RE.search(masked)
    if match is not None:
        limit = min(limit, match.start())
    return limit


def reading_view(masked: str, blocks) -> str:
    """通读输入 = 选择性回填视图，且已剔除附录与参考文献（架构 §3 survey 行）。"""
    return survey_view(masked[: cut_offset(masked)], blocks).strip() + "\n"


# ------------------------------------------------------------------ 原文照录


def _slot_text(captions: Sequence[Caption], kind: str) -> str:
    return next((c.text.strip() for c in captions if c.kind == kind and c.text.strip()), "")


def paper_facts(masked: str, blocks) -> tuple[str, str]:
    """`(标题, 摘要)`，**原文照录**（架构 §3：abstract 用原文而非译文）。

    先看 mask 抽出的 CAP 槽位（前导区里的 `\\title` / abstract 走这条）；没有就把掩码流
    完整回填成原文（`unmask(masked, blocks) == flat.tex`，由 mask 的往返自检担保），再按
    词法定位 `\\title{…}`、`\\begin{abstract}…\\end{abstract}` 或 `\\abstract{…}`——正文里
    的标题与摘要（revtex / IEEEtran 惯例）走这条。
    """
    _, captions = _normalize_blocks(blocks)
    title = _slot_text(captions, "title")
    abstract = _slot_text(captions, "abstract")
    if title and abstract:
        return title, abstract

    try:
        original = unmask(masked, blocks, strict=False)
    except Exception:  # noqa: BLE001 —— 取不到原文只是少两个字段，不该拖垮 survey
        return title, abstract

    verbatim = load_environment_table().verbatim_envs
    lexer = Lexer(original, verbatim_envs=verbatim)
    for tok in lexer:
        if title and abstract:
            break
        if tok.kind == "control":
            cs = original[tok.start : tok.end]
            if cs == "\\title" and not title:
                group = read_group(original, skip_optionals(original, tok.end))
                if group is not None:
                    title, lexer.pos = group[0].strip(), group[1]
            elif cs == "\\abstract" and not abstract:
                # revtex 的老写法 `\abstract{…}`（环境形式在下一分支）
                group = read_group(original, skip_optionals(original, tok.end))
                if group is not None:
                    abstract, lexer.pos = group[0].strip(), group[1]
            continue
        if tok.kind == "begin" and tok.name == "abstract" and not abstract:
            try:
                end = find_env_end(original, tok.start, "abstract", verbatim)
            except TexLexError:
                continue
            inner_end = original.rfind("\\end", tok.end, end)
            abstract, lexer.pos = original[tok.end : inner_end].strip(), end
    return title, abstract


# ------------------------------------------------------------------ 章节骨架


def skeleton_sections(masked: str) -> list[dict]:
    """从掩码流的标题命令扫出章节树（确定性，不需要模型）。

    标题**照录原文**，`summary` 留空——降级路径的 brief 因此仍有真实的结构信息，
    逐块翻译至少知道自己身处哪一节、全文有哪些节。附录的节也收进来并标 `is_appendix`
    （它们不进通读输入，但仍要翻译）。
    """
    headings = [p.heading for p in split_paragraphs(masked) if p.heading is not None]
    if not headings:
        return []
    base = min(h.level for h in headings)
    roots: list[dict] = []
    stack: list[tuple[int, dict]] = []  # (原始 level, 节点)
    for heading in headings:
        node: dict = {"title": heading.title, "summary": "", "level": heading.level - base + 1}
        if heading.numbered and heading.path:
            node["number"] = heading.path[-1]
        if heading.is_appendix:
            node["is_appendix"] = True
        while stack and stack[-1][0] >= heading.level:
            stack.pop()
        if stack:
            stack[-1][1].setdefault("children", []).append(node)
        else:
            roots.append(node)
        stack.append((heading.level, node))
    return roots


# ------------------------------------------------------------------ JSON 解析


_FENCE_RE = re.compile(r"```[a-zA-Z]*\n?")

#: 决策对象里认得的字段。一个都不含 = 这不是我们要的那个对象（`\LaTeX{}` 也是一对
#: 花括号，恰好还是合法 JSON——所以「解析成功」本身不构成证据）。
KNOWN_KEYS = frozenset(
    {"paper", "sections", "notation", "naming_conventions", "style", "terms", "do_not_translate"}
)


def parse_json_object(text: str, *, known_keys: frozenset[str] = KNOWN_KEYS) -> dict:
    """从模型输出里抠出决策 JSON 对象（防御性）。

    容忍：markdown 代码块围栏、JSON 前后的解释性文字、对象后面的多余内容、JSON 之前
    出现的其它花括号。不容忍：截断（括号没闭合）——半个 JSON 里的字段真假不可分，
    宁可把错误喂回去重试一次。
    """
    if not isinstance(text, str) or not text.strip():
        raise SurveyParseError("模型没有返回任何内容")
    body = _FENCE_RE.sub("", text).replace("```", "")
    if "{" not in body:
        raise SurveyParseError("输出里找不到 JSON 对象的起始 '{'")

    last = "输出里没有可用的 JSON 对象"
    truncated = False
    start = body.find("{")
    while start >= 0:
        end = _match_brace(body, start)
        if end is None:
            truncated = True
            break
        try:
            data = json.loads(body[start : end + 1])
        except json.JSONDecodeError as exc:
            last = f"JSON 解析失败：{exc}"
        else:
            if isinstance(data, dict) and known_keys & set(data):
                return data
            last = "解析出的 JSON 对象里没有任何已知字段（sections / terms / …）"
        start = body.find("{", start + 1)
    if truncated:
        raise SurveyParseError("JSON 对象没有闭合（疑似被截断）——请输出完整的 JSON")
    raise SurveyParseError(last)


def _match_brace(text: str, start: int) -> int | None:
    """从 `text[start] == '{'` 起找配对的 `}`，跳过字符串字面量与转义。"""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


# ------------------------------------------------------------------ brief 组装


def _text(value, limit: int = 4000) -> str:
    if isinstance(value, str):
        return value.strip()[:limit]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _sections(raw, depth: int = 1) -> list[dict]:
    """按 `brief.schema.json` 的 `section` 白名单清洗模型给的章节树（丢未知字段）。"""
    out: list[dict] = []
    if not isinstance(raw, list) or depth > 6:
        return out
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        title = _text(item.get("title"), 500)
        if not title:
            continue
        node: dict = {"title": title, "summary": _text(item.get("summary"))}
        for key in ("id", "number"):
            value = _text(item.get(key), 100)
            if value:
                node[key] = value
        level = item.get("level")
        node["level"] = level if isinstance(level, int) and level >= 1 else depth
        if item.get("is_appendix") is True:
            node["is_appendix"] = True
        children = _sections(item.get("children"), depth + 1)
        if children:
            node["children"] = children
        out.append(node)
    return out


def _notation(raw) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        symbol, meaning = _text(item.get("symbol"), 200), _text(item.get("meaning"), 500)
        if not symbol or not meaning:
            continue
        entry = {"symbol": symbol, "meaning": meaning}
        first_seen = _text(item.get("first_seen"), 100)
        if first_seen:
            entry["first_seen"] = first_seen
        out.append(entry)
    return out


def _naming(raw) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name, convention = _text(item.get("name"), 200), _text(item.get("convention"), 500)
        if not name or not convention:
            continue
        entry = {"name": name, "convention": convention}
        note = _text(item.get("note"), 500)
        if note:
            entry["note"] = note
        out.append(entry)
    return out


def _style(raw) -> dict:
    if not isinstance(raw, Mapping):
        return {}
    style: dict = {}
    for key in ("tone", "audience"):
        value = _text(raw.get(key), 500)
        if value:
            style[key] = value
    notes = [_text(n, 500) for n in raw.get("notes", []) if _text(n, 500)] if isinstance(
        raw.get("notes"), list
    ) else []
    if notes:
        style["notes"] = notes
    return style


def skeleton_brief(
    masked: str,
    blocks,
    *,
    arxiv_id: str | None = None,
    model: str = "",
    view_hash: str = "",
) -> dict:
    """降级骨架：纯确定性的 brief（abstract 原文照录 + 章节树，其余留空）。"""
    return build_brief(
        {},
        masked=masked,
        blocks=blocks,
        arxiv_id=arxiv_id,
        model=model,
        view_hash=view_hash,
    )


def build_brief(
    decision: Mapping,
    *,
    masked: str,
    blocks,
    arxiv_id: str | None = None,
    model: str = "",
    view_hash: str = "",
) -> dict:
    """把模型决策（可为空 dict = 降级骨架）组装成 `brief.json` 的内容。

    程序侧掌握的字段不交给模型：`abstract` 与 `paper.title` 原文照录，章节树在模型没给
    出时回落到确定性骨架（:func:`skeleton_sections`）。
    """
    title, abstract = paper_facts(masked, blocks)
    raw_paper = decision.get("paper") if isinstance(decision.get("paper"), Mapping) else {}

    paper: dict = {}
    if arxiv_id:
        paper["arxiv_id"] = arxiv_id
    if title or _text(raw_paper.get("title"), 500):
        paper["title"] = title or _text(raw_paper.get("title"), 500)
    authors = [
        _text(a, 200) for a in raw_paper.get("authors", []) if _text(a, 200)
    ] if isinstance(raw_paper.get("authors"), list) else []
    if authors:
        paper["authors"] = authors
    category = _text(raw_paper.get("primary_category"), 100)
    if category:
        paper["primary_category"] = category

    brief: dict = {
        "contract_version": CONTRACT_VERSION,
        "abstract": abstract,
        "sections": _sections(decision.get("sections")) or skeleton_sections(masked),
    }
    if paper:
        brief["paper"] = paper
    for key, value in (
        ("notation", _notation(decision.get("notation"))),
        ("naming_conventions", _naming(decision.get("naming_conventions"))),
        ("style", _style(decision.get("style"))),
    ):
        if value:
            brief[key] = value
    generated: dict = {"prompt_version": prompt_version(), "generated_at": _now()}
    if model:
        generated["model_id"] = model
    if view_hash:
        generated["input_hash"] = view_hash
    brief["generated_by"] = generated
    return brief


def brief_hash(brief: Mapping) -> str:
    """brief 的**内容** hash（进块级缓存 key 的 `brief_hash`，架构 §4）。

    刻意排除 `generated_by`：那里有生成时间戳，把它算进去等于「survey 一重跑就全量重
    翻」，而架构 §4 明说重翻的触发条件是 brief **内容**变化。
    """
    payload = {k: v for k, v in brief.items() if k != "generated_by"}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def render_brief(brief: Mapping, *, max_sections: int = 60) -> str:
    """把 brief 渲染成逐块翻译提示词里的那段全局上下文（架构 §3 translate 行）。"""
    lines: list[str] = []
    paper = brief.get("paper") if isinstance(brief.get("paper"), Mapping) else {}
    if paper.get("title"):
        lines.append(f"标题（原文）：{paper['title']}")
    if brief.get("abstract"):
        lines.append(f"摘要（原文，勿重译）：{brief['abstract']}")

    rendered: list[str] = []

    def walk(nodes, depth: int = 0) -> None:
        for node in nodes:
            if len(rendered) >= max_sections or not isinstance(node, Mapping):
                continue
            number = f"{node.get('number')} " if node.get("number") else ""
            summary = f"：{node['summary']}" if node.get("summary") else ""
            rendered.append(f"{'  ' * depth}- {number}{node.get('title', '')}{summary}")
            walk(node.get("children", []), depth + 1)

    walk(brief.get("sections", []))
    if rendered:
        lines.append("章节结构与各节摘要：\n" + "\n".join(rendered))
    if brief.get("notation"):
        lines.append(
            "记号约定：\n"
            + "\n".join(f"- {n['symbol']} = {n['meaning']}" for n in brief["notation"])
        )
    if brief.get("naming_conventions"):
        lines.append(
            "命名约定：\n"
            + "\n".join(f"- {n['name']}：{n['convention']}" for n in brief["naming_conventions"])
        )
    style = brief.get("style") if isinstance(brief.get("style"), Mapping) else {}
    if style:
        bits = [style[k] for k in ("tone", "audience") if style.get(k)]
        bits += list(style.get("notes", []))
        if bits:
            lines.append("文风基调：\n" + "\n".join(f"- {b}" for b in bits))
    return "\n\n".join(lines)


# ------------------------------------------------------------------ 结果


@dataclass(frozen=True)
class SurveyResult:
    """survey 阶段的结构化结果。`degraded` 为真即走了确定性骨架路径。"""

    status: str
    brief: dict = field(default_factory=dict)
    glossary: Glossary = field(default_factory=empty_glossary)
    view: str = ""
    view_hash: str = ""
    attempts: int = 0
    terms_added: int = 0
    do_not_translate_added: int = 0
    warnings: tuple[str, ...] = ()
    message: str = ""

    @property
    def ok(self) -> bool:
        """降级也算成功——brief 是增益不是门禁（架构 §3：survey 失败不阻塞流水线）。"""
        return self.status in (OK, DEGRADED)

    @property
    def degraded(self) -> bool:
        return self.status == DEGRADED

    @property
    def brief_hash(self) -> str:
        return brief_hash(self.brief) if self.brief else ""

    @property
    def brief_text(self) -> str:
        """逐块翻译要注入的那段全局上下文。"""
        return render_brief(self.brief) if self.brief else ""

    def to_json(self) -> dict:
        """manifest / report 用的摘要（不含 brief 正文——那是 brief.json 的活）。"""
        data: dict = {
            "status": self.status,
            "degraded": self.degraded,
            "attempts": self.attempts,
            "prompt_version": prompt_version(),
            "brief_hash": self.brief_hash,
            "view_chars": len(self.view),
            "sections": len(self.brief.get("sections", ())),
            "notation": len(self.brief.get("notation", ())),
            "terms": len(self.glossary.terms),
            "do_not_translate": len(self.glossary.do_not_translate),
            "terms_added": self.terms_added,
            "do_not_translate_added": self.do_not_translate_added,
            "style_version": self.glossary.style_version,
        }
        if self.warnings:
            data["warnings"] = list(self.warnings)
        if self.message:
            data["message"] = self.message
        return data


# ------------------------------------------------------------------ 阶段入口


def survey(
    masked: str,
    blocks,
    *,
    complete: CompleteFn | None = None,
    glossary: Glossary | None = None,
    model: str = "",
    arxiv_id: str | None = None,
    skill_dir: str | os.PathLike[str] | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> SurveyResult:
    """通读一次，产出 brief 与术语决策表（关节④）。

    :param masked: mask 阶段的掩码流。
    :param blocks: `MaskResult` 或 blocks.json 的 dict。
    :param complete: 关节④ 原语；None 或不可用时直接走降级骨架（不报错）。
    :param glossary: 三层合并后的**输入表**；agent 的新决策叠在它上面，用户条目优先。
    :param model: 模型标识，进 brief 的 `generated_by`。
    :param skill_dir: prompt 资产目录，默认查 `skill/`（见 `tongtu.prompts`）。
    :param max_retries: 解析失败后的重试上限（总调用数 = 1 + 本值）。

    出口判据是机械的：brief 与决策表都要过 `docs/schemas/` 的 schema 校验。模型那一路
    失败一律降级为确定性骨架，`status=degraded`，**不阻塞流水线**。
    """
    base = glossary if glossary is not None else empty_glossary()
    warnings: list[str] = []
    view = reading_view(masked, blocks)
    view_hash = hashlib.sha256(view.encode("utf-8")).hexdigest()

    decision, attempts, errors = _ask(
        complete,
        view,
        model=model,
        skill_dir=skill_dir,
        max_retries=max_retries,
        warnings=warnings,
    )

    def assemble(source: Mapping | None) -> dict:
        return build_brief(
            source or {},
            masked=masked,
            blocks=blocks,
            arxiv_id=arxiv_id,
            model=model,
            view_hash=view_hash,
        )

    brief = assemble(decision)
    brief_errors = _schema_errors(brief, "brief", warnings)
    if brief_errors and decision is not None:
        # 模型那一路组装出的 brief 不合契约 → 退回确定性骨架（它只依赖我们自己的代码）。
        warnings.append("模型产出的 brief 不合 schema，已退回确定性骨架：" + brief_errors[0])
        errors = [*errors, *brief_errors]
        decision = None
        brief = assemble(None)
        brief_errors = _schema_errors(brief, "brief", warnings)

    decided = with_agent_decisions(
        base,
        terms=_list_of(decision, "terms"),
        do_not_translate=_list_of(decision, "do_not_translate"),
    )
    if _schema_errors(decided.to_json(), "glossary", warnings):
        warnings.append("agent 的术语决策不合 schema，本篇只用用户输入表")
        decided = base
    added_terms = len(decided.terms) - len(base.terms)
    added_dnt = len(decided.do_not_translate) - len(base.do_not_translate)

    if brief_errors:
        # 骨架都不合契约 = 本模块自己的 bug，这才是真失败（不许悄悄放过）。
        return SurveyResult(
            status=FAILED,
            brief=brief,
            glossary=decided,
            view=view,
            view_hash=view_hash,
            attempts=attempts,
            warnings=tuple(warnings),
            message="brief 不通过 schema 校验：" + brief_errors[0],
        )

    degraded = decision is None
    return SurveyResult(
        status=DEGRADED if degraded else OK,
        brief=brief,
        glossary=decided,
        view=view,
        view_hash=view_hash,
        attempts=attempts,
        terms_added=added_terms,
        do_not_translate_added=added_dnt,
        warnings=tuple(warnings),
        message=(
            "通读未取得可用 JSON，brief 降级为确定性骨架（章节树 + 原文摘要），术语零增补："
            + (errors[-1] if errors else "关节④不可用")
            if degraded
            else ""
        ),
    )


def _list_of(decision: Mapping | None, key: str) -> tuple:
    """决策里的某个数组字段（缺失、类型不对、整体降级时一律当空）。"""
    value = (decision or {}).get(key)
    return tuple(value) if isinstance(value, list) else ()


def _ask(
    complete: CompleteFn | None,
    view: str,
    *,
    model: str,
    skill_dir: str | os.PathLike[str] | None,
    max_retries: int,
    warnings: list[str],
) -> tuple[dict | None, int, list[str]]:
    """拉起关节④，至多 1 + `max_retries` 次；返回 (决策 dict 或 None, 调用次数, 错误)。"""
    if complete is None or not callable(complete):
        warnings.append("没有可用的 complete 原语（关节④），survey 直接走降级骨架")
        return None, 0, ["关节④不可用"]
    try:
        prompt_text = load_prompt(skill_dir)
    except PromptError as exc:
        warnings.append(f"读不到通读 prompt 资产（{exc}），survey 直接走降级骨架")
        return None, 0, [str(exc)]

    errors: list[str] = []
    attempts = 0
    while attempts <= max_retries:
        attempts += 1
        try:
            raw = complete(build_prompt(prompt_text, errors[-1:]), view, model or None)
        except Exception as exc:  # noqa: BLE001 —— 关节炸了不该拖垮确定性骨架
            errors.append(f"关节④调用失败（{type(exc).__name__}）：{exc}")
            warnings.append(errors[-1])
            continue
        try:
            return parse_json_object(raw), attempts, errors
        except SurveyParseError as exc:
            errors.append(str(exc))
            warnings.append(f"第 {attempts} 次通读输出解析失败：{exc}")
    return None, attempts, errors


def _schema_errors(instance, name: str, warnings: list[str]) -> list[str]:
    """过 schema 校验；schema 目录不可用时记警告并放行（不拿环境问题卡产物）。"""
    try:
        return schema_check(instance, name)
    except SchemaError as exc:
        warnings.append(f"跳过 {name} 的 schema 校验：{exc}")
        return []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
