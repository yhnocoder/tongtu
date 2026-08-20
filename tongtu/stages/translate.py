r"""translate 阶段驱动器：逐 chunk 组装上下文、调 `ask` 翻译、脚本驱动 validate 与重试。

translate 只读 `build/`、写 `build/` 与 `logs/`（每次 `ask` 调用的日志）。上游结论与三个输入
hash 从 chunk manifest 与 survey manifest 装载。四层 validate 的实现在 `tongtu/validation.py`，
术语命中复用 `tongtu/glossary.py` 的 `relevant_terms`，本模块管上下文组装、`ask` 调用与重试、
出口判定与落盘。设计是 `docs/stages/translate.md`。

前置条件：chunk manifest 缺失或不可解析，或状态是 ok 但任一 chunk 文件缺失 → 状态
`chunk_missing`；chunk 状态不是 ok → `chunk_not_ok`；survey manifest 缺失或不可解析，或状态
是 ok 但 `build/glossary.json`、`build/brief.json` 有缺（含不可解析）→ `survey_missing`；
survey 状态不是 ok → `survey_not_ok`。前置条件不满足同样写 translate manifest：驱动器不向
调用方抛栈，每次执行的结论都落盘。

翻译循环：`translatable_chars` 为 0 的纯 placeholder chunk 不调 `ask`，原文即译文；其余组装
上下文调 `ask`，拿回译文跑四层 validate，不通过就把校验错误附进提示词重新 ask，至多
`MAX_RETRIES` 次；仍不通过则该 chunk 回退原文。chunk 之间没有数据依赖（neighbors 取的是源
文本，不取译文），整个循环用标准库线程池并发，写盘收在主线程，结果按文档序落盘，产物与串行
执行逐字节相同。

出口判定：回退比例超过 `max_fallback_ratio` 判整体失败、不进入 compile——大面积回退产出的
PDF 大半仍是英文。判定在全部 chunk 跑完之后做，不提前中断：中断只会丢掉已经付过钱的译文。

重跑语义：复用的粒度是整个阶段，没有 chunk 级翻译记忆（理由见设计稿的复用粒度节）。已有
translate manifest 可解析、状态 ok、三个输入 hash（`chunks_sha256` 从 chunk manifest 转录，
`glossary_sha256` 与 `brief_sha256` 从 survey manifest 转录）与 `model_id`、`prompt_version`
一致、上次的回退比例仍在本次阈值之内、全部译文文件存在 → 跳过；失败状态不跳过；`force` 无视
已有结论整篇重翻。每次非跳过的执行开始先整目录删除 `build/translated/`。
"""

from __future__ import annotations

import hashlib
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .. import assets, chunking, glossary, manifests, validation, workdir
from ..agent import opencode
from ..artifacts.chunk import ChunkManifest, ChunkRecord, ChunkStatus
from ..artifacts.survey import BriefFile, GlossaryFile, SurveyManifest, SurveyStatus
from ..artifacts.translate import (
    ChunkTranslateRecord,
    ChunkTranslateStatus,
    TranslateManifest,
    TranslateStatus,
)
from ..chunking import Part
from .chunk import STAGE_NAME as CHUNK_STAGE_NAME
from .chunk import chunk_path
from .survey import STAGE_NAME as SURVEY_STAGE_NAME
from .survey import brief_path, glossary_file_path

#: 阶段名，也是 stage manifest 的文件名主干。
STAGE_NAME = "translate"

#: 译文文件所在目录，位于 build/ 之下；只存本阶段产物，故整目录删除是安全的。
TRANSLATED_DIRNAME = "translated"

#: 译文文件的扩展名，与 chunk 文件一致。
TRANSLATED_SUFFIX = ".tex"

#: 默认模型与推理强度。两者都由实测定，依据见 docs/models.md：muse-spark 与 deepseek-v4-pro
#: 的 validate 通过率相同而前者输出 token 更少；low 档格式遵循最好，medium 档空行被吞。
DEFAULT_MODEL = "muse-spark-1.2-contributor"
REASONING_EFFORT = "low"

#: validate 不通过后重新 ask 的次数上限，超过即该 chunk 回退原文。只有产出了可评判译文的尝试
#: 消耗这个额度：`ask` 报错与返回空译文的那次没有给出可评判的东西，把差异说明回灌给它没有意义。
MAX_RETRIES = 2

