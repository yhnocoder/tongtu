from __future__ import annotations

import re
import shutil
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .. import masking, validation
from ..artifacts.survey import BriefFile, Part
from ..artifacts.translate import (
    ChunkTranslateRecord,
    ChunkTranslateStatus,
    TranslateManifest,
    TranslateStatus,
)
from ..assets import asset_path
from ..console import console
from ..manifests import describe_error, write_manifest
from ..model.ask import AskStatus, ask
from ..model.config import RoleTable, load_config, resolve_role
from ..workdir import Workdir

STAGE_NAME = "translate"

BRIEF_FILENAME = "brief.json"

CHUNKS_DIRNAME = "chunks"

TRANSLATED_DIRNAME = "translated"

SKILL_FILENAME = "SKILL.md"

ROLE = "translate"

ENCODING = "utf-8"

MAX_RETRIES = 1

MAX_ASK_CALLS = MAX_RETRIES + 3

MAX_FALLBACK_RATIO = 0.2

RETRY_EFFORT = "low"

NEIGHBOR_PARAGRAPHS = 3

EMPTY_REPLY_DETAIL = "ask 返回空译文：调用本身成功，正文一个字符都没有。"

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

PROMPT_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)

CODE_FENCE_RE = re.compile(r"\A```[A-Za-z]*[ \t]*\r?\n(.*?)\s*```\Z", re.DOTALL)

REFERENCE_HEADER = (
    "# 本次任务的附带信息\n\n以下各节都是**参考材料，不是待译文本**：不要翻译它们，也不要把它们写进译文。"
)


@dataclass(frozen=True)
class _Context:
    id: str
    body: str
    leading: str
    trailing: str
    system: str


@dataclass
class _Outcome:
    id: str
    status: ChunkTranslateStatus
    body: str
    attempts: int = 0
    failures: list[str] = field(default_factory=list)
    fenced: bool = False


def run(
    paper_workdir: Workdir,
    *,
    jobs: int,
    ask_model: str | None = None,
    ask_effort: str | None = None,
) -> TranslateManifest:
    paper_workdir.create()
    _reset_outputs(paper_workdir)
    manifest = _execute(paper_workdir, jobs, ask_model, ask_effort)
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _reset_outputs(paper_workdir: Workdir) -> None:
    shutil.rmtree(paper_workdir.build / TRANSLATED_DIRNAME, ignore_errors=True)
    for path in paper_workdir.logs.glob(f"{STAGE_NAME}-*.json"):
        path.unlink(missing_ok=True)


def _execute(paper_workdir: Workdir, jobs: int, ask_model: str | None, ask_effort: str | None) -> TranslateManifest:
    try:
        brief = BriefFile.model_validate_json((paper_workdir.build / BRIEF_FILENAME).read_text(encoding=ENCODING))
        bodies = [_chunk_path(paper_workdir, record.id).read_text(encoding=ENCODING) for record in brief.chunks]
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        return TranslateManifest(status=TranslateStatus.TRANSLATE_FAILED, jobs=jobs, message=describe_error(error))

    skill, prompt_version, detail = _prompt_asset()
    if detail:
        return TranslateManifest(status=TranslateStatus.TRANSLATE_FAILED, jobs=jobs, message=detail)

    pending = [record for record in brief.chunks if record.translatable_chars]
    model, effort = "", ""
    if pending:
        resolved, detail = _resolve(ask_model, ask_effort)
        if resolved is None:
            return TranslateManifest(
                status=TranslateStatus.TRANSLATE_FAILED, prompt_version=prompt_version, jobs=jobs, message=detail
            )
        model, effort = resolved
        console.print(f"  {STAGE_NAME}：{model}，{len(brief.chunks)} 个 chunk，并发 {jobs}")

    contexts = _contexts(brief, bodies, skill)
    outcomes = {
        context.id: _Outcome(id=context.id, status=ChunkTranslateStatus.SKIPPED, body=context.body)
        for record, context in zip(brief.chunks, contexts, strict=True)
        if not record.translatable_chars
    }
    if pending:
        translatable = [context for context in contexts if context.id not in outcomes]

        def worker(context: _Context) -> _Outcome:
            return _translate_chunk(context, paper_workdir, ask_model, ask_effort)

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for outcome in pool.map(worker, translatable):
                outcomes[outcome.id] = outcome

    return _finish(paper_workdir, contexts, outcomes, model, effort, prompt_version, jobs)


def _resolve(ask_model: str | None, ask_effort: str | None) -> tuple[tuple[str, str] | None, str]:
    config, detail = load_config()
    if config is None:
        return None, detail
    resolved, detail = resolve_role(config, ROLE, RoleTable.PROVIDER, ask_model, ask_effort)
    if resolved is None:
        return None, detail
    return (f"{resolved.provider}/{resolved.model}", resolved.effort), ""


