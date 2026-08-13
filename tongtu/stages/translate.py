"""translate 阶段驱动器：块循环 + 上下文组装 + validate 内环（架构 §3 translate 行、关节⑤）。

出口判据是机械的：**validate 全绿**（占位符 multiset / 控制序列 multiset / 括号与 `$`
计数 / 段落数）。本模块只做驱动，判断全在关节⑤（`complete` 原语）里，裁决全在
:mod:`tongtu.validate` 手里——agent 的「我检查过了」在这一层没有任何效力（架构 §2 原则 1）。

    块循环 → 组装上下文 → 查缓存 → complete(prompt, text, model) → validate
                                       ↑                              │失败
                                       └──── 把 format_errors 喂回 ────┘（至多 max_retries 次）

重试用尽 → **回退原文**并记 `status="fallback"`（架构 §3 compile 行同一条纪律：保证流水线
永远能往下走，回退详情进 report），不抛栈、不终止。

## 块的首尾空白由驱动器保管

`ChunkPlan` 的块首尾相接、拼接恒等于掩码流（`plan.reassemble() == masked`），compile 的
二分与回填正依赖这条。故送进 agent 的是 `chunk.body`（去掉首尾空白的块正文），拿回译文后
由驱动器把原来的首尾空白**原样**接回去：

    translation = lead + complete(prompt, body) + trail

于是「译块拼接 == 掩码流的形状」由代码保证，而不是指望模型不动空白。validate 也只比对
body 与译文 body——空白不参与判断，段落数这一层才不会被首尾换行搅混。

## 上下文从哪来

`brief` / `glossary` / `style_version` 都是 **survey 阶段**的产物（架构 §3 survey 行）：

* `brief`——全文纲要的渲染文本（`tongtu.stages.survey.render_brief`），进提示词；它的内容
  hash（`brief_hash`）进 cache key；
* `glossary`——术语决策表的 `{可命中写法: 译法}` 映射（`tongtu.glossary.term_map`）。命中
  判定用的是 `tongtu.glossary.hit_terms`——与 `relevant_terms` 同一份实现，两处各写一遍
  必然漂，而漂了就意味着缓存 key 与提示词不是一回事；
* `style_version`——术语表第三段的文风规则版本号（架构 §8），bump 即全量重翻；
* `cache`——块级翻译缓存（`{cache_key: 译文正文}` 形状的可变映射）。key 的构成按架构 §4
  算全（见 :func:`cache_key`）；它的装载与写回（`out/chunks.json` 这份权威翻译记忆）住在
  :mod:`tongtu.memory`——本模块只查、只写这个映射，不认识工作目录。

**刻意不传前块译文**（架构 §3 末）：邻域上下文只用原文，否则缓存失效沿块链级联、并行
翻译退化为串行。

## 内环是可复用的

「组装提示词 → 调关节⑤ → validate → 把错误喂回去重试」这一圈抽成了
:func:`translate_body`，块循环与 compile 的**坏段重译**（关节⑤复用，
:func:`retranslate_segment`）走同一份实现——出口判据在两处必须是同一个 validate，否则
「编译回环救活的那一段」就绕过了机械校验。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, MutableMapping, Sequence

from .. import CONTRACT_VERSION, prompts
from ..glossary import hit_terms
from ..validate import Error, check, format_errors, summarize
from .chunk import Chunk, ChunkPlan
from .compile import TranslatedChunk

__all__ = [
    "CACHED",
    "Attempt",
    "Context",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_NEIGHBOR_PARAGRAPHS",
    "FAILED",
    "FALLBACK",
    "JOINT",
    "OK",
    "OK_WITH_FALLBACK",
    "PROMPT_TAIL",
    "PROMPT_VERSION",
    "Progress",
    "STATUSES",
    "STYLE_VERSION",
    "TRANSLATED",
    "ChunkTranslation",
    "TranslateResult",
    "assemble_context",
    "build_prompt",
    "cache_key",
    "normalize_source",
    "prompt_rules",
    "retranslate_segment",
    "split_affixes",
    "translate",
    "translate_body",
]

# ------------------------------------------------------------------ 常量

#: 本阶段的 agent 关节（`tongtu.agent.JOINTS` 的 ⑤）。
JOINT = "translate"

#: validate 失败后的重试上限（**首次调用之外**的次数；总调用数 = 1 + max_retries）。
DEFAULT_MAX_RETRIES = 3

#: 邻域原文的段数（前节末段 / 后节首段各取几段）。附录 B 开放问题 5：M3 用 fixture 校准。
DEFAULT_NEIGHBOR_PARAGRAPHS = 1

#: prompt 资产版本号。**单一来源在 `tongtu.prompts`**（规则住在 `skill/`，版本号跟规则走）。
PROMPT_VERSION = prompts.PROMPT_VERSION

#: 全局文风规则版本号（架构 §4：bump 即全量重翻，是显式有意的行为）。同上，单一来源。
STYLE_VERSION = prompts.STYLE_VERSION

# 阶段状态。
OK = "ok"
OK_WITH_FALLBACK = "ok_with_fallback"
FAILED = "failed"

STATUSES: tuple[str, ...] = (OK, OK_WITH_FALLBACK, FAILED)

# 块状态。`chunks.schema.json` 的 status 枚举只有前两个，`CACHED` 是驱动侧的运行时区分
# （缓存命中的块序列化时仍是 translated），也是事件流 chunk_progress 的一个取值。
TRANSLATED = "translated"
FALLBACK = "fallback"
CACHED = "cached"

#: 回退原因（`chunks.schema.json` 的 `fallback_reason`）。
REASON_VALIDATE = "validate_failed"
REASON_AGENT = "agent_unavailable"

#: 空白规范化（cache key 用）：连续空白折成单个空格。
_WS_RE = re.compile(r"\s+")


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True)
class Progress:
    """一条块进度，字段与 `events.schema.json` 的 `chunk_progress` 对齐。"""

    id: str
    index: int
    total: int
    status: str  # started / cached / translated / retry / fallback / failed
    attempt: int = 1
    reason: str | None = None


#: 块进度回调。编排器拿它发 `--json` 事件流；`None` 表示不关心。
ProgressFn = Callable[[Progress], None]

#: 关节⑤：`complete(prompt, text, model) -> text`（见 `tongtu.agent.Complete`）。
CompleteFn = Callable[..., str]


@dataclass(frozen=True)
class Context:
    """一块的翻译上下文（架构 §3：brief + 命中术语 + 邻域**原文**）。"""

    before: str = ""
    """前一块的末尾若干段原文。"""

    after: str = ""
    """后一块的开头若干段原文。"""

    terms: tuple[tuple[str, str], ...] = ()
    """本块命中的术语条目（已排序），进 cache key。"""

    brief: str = ""
    """全文纲要的渲染文本（由 survey 阶段提供，见 `tongtu.stages.survey.render_brief`）。"""

    @property
    def neighbor_src(self) -> str:
        return f"{self.before}\n\n{self.after}".strip()

    @property
    def neighbor_hash(self) -> str:
        return hashlib.sha256(self.neighbor_src.encode("utf-8")).hexdigest()

    def to_json(self) -> dict:
        return {
            "neighbor_hash": self.neighbor_hash,
            "terms": [{"term": t, "translation": v} for t, v in self.terms],
        }


@dataclass(frozen=True)
class ChunkTranslation:
    """一块的翻译结果。字段与 `chunks.schema.json` 的 `chunk` 条目对齐。"""

    id: str
    index: int
    source: str
    """块源码（**含首尾空白**的 `chunk.text`）——拼接恒等于掩码流。"""

    translation: str
    """译文（同样含首尾空白）；`status == FALLBACK` 时即原文。"""

    status: str = TRANSLATED
    section_path: tuple[str, ...] = ()
    style_version: str = STYLE_VERSION
    """生效的文风规则版本号（来自术语表第三段，架构 §8）——它进了本块的 cache_key。"""

    section: str | None = None
    attempts: int = 1
    cached: bool = False
    cache_key: str = ""
    src_hash: str = ""
    neighbor_hash: str = ""
    terms: tuple[tuple[str, str], ...] = ()
    paragraph_count: int = 0
    model: str = ""
    fallback_reason: str | None = None
    errors: tuple[Error, ...] = ()
    """最后一次失败的 validate 错误（回退时非空）——进 report 的失败统计。"""

    translated_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == TRANSLATED

    @property
    def unit(self) -> TranslatedChunk:
        """compile 阶段的输入形态（`tongtu.stages.compile.TranslatedChunk`）。"""
        return TranslatedChunk(
            id=self.id,
            source=self.source,
            translation=self.translation,
            section=self.section,
        )

    def to_json(self) -> dict:
        """`chunks.schema.json` 的一条 `chunks[]`（缓存命中仍序列化为 translated）。"""
        data: dict = {
            "id": self.id,
            "index": self.index,
            "src": self.source,
            "src_hash": self.src_hash,
            "cache_key": self.cache_key,
            "translation": self.translation,
            "status": TRANSLATED if self.status == CACHED else self.status,
            "attempts": self.attempts,
            "paragraph_count": self.paragraph_count,
            "prompt_version": PROMPT_VERSION,
            "style_version": self.style_version,
        }
        if self.section_path:
            data["section_path"] = list(self.section_path)
        if self.model:
            data["model_id"] = self.model
        if self.neighbor_hash:
            data["neighbor_hash"] = self.neighbor_hash
        if self.terms:
            data["terms"] = [{"term": t, "translation": v} for t, v in self.terms]
        if self.fallback_reason:
            data["fallback_reason"] = self.fallback_reason
        if self.translated_at:
            data["translated_at"] = self.translated_at
        return data


@dataclass(frozen=True)
class TranslateResult:
    """translate 阶段的结构化结果。"""

    status: str
    chunks: tuple[ChunkTranslation, ...] = ()
    model: str = ""
    brief_hash: str = ""
    style_version: str = STYLE_VERSION
    failures_by_check: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (OK, OK_WITH_FALLBACK)

    @property
    def units(self) -> tuple[TranslatedChunk, ...]:
        """compile 的输入：译块序列（拼接后即译文掩码流）。"""
        return tuple(c.unit for c in self.chunks)

    @property
    def stream(self) -> str:
        return "".join(c.translation for c in self.chunks)

    @property
    def fallbacks(self) -> tuple[ChunkTranslation, ...]:
        return tuple(c for c in self.chunks if c.status == FALLBACK)

    @property
    def cache_hits(self) -> int:
        """翻译记忆命中、未拉起关节⑤的块数（架构 §4 的块级缓存）。"""
        return sum(1 for c in self.chunks if c.cached)

    @property
    def cache_misses(self) -> int:
        """未命中、真的调了一次（或多次）模型的块数。命中 + 未命中 = 块数。"""
        return sum(1 for c in self.chunks if not c.cached)

    def to_chunks_json(self) -> dict:
        """按 `docs/schemas/chunks.schema.json` 组装翻译记忆（export 阶段照此落盘）。"""
        data: dict = {
            "contract_version": CONTRACT_VERSION,
            "style_version": self.style_version,
            "prompt_version": PROMPT_VERSION,
            "chunks": [c.to_json() for c in self.chunks],
        }
        if self.model:
            data["model_id"] = self.model
        if self.brief_hash:
            data["brief_hash"] = self.brief_hash
        return data

    def to_json(self) -> dict:
        """manifest / report 用的摘要（不含译文正文——那是 chunks.json 的活）。"""
        data: dict = {
            "status": self.status,
            "chunk_count": len(self.chunks),
            "translated": sum(1 for c in self.chunks if c.status != FALLBACK),
            "fallback": len(self.fallbacks),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "attempts": sum(c.attempts for c in self.chunks),
            "model": self.model,
            "prompt_version": PROMPT_VERSION,
            "style_version": self.style_version,
        }
        if self.failures_by_check:
            data["failures_by_check"] = dict(self.failures_by_check)
        if self.fallbacks:
            data["fallback_chunks"] = [c.id for c in self.fallbacks]
        if self.warnings:
            data["warnings"] = list(self.warnings)
        if self.message:
            data["message"] = self.message
        return data


# ------------------------------------------------------------------ 纯函数


def split_affixes(text: str) -> tuple[str, str, str]:
    """把块文本拆成 `(首部空白, 正文, 尾部空白)`，`"".join(...) == text` 恒成立。"""
    if not text.strip():
        return ("", "", text)
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    return (text[:start], text[start:end], text[end:])


def normalize_source(text: str) -> str:
    """cache key 用的空白规范化（架构 §4 的 `norm(chunk_src)`）。"""
    return _WS_RE.sub(" ", text).strip()


def cache_key(
    source: str,
    *,
    neighbor_src: str = "",
    terms: Iterable[tuple[str, str]] = (),
    brief_hash: str = "",
    style_version: str = STYLE_VERSION,
    prompt_version: str = PROMPT_VERSION,
    model: str = "",
) -> str:
    """块级翻译缓存的 key（架构 §4 的公式，逐项照搬）。

        key = hash( norm(chunk_src) + neighbor_src + relevant_terms(chunk)
                  + brief_hash + style_version + prompt_version + model_id )

    术语条目按块内命中计入——改一个词只失效含它的块；全局文风规则单列 `style_version`，
    bump 即全量重翻（显式有意的行为）。各段之间插入分隔符，避免拼接歧义。
    """
    digest = hashlib.sha256()
    parts = (
        normalize_source(source),
        neighbor_src,
        "\n".join(f"{term}\t{value}" for term, value in sorted(terms)),
        brief_hash,
        style_version,
        prompt_version,
        model,
    )
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def assemble_context(
    chunk: Chunk,
    plan: ChunkPlan | None = None,
    *,
    brief: str | None = None,
    glossary: Mapping[str, str] | None = None,
    neighbor_paragraphs: int = DEFAULT_NEIGHBOR_PARAGRAPHS,
) -> Context:
    """组装一块的上下文：邻域**原文** + 命中术语（+ M3 起的 brief）。

    邻域取自 `plan.paragraphs`（章节级分块后即前节末段与后节首段）；没有 plan 时退化为
    空邻域——单阶段调试与 `retranslate` 走这条也不会崩。
    """
    before = after = ""
    if plan is not None and plan.paragraphs:
        count = max(0, neighbor_paragraphs)
        if count and chunk.prev_tail_para is not None:
            start = max(0, chunk.prev_tail_para - count + 1)
            before = "\n\n".join(
                p.text for p in plan.paragraphs[start : chunk.prev_tail_para + 1]
            )
        if count and chunk.next_head_para is not None:
            end = min(len(plan.paragraphs), chunk.next_head_para + count)
            after = "\n\n".join(p.text for p in plan.paragraphs[chunk.next_head_para : end])
    return Context(
        before=before,
        after=after,
        terms=hit_terms(chunk.body, glossary),
        brief=brief or "",
    )


def prompt_rules() -> str:
    """关节⑤的规则正文 = `skill/translate.md`（PHASE0 §3.4，经 :mod:`tongtu.prompts` 装载）。

    资产缺失时抛 :class:`tongtu.prompts.PromptError`——**故意让它响**：没有规则的「翻译」
    就是拿默认文风乱译一通，而缓存 key 里的 `prompt_version` 还写着规则已经生效。
    """
    return prompts.load(prompts.TRANSLATE)


#: 待翻译正文的引导行（`complete` 的 `text` 参数接在提示词之后）。
PROMPT_TAIL = "待翻译正文："


def build_prompt(
    context: Context, errors: Iterable[Error] = (), notes: Iterable[str] = ()
) -> str:
    """组装提示词：`skill/translate.md` 的规则 + 本块上下文 + 上一轮的校验错误（+ 补充说明）。

    **拼接而非 format**：规则里全是 `\\section{...}`、`⟦BLK-n⟧` 这类字面量，模板替换会炸。

    `notes` 是调用方自带的整段说明（坏段重译把编译错误从这里递进来），原样附在末尾——
    本函数不替调用方措辞。
    """
    blocks: list[str] = [prompt_rules()]
    if context.brief:
        blocks.append(f"全文纲要：\n{context.brief}")
    if context.terms:
        blocks.append(
            "术语（必须照此译）：\n"
            + "\n".join(f"- {term} → {value}" for term, value in context.terms)
        )
    if context.before:
        blocks.append(f"上文原文（仅供参考，不要翻译）：\n{context.before}")
    if context.after:
        blocks.append(f"下文原文（仅供参考，不要翻译）：\n{context.after}")
    errors = tuple(errors)
    if errors:
        blocks.append(
            "上一版译文没通过机械校验，请修正后重译（不要解释，直接给译文）：\n"
            + format_errors(errors)
        )
    blocks.extend(note for note in notes if note)
    blocks.append(PROMPT_TAIL)
    return "\n\n".join(blocks) + "\n"


# ------------------------------------------------------------------ validate 内环


@dataclass(frozen=True)
class Attempt:
    """一次「翻到 validate 全绿或重试用尽」的结果（:func:`translate_body` 的返回值）。"""

    translation: str | None
    """通过 validate 的译文正文；`None` = 重试用尽（调用方决定回退还是放弃）。"""

    attempts: int = 0
    errors: tuple[Error, ...] = ()
    """最后一次失败的 validate 错误（成功时为空）。"""

    reason: str = REASON_VALIDATE
    """失败原因（`chunks.schema.json` 的 `fallback_reason`）。"""

    @property
    def ok(self) -> bool:
        return self.translation is not None


def translate_body(
    body: str,
    *,
    complete: CompleteFn,
    context: Context | None = None,
    model: str = "",
    max_retries: int = DEFAULT_MAX_RETRIES,
    notes: Sequence[str] = (),
    on_retry: Callable[[int, str], None] | None = None,
) -> Attempt:
    """内环：调关节⑤，validate 不过就把错误喂回去重试，至多 `1 + max_retries` 次。

    这是**出口判据所在**：只有 :func:`tongtu.validate.check` 一条错误都挑不出来才算过。
    关节抛异常、返回空译文都只算一次失败的尝试（可重试），不抛给调用方——块循环与
    compile 的坏段重译都指望这一点。
    """
    if max_retries < 0:
        raise ValueError(f"max_retries 不得为负：{max_retries}")
    context = context if context is not None else Context()
    errors: tuple[Error, ...] = ()
    reason = REASON_VALIDATE
    attempt = 0
    while attempt <= max_retries:
        attempt += 1
        if attempt > 1 and on_retry is not None:
            on_retry(attempt, errors[0].message if errors else reason)
        prompt = build_prompt(context, errors, notes)
        try:
            candidate = complete(prompt, body, model or None)
        except Exception as exc:  # 关节炸了不该拖垮流水线：当作一次失败，可重试
            errors = (
                Error(check="agent", message=f"关节⑤调用失败（{type(exc).__name__}）：{exc}"),
            )
            reason = REASON_AGENT
            continue
        if not isinstance(candidate, str) or not candidate.strip():
            errors = (Error(check="agent", message="关节⑤返回了空译文"),)
            reason = REASON_AGENT
            continue
        found = tuple(check(body, candidate))
        if not found:
            return Attempt(translation=candidate, attempts=attempt)
        errors = found
        reason = REASON_VALIDATE
    return Attempt(translation=None, attempts=attempt, errors=errors, reason=reason)


def retranslate_segment(
    source: str,
    *,
    complete: CompleteFn,
    model: str = "",
    brief: str | None = None,
    glossary: Mapping[str, str] | None = None,
    detail: str = "",
    max_retries: int = 0,
) -> str | None:
    """坏段重译一次（关节⑤复用，compile 的 `retranslate` 回调形状）。

    与块循环共用 :func:`translate_body`，故**同一套 validate 仍是出口判据**——编译回环
    只是提出「这一段有问题」，它没有资格让一段译文绕过机械校验。首尾空白照旧由驱动器
    保管（`lead + 译文 + trail`），段落形状不因重译而变。

    `detail` 是编译日志里的第一个 `!` 错误，作为补充说明进提示词。返回 `None` = 这次没
    翻出可用的译文（调用方回退原文）。
    """
    lead, body, trail = split_affixes(source)
    if not body:
        return None
    notes = (
        (
            "上一版译文让 xelatex 编译失败了，请重译这一段并避开可能致错的写法"
            f"（占位符与控制序列必须原样保留）。编译器给的第一个错误：{detail}",
        )
        if detail
        else ()
    )
    outcome = translate_body(
        body,
        complete=complete,
        context=Context(terms=hit_terms(body, glossary), brief=brief or ""),
        model=model,
        max_retries=max_retries,
        notes=notes,
    )
    if outcome.translation is None:
        return None
    return lead + outcome.translation + trail


# ------------------------------------------------------------------ 阶段入口


def translate(
    chunks: ChunkPlan | Sequence[Chunk],
    *,
    complete: CompleteFn,
    model: str = "",
    brief: str | None = None,
    brief_hash: str = "",
    glossary: Mapping[str, str] | None = None,
    style_version: str = STYLE_VERSION,
    cache: MutableMapping[str, str] | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    neighbor_paragraphs: int = DEFAULT_NEIGHBOR_PARAGRAPHS,
    progress: ProgressFn | None = None,
) -> TranslateResult:
    """逐块翻译，validate 全绿才放行；重试用尽回退原文。

    :param chunks: `ChunkPlan`（推荐，有邻域上下文）或裸 `Chunk` 序列。
    :param complete: 关节⑤ 原语 `complete(prompt, text, model) -> text`。
    :param model: 模型标识，进 cache key 与 chunks.json。
    :param brief: survey 的全文纲要渲染文本（M3；本期传 None）。
    :param brief_hash: 纲要内容 hash，进 cache key（M3；本期传空）。
    :param glossary: 术语决策表 `{可命中写法: 译法}`（`tongtu.glossary.term_map`）。
    :param style_version: 生效的文风规则版本号（术语表第三段）；bump 即全量重翻。
    :param cache: 块级翻译缓存 `{cache_key: 译文正文}`；命中即免调用，翻成功的块写回。
        装载与落盘见 :mod:`tongtu.memory`（权威记忆是产物包里的 `chunks.json`）。
    :param max_retries: validate 失败后的重试上限；总调用数 = 1 + `max_retries`。
    :param progress: 块进度回调，编排器用它发 `chunk_progress` 事件。
    """
    plan = chunks if isinstance(chunks, ChunkPlan) else None
    items: tuple[Chunk, ...] = tuple(plan.chunks if plan is not None else chunks)
    if not items:
        return TranslateResult(status=FAILED, model=model, message="块清单为空，没有可翻译的内容")
    if max_retries < 0:
        raise ValueError(f"max_retries 不得为负：{max_retries}")

    total = len(items)
    results: list[ChunkTranslation] = []
    failures: dict[str, int] = {}
    warnings: list[str] = []

    def emit(chunk_id: str, index: int, status: str, attempt: int, reason: str | None) -> None:
        if progress is not None:
            progress(
                Progress(
                    id=chunk_id,
                    index=index,
                    total=total,
                    status=status,
                    attempt=attempt,
                    reason=reason,
                )
            )

    for index, chunk in enumerate(items):
        lead, body, trail = split_affixes(chunk.text)
        context = assemble_context(
            chunk,
            plan,
            brief=brief,
            glossary=glossary,
            neighbor_paragraphs=neighbor_paragraphs,
        )
        key = cache_key(
            chunk.text,
            neighbor_src=context.neighbor_src,
            terms=context.terms,
            brief_hash=brief_hash,
            style_version=style_version,
            model=model,
        )
        base = dict(
            id=chunk.id,
            index=index,
            source=chunk.text,
            section_path=chunk.section_path,
            section=chunk.section_titles[-1] if chunk.section_titles else None,
            cache_key=key,
            src_hash=hashlib.sha256(normalize_source(chunk.text).encode("utf-8")).hexdigest(),
            neighbor_hash=context.neighbor_hash,
            terms=context.terms,
            paragraph_count=chunk.paragraph_count,
            model=model,
            style_version=style_version,
            translated_at=_now(),
        )

        # 空块（只有空白的块理论上不存在，防御性处理）：原样通过，不打扰 agent。
        if not body:
            results.append(ChunkTranslation(translation=chunk.text, attempts=0, **base))
            emit(chunk.id, index, TRANSLATED, 0, None)
            continue

        if cache is not None and key in cache:
            results.append(
                ChunkTranslation(
                    translation=lead + cache[key] + trail,
                    status=CACHED,
                    attempts=0,
                    cached=True,
                    **base,
                )
            )
            emit(chunk.id, index, CACHED, 0, None)
            continue

        emit(chunk.id, index, "started", 1, None)
        outcome = translate_body(
            body,
            complete=complete,
            context=context,
            model=model,
            max_retries=max_retries,
            on_retry=lambda attempt, reason, _id=chunk.id, _i=index: emit(
                _id, _i, "retry", attempt, reason
            ),
        )

        if outcome.translation is not None:
            if cache is not None:
                cache[key] = outcome.translation
            results.append(
                ChunkTranslation(
                    translation=lead + outcome.translation + trail,
                    attempts=outcome.attempts,
                    **base,
                )
            )
            emit(chunk.id, index, TRANSLATED, outcome.attempts, None)
            continue

        # 重试用尽 → 回退原文（保证流水线继续；详情进 report）
        for name, count in summarize(outcome.errors).items():
            failures[name] = failures.get(name, 0) + count
        detail = outcome.errors[0].message if outcome.errors else "未知原因"
        warnings.append(
            f"块 {chunk.id} 重试 {outcome.attempts} 次仍未通过校验，回退原文：{detail}"
        )
        results.append(
            ChunkTranslation(
                translation=chunk.text,
                status=FALLBACK,
                attempts=outcome.attempts,
                fallback_reason=outcome.reason,
                errors=outcome.errors,
                **base,
            )
        )
        emit(chunk.id, index, FALLBACK, outcome.attempts, detail)

    fallbacks = [c for c in results if c.status == FALLBACK]
    return TranslateResult(
        status=OK_WITH_FALLBACK if fallbacks else OK,
        chunks=tuple(results),
        model=model,
        brief_hash=brief_hash,
        style_version=style_version,
        failures_by_check=failures,
        warnings=tuple(warnings),
        message=(
            f"{len(fallbacks)}/{total} 块回退原文（校验未通过）" if fallbacks else ""
        ),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