#: 一个 chunk 的 `ask` 调用次数硬上限。它大于 `MAX_RETRIES + 1`，差额留给不消耗重试额度的空返回
#: 与 `ask` 报错；没有这个上限，模型持续返回空译文时循环不会停。
MAX_ASK_CALLS = MAX_RETRIES + 3

#: 默认并发度。上限由 API 的速率限制决定而非本地核数，故是可覆盖的默认值而不是按核数推算。
DEFAULT_JOBS = 4

#: 默认的回退比例阈值，超过它 translate 整体判失败。
DEFAULT_MAX_FALLBACK_RATIO = 0.2

#: neighbors 取前一 chunk 末几段与后一 chunk 首几段。
NEIGHBOR_PARAGRAPHS = 3

#: prompt 资产的位置与它 frontmatter 里的版本号字段。
PROMPT_ASSET = ("skill", "translate", "SKILL.md")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
PROMPT_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)

#: 整段包在代码围栏里的译文。SKILL.md 写了「只输出译文本身」，但四层 validate 对围栏不敏感
#: （反引号不是控制序列，也不改花括号与 `$` 的计数），不剥掉就会原样进 zh.tex。开栏的语言
#: 标签后允许空白与 CRLF，闭合栏允许紧贴末行文本。
CODE_FENCE_RE = re.compile(r"\A```[A-Za-z]*[ \t]*\r?\n(.*?)\s*```\Z", re.DOTALL)

#: 产物文本的编码；读写都用它。
ENCODING = "utf-8"


@dataclass(frozen=True)
class TranslateResult:
    """驱动器的返回值：manifest、工作目录与是否命中跳过。"""

    manifest: TranslateManifest
    workdir: workdir.Workdir
    skipped: bool


@dataclass(frozen=True)
class ChunkContext:
    """一个 chunk 的全部翻译输入：正文、首尾空白与按 `part` 组合的附带上下文。"""

    id: str
    body: str
    leading: str
    trailing: str
    heading_tree: tuple[str, ...]
    abstract: str | None
    previous: str
    following: str
    terms: tuple[str, ...]
    do_not_translate: tuple[str, ...]
    style: str | None


@dataclass
class ChunkOutcome:
    """一个 chunk 的翻译结局，worker 的返回值。"""

    id: str
    status: ChunkTranslateStatus
    body: str
    attempts: int = 0
    failures: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 阶段驱动器


def translate(
    workdir_name: str | None = None,
    workdir_path: Path | None = None,
    *,
    model: str | None = None,
    jobs: int = DEFAULT_JOBS,
    max_fallback_ratio: float = DEFAULT_MAX_FALLBACK_RATIO,
    force: bool = False,
) -> TranslateResult:
    """逐 chunk 翻译并跑四层 validate，写出 build/translated/ 与 manifest。

    `workdir_name` 是工作目录名（arXiv 编号，或本地源码目录的 basename），`workdir_path`
    直接给出论文工作目录本身并覆盖前者。`model` 为 None 时用 `DEFAULT_MODEL`。`jobs` 是并发
    度，`max_fallback_ratio` 是出口判定的回退比例阈值，`force` 无视已有结论整篇重翻。
    """
    paper_workdir = workdir.Workdir(workdir.resolve(workdir_name, workdir_path))
    paper_workdir.create()  # 前置条件不满足时也要写 manifest，先确保四区存在
    model_id = model or DEFAULT_MODEL

    chunk_manifest = manifests.load_manifest(paper_workdir.manifest_path(CHUNK_STAGE_NAME), ChunkManifest)
    if chunk_manifest is None:
        _reset_outputs(paper_workdir)
        return _write_result(
            paper_workdir,
            TranslateManifest(
                status=TranslateStatus.CHUNK_MISSING,
                model_id=model_id,
                message="读不到 build/manifests/chunk.json 或它不可解析，先跑 `tongtu stage chunk`。",
            ),
        )
    blocking = _blocking_chunk_status(paper_workdir, chunk_manifest, model_id)
    if blocking is not None:
        _reset_outputs(paper_workdir)
        return _write_result(paper_workdir, blocking)
    survey_manifest = manifests.load_manifest(paper_workdir.manifest_path(SURVEY_STAGE_NAME), SurveyManifest)
    if survey_manifest is None:
        _reset_outputs(paper_workdir)
        return _write_result(
            paper_workdir,
            _manifest(
                TranslateStatus.SURVEY_MISSING,
                chunk_manifest,
                None,
                model_id,
                message="读不到 build/manifests/survey.json 或它不可解析，先跑 `tongtu stage survey`。",
            ),
        )

    skill, prompt_version, asset_error = _prompt_asset()
    if asset_error:
        _reset_outputs(paper_workdir)
        return _write_result(
            paper_workdir,
            _manifest(TranslateStatus.TRANSLATE_FAILED, chunk_manifest, survey_manifest, model_id, message=asset_error),
        )
    if not force:
        existing = _load_skippable_manifest(
            paper_workdir, chunk_manifest, survey_manifest, model_id, prompt_version, max_fallback_ratio
        )
        if existing is not None:
            return TranslateResult(manifest=existing, workdir=paper_workdir, skipped=True)

    _reset_outputs(paper_workdir)
    blocked, resolved_glossary, brief = _load_inputs(paper_workdir, chunk_manifest, survey_manifest, model_id)
    if blocked is not None:
        return _write_result(paper_workdir, blocked)

    try:
        manifest = _translate(
            paper_workdir,
            chunk_manifest,
            survey_manifest,
            resolved_glossary,
            brief,
            skill=skill,
            model_id=model_id,
            prompt_version=prompt_version,
            jobs=jobs,
            max_fallback_ratio=max_fallback_ratio,
        )
    except OSError as error:  # chunk 文件读不出、译文写不出：磁盘侧的失败统一转状态
        manifest = _manifest(
            TranslateStatus.TRANSLATE_FAILED,
            chunk_manifest,
            survey_manifest,
            model_id,
            prompt_version=prompt_version,
            message=manifests.describe_error(error),
        )
    return _write_result(paper_workdir, manifest)