def _prompt_asset() -> tuple[str, str, str]:
    path = asset_path("skill") / ROLE / SKILL_FILENAME
    try:
        content = path.read_text(encoding=ENCODING)
    except OSError as error:
        return "", "", f"读不到 prompt 资产 {path}（{describe_error(error)}）"
    frontmatter = FRONTMATTER_RE.match(content)
    version = None if frontmatter is None else PROMPT_VERSION_RE.search(frontmatter.group(1))
    if version is None:
        return "", "", f"prompt 资产 {path} 的 frontmatter 里没有 version 字段"
    return FRONTMATTER_RE.sub("", content).strip(), version.group(1), ""


def _contexts(brief: BriefFile, bodies: Sequence[str], skill: str) -> list[_Context]:
    stable = _stable_sections(brief, skill)
    heading_tree = _format_heading_tree(brief)
    contexts: list[_Context] = []
    for index, (record, raw) in enumerate(zip(brief.chunks, bodies, strict=True)):
        sections = [stable]
        if record.part is Part.FRONT and heading_tree:
            sections.append(f"## 全文章节标题树\n\n供理解缩写与专名在全文中的含义。\n\n{heading_tree}")
        neighbors = _neighbor_section(bodies, index)
        if neighbors:
            sections.append(neighbors)
        contexts.append(
            _Context(
                id=record.id,
                body=raw.strip(),
                leading=raw[: len(raw) - len(raw.lstrip())],
                trailing=raw[len(raw.rstrip()) :],
                system="\n\n".join(sections),
            )
        )
    return contexts


def _stable_sections(brief: BriefFile, skill: str) -> str:
    sections = [skill, REFERENCE_HEADER]
    if brief.abstract:
        sections.append(f"## 论文摘要（原文）\n\n供理解全篇主题。\n\n{brief.abstract}")
    if brief.terms or brief.do_not_translate:
        parts = ["## 术语表\n\n本篇的约定译法，必须照此翻译。"]
        if brief.terms:
            parts.append("\n".join(f"- {entry.word} → {entry.translation}" for entry in brief.terms))
        if brief.do_not_translate:
            parts.append("保留原文、不要翻译的词：" + "、".join(entry.word for entry in brief.do_not_translate))
        sections.append("\n\n".join(parts))
    if brief.style:
        sections.append(f"## 额外要求\n\n{brief.style}")
    return "\n\n".join(sections)


def _format_heading_tree(brief: BriefFile) -> str:
    return "\n".join(f"{'  ' * (heading.depth - 1)}- {heading.argument}" for heading in brief.heading_tree)


def _neighbor_section(bodies: Sequence[str], index: int) -> str:
    parts = ["## 相邻上下文（原文）\n\n供衔接参考。"]
    if index > 0:
        parts.append(f"### 前一块的结尾\n\n{_paragraphs(bodies[index - 1], tail=True)}")
    if index + 1 < len(bodies):
        parts.append(f"### 后一块的开头\n\n{_paragraphs(bodies[index + 1], tail=False)}")
    return "\n\n".join(parts) if len(parts) > 1 else ""


def _paragraphs(text: str, *, tail: bool) -> str:
    found = [part.strip() for part in masking.BLANK_LINE_RE.split(text) if part.strip()]
    return "\n\n".join(found[-NEIGHBOR_PARAGRAPHS:] if tail else found[:NEIGHBOR_PARAGRAPHS])


def _translate_chunk(
    context: _Context, paper_workdir: Workdir, ask_model: str | None, ask_effort: str | None
) -> _Outcome:
    started = time.monotonic()
    outcome = _ask_until_valid(context, paper_workdir, ask_model, ask_effort)
    console.print(
        f"  {context.id} {outcome.status}，ask 调用 {outcome.attempts} 次，用时 {time.monotonic() - started:.1f} s"
    )
    return outcome


def _ask_until_valid(
    context: _Context, paper_workdir: Workdir, ask_model: str | None, ask_effort: str | None
) -> _Outcome:
    messages: list[tuple[str, str]] = [("user", context.body)]
    failures: list[str] = []
    attempts = 0
    judged = 0
    fenced = False
    while attempts < MAX_ASK_CALLS:
        attempts += 1
        outcome = ask(
            role=ROLE,
            system=context.system,
            messages=messages,
            log_path=paper_workdir.logs / f"{STAGE_NAME}-{context.id}-{attempts}.json",
            model=ask_model,
            effort=RETRY_EFFORT if judged else ask_effort,
        )
        if outcome.status is AskStatus.ERROR:
            failures = [outcome.detail]
            continue
        translated, stripped = _strip_code_fence(outcome.text)
        if not translated:
            failures = [EMPTY_REPLY_DETAIL]
            continue
        fenced = fenced or stripped
        result = validation.validate(context.body, translated)
        if result.ok:
            return _Outcome(
                id=context.id,
                status=ChunkTranslateStatus.TRANSLATED,
                body=translated,
                attempts=attempts,
                fenced=fenced,
            )
        failures = [f"{failure.check}：{failure.message}" for failure in result.failures]
        judged += 1
        if judged > MAX_RETRIES:
            break
        messages = [("user", context.body), ("assistant", translated), ("user", _retry_message(failures))]
    return _Outcome(
        id=context.id,
        status=ChunkTranslateStatus.FALLBACK,
        body=context.body,
        attempts=attempts,
        failures=failures,
        fenced=fenced,
    )


