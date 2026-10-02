from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from .. import chunks, masking, pipeline, validation
from ..artifacts.survey import BriefFile, Part
from ..artifacts.translate import (
    ChunkTranslateRecord,
    ChunkTranslateStatus,
    TranslateManifest,
    TranslateStatus,
)
from ..assets import asset_path
from ..manifests import describe_error, write_manifest
from ..model.ask import AskOutcome, AskStatus, ask
from ..model.config import Target, load_config, role_target
from ..model.work import StopReason, work
from ..workdir import ENCODING, Workdir

STAGE_NAME = "translate"

SKILL_FILENAME = "SKILL.md"

ROLE = "translate"

RETRY_EFFORT = "low"

NEIGHBOR_PARAGRAPHS = 3

EMPTY_REPLY_DETAIL = "model returned an empty translation: the call succeeded with not a single character of body."

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

PROMPT_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)

CODE_FENCE_RE = re.compile(r"\A```[A-Za-z]*[ \t]*\r?\n(.*?)\s*```\Z", re.DOTALL)

REFERENCE_HEADER = (
    "# 本次任务的附带信息\n\n以下各节都是**参考材料，不是待译文本**：不要翻译它们，也不要把它们写进译文。"
)


class ChunkProgressState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    WARNING = "warning"


@dataclass(frozen=True)
class ChunkProgress:
    id: str
    tokens: int
    state: ChunkProgressState


Report = Callable[[str, str], None]

ProgressCallback = Callable[[tuple[ChunkProgress, ...]], None]


@dataclass(frozen=True)
class _Context:
    id: str
    raw: str
    body: str
    system: str
    tokens: int


@dataclass
class _Outcome:
    id: str
    status: ChunkTranslateStatus
    body: str
    attempts: int = 0
    failures: list[str] = field(default_factory=list)
    model: str = ""


def run(
    paper_workdir: Workdir,
    *,
    jobs: int,
    report: Report | None = None,
    progress: ProgressCallback | None = None,
) -> TranslateManifest:
    paper_workdir.create()
    pipeline.clean(paper_workdir, STAGE_NAME)
    manifest = _execute(
        paper_workdir, jobs, report or (lambda status, summary: None), progress or (lambda chunks: None)
    )
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _execute(
    paper_workdir: Workdir,
    jobs: int,
    report: Report,
    progress: ProgressCallback,
) -> TranslateManifest:
    try:
        brief = BriefFile.model_validate_json(paper_workdir.brief.read_text(encoding=ENCODING))
        bodies = [_chunk_path(paper_workdir, record.id).read_text(encoding=ENCODING) for record in brief.chunks]
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        return TranslateManifest(status=TranslateStatus.TRANSLATE_FAILED, jobs=jobs, message=describe_error(error))

    skill, prompt_version, detail = _prompt_asset()
    if detail:
        return TranslateManifest(status=TranslateStatus.TRANSLATE_FAILED, jobs=jobs, message=detail)

    pending = [record for record in brief.chunks if record.translatable_chars]
    effort = ""
    if pending:
        target, detail = _resolve()
        if target is None:
            return TranslateManifest(
                status=TranslateStatus.TRANSLATE_FAILED, prompt_version=prompt_version, jobs=jobs, message=detail
            )
        display = str(target)
        effort = target.effort or ""
        report(
            STAGE_NAME,
            f"{display}, {len(brief.chunks)} chunks, {sum(record.tokens for record in brief.chunks)} tok, jobs {jobs}",
        )

    contexts = _contexts(brief, bodies, skill)
    outcomes = {
        context.id: _Outcome(id=context.id, status=ChunkTranslateStatus.SKIPPED, body=context.body)
        for record, context in zip(brief.chunks, contexts, strict=True)
        if not record.translatable_chars
    }
    if pending:
        translatable = [context for context in contexts if context.id not in outcomes]
        states = {
            record.id: ChunkProgressState.DONE if record.id in outcomes else ChunkProgressState.PENDING
            for record in brief.chunks
        }
        lock = threading.Lock()

        def publish() -> None:
            progress(tuple(ChunkProgress(record.id, record.tokens, states[record.id]) for record in brief.chunks))

        publish()

        def worker(context: _Context) -> _Outcome:
            with lock:
                states[context.id] = ChunkProgressState.RUNNING
                publish()
            outcome = _translate_until_valid(context, paper_workdir, target)
            with lock:
                if outcome.status is ChunkTranslateStatus.FALLBACK:
                    states[context.id] = ChunkProgressState.WARNING
                    report("warning", f"{context.id} fell back to the English source after {outcome.attempts} attempts")
                else:
                    states[context.id] = ChunkProgressState.DONE
                publish()
            return outcome

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for outcome in pool.map(worker, translatable):
                outcomes[outcome.id] = outcome

    model = next((outcome.model for outcome in outcomes.values() if outcome.model), "")
    return _finish(paper_workdir, contexts, outcomes, model, effort, prompt_version, jobs)