def _blocking_chunk_status(
    paper_workdir: workdir.Workdir, chunk_manifest: ChunkManifest, model_id: str
) -> TranslateManifest | None:
    """chunk 侧的两条前置条件，都不满足时返回 None。

    它排在读 survey manifest 之前：survey 的 hash 是跳过判定的输入，必须先读进来，而设计稿
    的前置条件把 chunk 两条排在 survey 两条之前——两个 chunk 状态同时不满足时，报出来的应是
    chunk 那条，否则 pdf_only 沿链的退出码会从 3 掉成 1。
    """
    if chunk_manifest.status is not ChunkStatus.OK:
        return _manifest(
            TranslateStatus.CHUNK_NOT_OK,
            chunk_manifest,
            None,
            model_id,
            message=f"chunk 的状态是 {chunk_manifest.status}，不是 ok，先重跑 `tongtu stage chunk`。",
        )
    absent = [record.id for record in chunk_manifest.chunks if not chunk_path(paper_workdir, record.id).is_file()]
    if absent:
        return _manifest(
            TranslateStatus.CHUNK_MISSING,
            chunk_manifest,
            None,
            model_id,
            message=f"chunk 的状态是 ok，但 build/chunks/ 下有 {len(absent)} 个 chunk 文件不在"
            f"（{'、'.join(absent[:5])}），先重跑 `tongtu stage chunk`。",
        )
    return None


def _load_inputs(
    paper_workdir: workdir.Workdir,
    chunk_manifest: ChunkManifest,
    survey_manifest: SurveyManifest,
    model_id: str,
) -> tuple[TranslateManifest | None, GlossaryFile, BriefFile]:
    """检查 survey 的状态与两件产物，并把它们读进来（chunk 侧两条已在 `_blocking_chunk_status`）。

    有问题时返回（相应的 manifest、空产物、空产物）；都齐全时返回（None、resolved glossary、
    brief）。产物只在这里读一遍，读到的对象直接交给主流程用。
    """

    def blocked(status: TranslateStatus, message: str) -> tuple[TranslateManifest, GlossaryFile, BriefFile]:
        return (
            _manifest(status, chunk_manifest, survey_manifest, model_id, message=message),
            GlossaryFile(),
            BriefFile(),
        )

    if survey_manifest.status is not SurveyStatus.OK:
        return blocked(
            TranslateStatus.SURVEY_NOT_OK,
            f"survey 的状态是 {survey_manifest.status}，不是 ok，先重跑 `tongtu stage survey`。",
        )
    resolved_glossary = manifests.load_manifest(glossary_file_path(paper_workdir), GlossaryFile)
    brief = manifests.load_manifest(brief_path(paper_workdir), BriefFile)
    if resolved_glossary is None or brief is None:
        name = glossary_file_path(paper_workdir).name if resolved_glossary is None else brief_path(paper_workdir).name
        return blocked(
            TranslateStatus.SURVEY_MISSING,
            f"survey 的状态是 ok，但 {name} 不在或不可解析，先重跑 `tongtu stage survey`。",
        )
    return None, resolved_glossary, brief