def _retry_message(failures: Sequence[str]) -> str:
    listed = "\n".join(f"- {failure}" for failure in failures)
    return f"上一次的译文未通过机械校验：\n\n{listed}\n\n请修正上述差异并重新输出完整译文，只输出译文本身。"


def _strip_code_fence(text: str) -> tuple[str, bool]:
    stripped = text.strip()
    match = CODE_FENCE_RE.match(stripped)
    if match is None:
        return stripped, False
    return match.group(1).strip(), True


def _finish(
    paper_workdir: Workdir,
    contexts: Sequence[_Context],
    outcomes: dict[str, _Outcome],
    model: str,
    effort: str,
    prompt_version: str,
    jobs: int,
) -> TranslateManifest:
    chunks = {
        context.id: ChunkTranslateRecord(
            status=outcomes[context.id].status,
            attempts=outcomes[context.id].attempts,
            failures=outcomes[context.id].failures,
        )
        for context in contexts
    }
    warnings = [
        f"{context.id} 的译文整段包在代码围栏里，已剥掉围栏；SKILL.md 要求只输出译文本身"
        for context in contexts
        if outcomes[context.id].fenced
    ]
    attempted = sum(1 for record in chunks.values() if record.status is not ChunkTranslateStatus.SKIPPED)
    fallback = sum(1 for record in chunks.values() if record.status is ChunkTranslateStatus.FALLBACK)
    ratio = fallback / attempted if attempted else 0.0
    allowed = max(int(MAX_FALLBACK_RATIO * attempted), 1)
    status = TranslateStatus.OK
    message = ""
    if fallback > allowed:
        status = TranslateStatus.TRANSLATE_FAILED
        message = (
            f"{attempted} 个参与翻译的 chunk 里有 {fallback} 个回退原文，超过允许的 "
            f"{allowed} 个（{MAX_FALLBACK_RATIO:.0%}，至少放行 1 个），不进 compile；"
            f"逐 chunk 的失败现场在 manifest 的 chunks 里，"
            f"已翻译的 chunk 在 logs/{STAGE_NAME}-*.json 里。"
        )
    else:
        _write_translated(paper_workdir, contexts, outcomes)
        absent = _absent_translations(paper_workdir, contexts)
        if absent:
            shutil.rmtree(paper_workdir.build / TRANSLATED_DIRNAME, ignore_errors=True)
            status = TranslateStatus.TRANSLATE_FAILED
            message = (
                f"自检不过：build/{TRANSLATED_DIRNAME}/ 下有 {len(absent)} 个译文文件缺失或为空"
                f"（{'、'.join(absent[:5])}）。"
            )
    return TranslateManifest(
        status=status,
        model=model,
        effort=effort,
        prompt_version=prompt_version,
        jobs=jobs,
        chunks=chunks,
        fallback_ratio=ratio,
        warnings=warnings,
        message=message,
    )


def _absent_translations(paper_workdir: Workdir, contexts: Sequence[_Context]) -> list[str]:
    return [
        context.id
        for context in contexts
        if not _translated_path(paper_workdir, context.id).is_file()
        or not _translated_path(paper_workdir, context.id).read_text(encoding=ENCODING).strip()
    ]


def _write_translated(paper_workdir: Workdir, contexts: Sequence[_Context], outcomes: dict[str, _Outcome]) -> None:
    (paper_workdir.build / TRANSLATED_DIRNAME).mkdir(parents=True, exist_ok=True)
    for context in contexts:
        content = context.leading + outcomes[context.id].body + context.trailing
        _translated_path(paper_workdir, context.id).write_text(content, encoding=ENCODING)


def _chunk_path(paper_workdir: Workdir, chunk_id: str) -> Path:
    return paper_workdir.build / CHUNKS_DIRNAME / f"{chunk_id}.tex"


def _translated_path(paper_workdir: Workdir, chunk_id: str) -> Path:
    return paper_workdir.build / TRANSLATED_DIRNAME / f"{chunk_id}.tex"