def _resolve() -> tuple[Target | None, str]:
    config, detail = load_config()
    if config is None:
        return None, detail
    return role_target(config, ROLE)


def _prompt_asset() -> tuple[str, str, str]:
    path = asset_path("skill") / ROLE / SKILL_FILENAME
    try:
        content = path.read_text(encoding=ENCODING)
    except OSError as error:
        return "", "", f"cannot read the prompt asset {path} ({describe_error(error)})"
    frontmatter = FRONTMATTER_RE.match(content)
    version = None if frontmatter is None else PROMPT_VERSION_RE.search(frontmatter.group(1))
    if version is None:
        return "", "", f"the frontmatter of prompt asset {path} has no version field"
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
                raw=raw,
                body=raw.strip(),
                system="\n\n".join(sections),
                tokens=record.tokens,
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
    before = _paragraphs(bodies[index - 1], tail=True) if index > 0 else "（这是全文的第一块，前面没有内容）"
    after = (
        _paragraphs(bodies[index + 1], tail=False)
        if index + 1 < len(bodies)
        else "（这是全文的最后一块，后面没有内容）"
    )
    return (
        "## 相邻上下文（原文）\n\n供衔接参考。\n\n"
        f"### 前一块的结尾\n\n```\n{before}\n```\n\n### 后一块的开头\n\n```\n{after}\n```"
    )


def _task_message(body: str) -> str:
    return f"请翻译：\n\n```\n{body}\n```"


def _paragraphs(text: str, *, tail: bool) -> str:
    found = [part.strip() for part in masking.BLANK_LINE_RE.split(text) if part.strip()]
    return "\n\n".join(found[-NEIGHBOR_PARAGRAPHS:] if tail else found[:NEIGHBOR_PARAGRAPHS])


def _translate_until_valid(context: _Context, paper_workdir: Workdir, target: Target) -> _Outcome:
    messages: list[tuple[str, str]] = [("user", _task_message(context.body))]
    failures: list[str] = []
    model = ""
    effort: str | None = None
    for attempts in (1, 2):
        if target.is_runtime:
            outcome = _agent_request(context, paper_workdir, messages, attempts, effort)
        else:
            outcome = ask(
                role=ROLE,
                system=context.system,
                messages=messages,
                log_path=paper_workdir.logs / f"{STAGE_NAME}-{context.id}-{attempts}.json",
                effort=effort,
            )
        model = outcome.model or model
        if outcome.status is AskStatus.ERROR:
            failures = [outcome.detail]
            continue
        translated = _strip_code_fence(outcome.text)
        if not translated:
            failures = [EMPTY_REPLY_DETAIL]
            continue
        result = validation.validate(context.body, translated)
        if result.ok:
            return _Outcome(
                id=context.id,
                status=ChunkTranslateStatus.TRANSLATED,
                body=translated,
                attempts=attempts,
                model=model,
            )
        failures = [f"{failure.check}: {failure.message}" for failure in result.failures]
        messages = [
            ("user", _task_message(context.body)),
            ("assistant", translated),
            ("user", _retry_message(failures)),
        ]
        effort = RETRY_EFFORT
    return _Outcome(
        id=context.id,
        status=ChunkTranslateStatus.FALLBACK,
        body=context.body,
        attempts=2,
        failures=failures,
        model=model,
    )