def _translate(
    paper_workdir: workdir.Workdir,
    chunk_manifest: ChunkManifest,
    survey_manifest: SurveyManifest,
    resolved_glossary: GlossaryFile,
    brief: BriefFile,
    *,
    skill: str,
    model_id: str,
    prompt_version: str,
    jobs: int,
    max_fallback_ratio: float,
) -> TranslateManifest:
    """前置条件满足之后的主流程：组装上下文、并发翻译、写出译文与 manifest。"""
    bodies = {
        record.id: chunk_path(paper_workdir, record.id).read_bytes().decode(ENCODING)
        for record in chunk_manifest.chunks
    }
    contexts = _contexts(chunk_manifest.chunks, bodies, resolved_glossary, brief)
    pending = [
        context for record, context in zip(chunk_manifest.chunks, contexts, strict=True) if record.translatable_chars
    ]
    # 两个不发请求也能判定的失败在开工前一次性报出：否则每个 chunk 都白跑 MAX_ASK_CALLS 次
    # 同样的本地失败，真正原因埋在各 chunk 的 failures 里。
    precondition = ""
    if pending and model_id not in opencode.PROVIDER.families:
        precondition = (
            f"OpenCode 的模型清单里没有 {model_id}，{len(pending)} 个 chunk 无法翻译。确认模型标识没写错；"
            f"确实有这个模型就往 tongtu/agent/opencode.py 的 MODEL_FAMILIES 表里补一条。"
        )
    elif pending and opencode.resolve_api_key() is None:
        precondition = f"{len(pending)} 个 chunk 无法翻译：{opencode.PROVIDER.key_hint}"
    if precondition:
        return _manifest(
            TranslateStatus.TRANSLATE_FAILED,
            chunk_manifest,
            survey_manifest,
            model_id,
            prompt_version=prompt_version,
            jobs=jobs,
            max_fallback_ratio=max_fallback_ratio,
            message=precondition,
        )

    outcomes = {
        record.id: ChunkOutcome(id=record.id, status=ChunkTranslateStatus.SKIPPED, body=context.body)
        for record, context in zip(chunk_manifest.chunks, contexts, strict=True)
        if not record.translatable_chars
    }
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for outcome in pool.map(lambda context: _translate_chunk(context, skill, model_id, paper_workdir), pending):
                outcomes[outcome.id] = outcome

    return _finish(
        paper_workdir,
        chunk_manifest,
        survey_manifest,
        contexts,
        outcomes,
        model_id=model_id,
        prompt_version=prompt_version,
        jobs=jobs,
        max_fallback_ratio=max_fallback_ratio,
    )


# ------------------------------------------------------------------ 单 chunk 翻译


def _translate_chunk(context: ChunkContext, skill: str, model_id: str, paper_workdir: workdir.Workdir) -> ChunkOutcome:
    """一个 chunk 的翻译与重试循环，在 worker 线程里跑；只返回结果，不碰磁盘产物。

    `ask` 失败、译文四层 validate 不通过、重试耗尽，三种情形同等对待：该 chunk 回退原文。
    循环里的任何异常也归到回退（网络错误与重试耗尽对出口判定的含义相同），不取消其余任务。
    只有 validate 的差异说明会附进下一次的提示词——`ask` 的失败现场（超时、429、返回空译文）
    对模型没有意义，它只进 manifest。

    重试额度只由产出了可评判译文的尝试消耗。`ask` 报错与返回空译文的那次不算：模型没有给出
    可评判的东西，把「四层全挂」回灌给它既不成立也白费一次额度。这类调用另由 `MAX_ASK_CALLS`
    兜住上限。
    """
    differences: list[str] = []
    detail = ""
    attempts = 0
    judged = 0
    while attempts < MAX_ASK_CALLS:
        attempts += 1
        log_path = paper_workdir.logs / f"{STAGE_NAME}-{context.id}-{attempts}.json"
        try:
            outcome = opencode.ask(
                prompt=build_prompt(skill, context, differences),
                text=context.body,
                model=model_id,
                schema=None,
                log_path=log_path,
                effort=REASONING_EFFORT,
            )
            if outcome.status != opencode.ASK_STATUS_OK:
                detail = outcome.detail
                continue
            translated = _strip_code_fence(outcome.text)
            if not translated.strip():
                detail = "ask 返回空译文：调用本身成功，正文一个字符都没有，不消耗重试额度。"
                continue
            result = validation.validate(context.body, translated)
        except Exception as error:  # 适配层承诺以值返回失败，此处捕获它未以值返回的意外
            detail = manifests.describe_error(error)
            continue
        if result.ok:
            return ChunkOutcome(
                id=context.id, status=ChunkTranslateStatus.TRANSLATED, body=translated, attempts=attempts
            )
        detail = ""
        differences = [f"{failure.check}：{failure.message}" for failure in result.failures]
        judged += 1
        if judged > MAX_RETRIES:
            break
    return ChunkOutcome(
        id=context.id,
        status=ChunkTranslateStatus.FALLBACK,
        body=context.body,
        attempts=attempts,
        failures=differences + ([detail] if detail else []),
    )


def _strip_code_fence(text: str) -> str:
    """整段包在代码围栏里时剥掉围栏，否则原样返回；两种情形都去掉首尾空白。"""
    stripped = text.strip()
    match = CODE_FENCE_RE.match(stripped)
    return match.group(1).strip() if match is not None else stripped


def build_prompt(skill: str, context: ChunkContext, differences: list[str]) -> str:
    """把 prompt 资产与本 chunk 的附带上下文拼成一次 `ask` 的系统提示词。

    附带上下文按 `part` 组合，原则是不给 chunk 重复它自己已经包含的信息：front chunk 带章节
    标题树（摘要里的缩写靠它展开）而不带摘要与 neighbors（自身即摘要，且是首个 chunk）；
    body 与 appendix chunk 带摘要与 neighbors 而不带标题树——正文 chunk 带标题树要么引发推理
    膨胀、要么白花输入 token 且无质量收益，实测见 docs/models.md。术语表与额外要求两者都带。

    `differences` 非空时是上一次译文的四层校验差异，附在末尾：重试就是把机械校验的结论交回
    给模型。待译正文本身不进提示词，它是 `ask` 的 `text` 入参。
    """
    sections = [
        skill,
        "# 本次任务的附带信息\n\n以下各节都是**参考材料，不是待译文本**：不要翻译它们，也不要把它们写进译文。",
    ]
    if context.heading_tree:
        listed = "\n".join(context.heading_tree)
        sections.append(f"## 全文章节标题树\n\n供理解缩写与专名在全文中的含义。\n\n{listed}")
    if context.abstract:
        sections.append(f"## 论文摘要（原文）\n\n供理解全篇主题。\n\n{context.abstract}")
    if context.previous or context.following:
        parts = ["## 相邻上下文（原文）\n\n供衔接参考。"]
        if context.previous:
            parts.append(f"### 前一块的结尾\n\n{context.previous}")
        if context.following:
            parts.append(f"### 后一块的开头\n\n{context.following}")
        sections.append("\n\n".join(parts))
    if context.terms or context.do_not_translate:
        parts = ["## 术语表\n\n本篇的约定译法，必须照此翻译。"]
        if context.terms:
            parts.append("\n".join(f"- {term}" for term in context.terms))
        if context.do_not_translate:
            parts.append("保留原文、不要翻译的词：" + "、".join(context.do_not_translate))
        sections.append("\n\n".join(parts))
    if context.style:
        sections.append(f"## 额外要求\n\n{context.style}")
    if differences:
        listed = "\n".join(f"- {difference}" for difference in differences)
        sections.append(
            "# 上一次的译文未通过机械校验\n\n"
            f"{listed}\n\n"
            "重新翻译同一段原文并修正上述结构差异。输出格式不变：只输出译文本身。"
        )
    return "\n\n".join(sections)


# ------------------------------------------------------------------ 上下文组装