def _agent_request(
    context: _Context,
    paper_workdir: Workdir,
    messages: list[tuple[str, str]],
    attempt: int,
    effort: str | None,
) -> AskOutcome:
    site = paper_workdir.sandbox(STAGE_NAME) / context.id / str(attempt)
    actual_model = ""
    try:
        site.mkdir(parents=True, exist_ok=True)
        task = context.system + "\n\n" + "\n\n".join(f"## {role}\n\n{text}" for role, text in messages)
        (site / "task.md").write_text(task, encoding=ENCODING)
        outcome = work(
            role=ROLE,
            workdir=site,
            trace_path=paper_workdir.logs / f"{STAGE_NAME}-{context.id}-{attempt}.jsonl",
            effort=effort,
            prompt=(
                "读取 task.md，按其中的翻译规则、论文语境与对话完成翻译。"
                "这是文件任务：将最终完整译文写入当前目录 translation.tex，"
                "不要在文件里写 Markdown 围栏、说明或总结。task.md 里的输出规则指该文件的内容。"
                "只在当前目录工作；完成文件后结束。"
            ),
        )
        actual_model = outcome.model
        if outcome.stop_reason is not StopReason.FINISHED:
            return AskOutcome(
                status=AskStatus.ERROR,
                detail=outcome.detail or f"agent stopped: {outcome.stop_reason}",
                model=outcome.model,
            )
        output = site / "translation.tex"
        if output.is_symlink():
            return AskOutcome(
                status=AskStatus.ERROR, detail="translation.tex must not be a symlink", model=outcome.model
            )
        return AskOutcome(status=AskStatus.OK, text=output.read_text(encoding=ENCODING), model=outcome.model)
    except (OSError, UnicodeDecodeError) as error:
        return AskOutcome(status=AskStatus.ERROR, detail=describe_error(error), model=actual_model)


def _retry_message(failures: Sequence[str]) -> str:
    listed = "\n".join(f"- {failure}" for failure in failures)
    return f"上一次的译文未通过机械校验：\n\n{listed}\n\n请修正上述差异并重新输出完整译文，只输出译文本身。"


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    match = CODE_FENCE_RE.match(stripped)
    if match is None:
        return stripped
    return match.group(1).strip()


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
    warnings: list[str] = []
    for context in contexts:
        outcome = outcomes[context.id]
        if outcome.status is ChunkTranslateStatus.FALLBACK:
            listed = "; ".join(outcome.failures) or "no failure detail was recorded"
            warnings.append(f"{context.id} fell back to the English source; the last attempt failed: {listed}")
    _write_translated(paper_workdir, contexts, outcomes)
    return TranslateManifest(
        status=TranslateStatus.OK,
        model=model,
        effort=effort,
        prompt_version=prompt_version,
        jobs=jobs,
        chunks=chunks,
        warnings=warnings,
    )


def _write_translated(paper_workdir: Workdir, contexts: Sequence[_Context], outcomes: dict[str, _Outcome]) -> None:
    paper_workdir.translated.mkdir(parents=True, exist_ok=True)
    for context in contexts:
        content = chunks.restore_padding(context.raw, outcomes[context.id].body)
        _translated_path(paper_workdir, context.id).write_text(content, encoding=ENCODING)


def _chunk_path(paper_workdir: Workdir, chunk_id: str) -> Path:
    return paper_workdir.chunks / f"{chunk_id}.tex"


def _translated_path(paper_workdir: Workdir, chunk_id: str) -> Path:
    return paper_workdir.translated / f"{chunk_id}.tex"