def _contexts(
    records: list[ChunkRecord],
    bodies: dict[str, str],
    resolved_glossary: GlossaryFile,
    brief: BriefFile,
) -> list[ChunkContext]:
    """逐 chunk 组装翻译输入。"""
    entries = [
        glossary.GlossaryEntry(word=entry.word, translation=entry.translation, decided_by=entry.decided_by)
        for entry in resolved_glossary.terms
    ] + [
        glossary.GlossaryEntry(word=entry.word, translation=None, decided_by=entry.decided_by)
        for entry in resolved_glossary.do_not_translate
    ]
    heading_tree = _format_heading_tree(brief)
    contexts: list[ChunkContext] = []
    for index, record in enumerate(records):
        raw = bodies[record.id]
        body = raw.strip()
        front = record.part is Part.FRONT
        hits = glossary.relevant_terms(entries, body)
        contexts.append(
            ChunkContext(
                id=record.id,
                body=body,
                leading=raw[: len(raw) - len(raw.lstrip())],
                trailing=raw[len(raw.rstrip()) :],
                heading_tree=heading_tree if front else (),
                abstract=None if front else brief.abstract,
                previous="" if front or index == 0 else _neighbor(bodies[records[index - 1].id], tail=True),
                following=(
                    "" if front or index + 1 >= len(records) else _neighbor(bodies[records[index + 1].id], tail=False)
                ),
                terms=tuple(f"{hit.word} → {hit.translation}" for hit in hits if hit.translation is not None),
                do_not_translate=tuple(hit.word for hit in hits if hit.translation is None),
                style=resolved_glossary.style,
            )
        )
    return contexts


def _neighbor(text: str, *, tail: bool) -> str:
    """相邻 chunk 供给的上下文：前邻取它末尾的若干段，后邻取它开头的若干段。"""
    found = chunking.paragraphs(text)
    return "\n\n".join(found[-NEIGHBOR_PARAGRAPHS:] if tail else found[:NEIGHBOR_PARAGRAPHS])


def _format_heading_tree(brief: BriefFile) -> tuple[str, ...]:
    """把 brief 里的标题树排成缩进列表，每级两个空格；没有标题树时返回空元组。"""
    if not brief.heading_tree:
        return ()
    return tuple(f"{'  ' * (heading.depth - 1)}- {heading.argument}" for heading in brief.heading_tree)


# ------------------------------------------------------------------ prompt 资产


def _prompt_asset() -> tuple[str, str, str]:
    """读 prompt 资产，返回（正文、版本号、失败说明）；读不到或没有版本号时前两项为空串。

    版本号进跳过判定：升级 prompt 资产时把 frontmatter 里的这个数递增，已有译文随之作废。
    """
    directory, *rest = PROMPT_ASSET
    path = assets.asset_path(directory).joinpath(*rest)
    try:
        content = path.read_text(encoding=ENCODING)
    except OSError as error:
        return "", "", f"读不到 prompt 资产 {path}（{manifests.describe_error(error)}）"
    frontmatter = FRONTMATTER_RE.match(content)
    version = None if frontmatter is None else PROMPT_VERSION_RE.search(frontmatter.group(1))
    if version is None:
        return "", "", f"prompt 资产 {path} 的 frontmatter 里没有 version 字段"
    return FRONTMATTER_RE.sub("", content).strip(), version.group(1), ""


# ------------------------------------------------------------------ 落盘与出口判定


def _finish(
    paper_workdir: workdir.Workdir,
    chunk_manifest: ChunkManifest,
    survey_manifest: SurveyManifest,
    contexts: list[ChunkContext],
    outcomes: dict[str, ChunkOutcome],
    *,
    model_id: str,
    prompt_version: str,
    jobs: int,
    max_fallback_ratio: float,
) -> TranslateManifest:
    """按文档序收集结果，写出译文文件与 manifest 字段。"""
    translated_dir(paper_workdir).mkdir(parents=True, exist_ok=True)
    records: list[ChunkTranslateRecord] = []
    for context in contexts:
        outcome = outcomes[context.id]
        content = context.leading + outcome.body + context.trailing
        translated_path(paper_workdir, context.id).write_bytes(content.encode(ENCODING))
        records.append(
            ChunkTranslateRecord(
                id=context.id,
                status=outcome.status,
                sha256=_sha256(content),
                attempts=outcome.attempts,
                failures=outcome.failures,
            )
        )

    fallback = [record.id for record in records if record.status is ChunkTranslateStatus.FALLBACK]
    skipped = [record.id for record in records if record.status is ChunkTranslateStatus.SKIPPED]
    attempted = len(records) - len(skipped)
    ratio = len(fallback) / attempted if attempted else 0.0
    status = TranslateStatus.OK
    message = ""
    if ratio > max_fallback_ratio:
        status = TranslateStatus.TRANSLATE_FAILED
        message = (
            f"{attempted} 个参与翻译的 chunk 里有 {len(fallback)} 个回退原文，"
            f"回退比例 {ratio:.0%} 超过阈值 {max_fallback_ratio:.0%}，不进入 compile。"
            "译文照常落盘，逐 chunk 的失败现场在 manifest 的 chunks 列表里。"
        )
    return _manifest(
        status,
        chunk_manifest,
        survey_manifest,
        model_id,
        prompt_version=prompt_version,
        jobs=jobs,
        max_fallback_ratio=max_fallback_ratio,
        translated_sha256=manifests.records_sha256(record.sha256 for record in records),
        chunks=records,
        chunks_total=len(records),
        fallback_chunks=fallback,
        fallback_ratio=ratio,
        skipped_chunks=skipped,
        message=message,
    )


def _sha256(text: str) -> str:
    """一段文本的 sha256。"""
    return hashlib.sha256(text.encode(ENCODING)).hexdigest()


def _manifest(
    status: TranslateStatus,
    chunk_manifest: ChunkManifest,
    survey_manifest: SurveyManifest | None,
    model_id: str,
    **fields: object,
) -> TranslateManifest:
    """组装 manifest：三个输入 hash、模型标识与上游状态一律转录，其余字段由调用处给出。"""
    return TranslateManifest(
        status=status,
        chunks_sha256=chunk_manifest.chunks_sha256,
        glossary_sha256="" if survey_manifest is None else survey_manifest.glossary_sha256,
        brief_sha256="" if survey_manifest is None else survey_manifest.brief_sha256,
        model_id=model_id,
        chunk_status=str(chunk_manifest.status),
        survey_status="" if survey_manifest is None else str(survey_manifest.status),
        fetch_status=chunk_manifest.fetch_status,
        **fields,
    )


# ------------------------------------------------------------------ 跳过判定与路径


def _load_skippable_manifest(
    paper_workdir: workdir.Workdir,
    chunk_manifest: ChunkManifest,
    survey_manifest: SurveyManifest,
    model_id: str,
    prompt_version: str,
    max_fallback_ratio: float,
) -> TranslateManifest | None:
    """读已有 translate manifest；六个判定值全都对得上且译文都在，返回它，否则返回 None。

    阈值也参与判定：上次按 20% 判 ok 的结论，在这次给出 10% 时不再成立，跳过它就等于让一次
    本该失败的执行静默退 0。
    """
    manifest = manifests.load_manifest(paper_workdir.manifest_path(STAGE_NAME), TranslateManifest)
    if manifest is None or manifest.status is not TranslateStatus.OK:
        return None
    if manifest.chunks_sha256 != chunk_manifest.chunks_sha256:
        return None
    if manifest.glossary_sha256 != survey_manifest.glossary_sha256:
        return None
    if manifest.brief_sha256 != survey_manifest.brief_sha256:
        return None
    if (manifest.model_id, manifest.prompt_version) != (model_id, prompt_version):
        return None
    if manifest.fallback_ratio > max_fallback_ratio:
        return None
    if not all(translated_path(paper_workdir, record.id).is_file() for record in manifest.chunks):
        return None
    return manifest


def translated_dir(paper_workdir: workdir.Workdir) -> Path:
    """译文文件所在目录；下游 compile 取同一个目录。"""
    return paper_workdir.build / TRANSLATED_DIRNAME


def translated_path(paper_workdir: workdir.Workdir, chunk_id: str) -> Path:
    """一个译文文件的路径。"""
    return translated_dir(paper_workdir) / f"{chunk_id}{TRANSLATED_SUFFIX}"


def _reset_outputs(paper_workdir: workdir.Workdir) -> None:
    """整目录删除 build/translated/：失败时不留上次的译文误导下游。"""
    shutil.rmtree(translated_dir(paper_workdir), ignore_errors=True)


def _write_result(paper_workdir: workdir.Workdir, manifest: TranslateManifest) -> TranslateResult:
    """写出 manifest 并组装返回值；除跳过外的每次执行（含失败）都经此处落盘。"""
    manifests.write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return TranslateResult(manifest=manifest, workdir=paper_workdir, skipped=False)
