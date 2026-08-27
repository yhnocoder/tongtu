from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path

from .. import chunks, validation
from ..artifacts.common import FixSession
from ..artifacts.review import ReviewManifest, ReviewStatus
from ..manifests import describe_error, timeout_warning, write_manifest
from ..model.config import RoleTable, load_config, resolve_role
from ..model.work import StopReason, work
from ..workdir import Workdir

STAGE_NAME = "review"

ROLE = "review"

CHUNKS_DIRNAME = "chunks"

TRANSLATED_DIRNAME = "translated"

SANDBOX_DIRNAME = "sandbox"

REVIEWED_DIRNAME = "reviewed"

BRIEF_FILENAME = "brief.json"

VALIDATE_FILENAME = "validate.py"

TRACE_FILENAME = "review.jsonl"

ENCODING = "utf-8"

SKIPPED_MESSAGE = "--no-review: no review session was started; translated/ was copied to reviewed/ unchanged."


def run(
    paper_workdir: Workdir,
    *,
    skip: bool = False,
    model_override: str | None = None,
    effort: str | None = None,
    report: Callable[[str, str], None] | None = None,
) -> ReviewManifest:
    paper_workdir.create()
    _reset_outputs(paper_workdir)
    manifest = _execute(paper_workdir, skip, model_override, effort, report or (lambda status, summary: None))
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _reset_outputs(paper_workdir: Workdir) -> None:
    shutil.rmtree(_site(paper_workdir), ignore_errors=True)
    shutil.rmtree(paper_workdir.build / REVIEWED_DIRNAME, ignore_errors=True)
    (paper_workdir.logs / TRACE_FILENAME).unlink(missing_ok=True)


def _execute(
    paper_workdir: Workdir,
    skip: bool,
    model_override: str | None,
    effort: str | None,
    report: Callable[[str, str], None],
) -> ReviewManifest:
    chunk_ids = sorted(path.stem for path in (paper_workdir.build / CHUNKS_DIRNAME).glob("*.tex"))
    if not chunk_ids:
        return _failed(f"build/{CHUNKS_DIRNAME}/ holds no chunk file; run survey first.")
    try:
        sources = {chunk_id: _read(paper_workdir, CHUNKS_DIRNAME, chunk_id) for chunk_id in chunk_ids}
        translated = {chunk_id: _read(paper_workdir, TRANSLATED_DIRNAME, chunk_id) for chunk_id in chunk_ids}
    except (OSError, UnicodeDecodeError) as error:
        return _failed(describe_error(error))

    if skip:
        report("--no-review", f"copying {TRANSLATED_DIRNAME}/ to {REVIEWED_DIRNAME}/")
        _write_reviewed(paper_workdir, sources, translated)
        return ReviewManifest(status=ReviewStatus.OK, message=SKIPPED_MESSAGE)

    resolved, detail = _resolve(model_override, effort)
    if resolved is None:
        return _failed(detail)
    model, skill_path = resolved

    try:
        _stage_site(paper_workdir, sources, translated, skill_path)
    except OSError as error:
        return _failed(describe_error(error))

    report("review session", f"running on {model}")
    started = time.monotonic()
    outcome = work(
        ROLE,
        _site(paper_workdir),
        trace_path=paper_workdir.logs / TRACE_FILENAME,
        model=model_override,
        effort=effort,
        report=lambda action: report("review session", action),
    )
    session = FixSession(stop_reason=str(outcome.stop_reason), model=model, duration_seconds=time.monotonic() - started)
    if outcome.stop_reason is StopReason.ERROR:
        return ReviewManifest(status=ReviewStatus.REVIEW_FAILED, session=session, message=outcome.detail)

    changed, reverted, bodies = _judge(paper_workdir, sources, translated)
    report(f"session {outcome.stop_reason}", f"{len(changed)} chunks changed, {len(reverted)} reverted")
    _write_reviewed(paper_workdir, sources, bodies)
    warnings = [timeout_warning(ROLE)] if outcome.stop_reason is StopReason.TIMEOUT else []
    return ReviewManifest(
        status=ReviewStatus.OK, session=session, changed=changed, reverted=reverted, warnings=warnings
    )


def _failed(message: str) -> ReviewManifest:
    return ReviewManifest(status=ReviewStatus.REVIEW_FAILED, message=message)


def _resolve(model_override: str | None, effort: str | None) -> tuple[tuple[str, str] | None, str]:
    config, detail = load_config()
    if config is None:
        return None, detail
    resolved, detail = resolve_role(config, ROLE, RoleTable.RUNTIME, model_override, effort)
    if resolved is None:
        return None, detail
    runtime = config.runtime[resolved.runtime or ""]
    return (f"{resolved.runtime}/{resolved.model}", runtime.skill_path.format(role=ROLE)), ""


def _stage_site(paper_workdir: Workdir, sources: dict[str, str], translated: dict[str, str], skill_path: str) -> None:
    site = _site(paper_workdir)
    for dirname, contents in ((CHUNKS_DIRNAME, sources), (REVIEWED_DIRNAME, translated)):
        (site / dirname).mkdir(parents=True, exist_ok=True)
        for chunk_id, body in contents.items():
            (site / dirname / f"{chunk_id}.tex").write_text(body, encoding=ENCODING)
    shutil.copyfile(paper_workdir.build / BRIEF_FILENAME, site / BRIEF_FILENAME)
    (site / skill_path).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(validation.__file__), site / skill_path / VALIDATE_FILENAME)


def _judge(
    paper_workdir: Workdir, sources: dict[str, str], translated: dict[str, str]
) -> tuple[list[str], list[str], dict[str, str]]:
    changed: list[str] = []
    reverted: list[str] = []
    bodies: dict[str, str] = {}
    for chunk_id, source in sources.items():
        reviewed = _reviewed_in_site(paper_workdir, chunk_id)
        body = translated[chunk_id]
        if reviewed != body:
            changed.append(chunk_id)
            if reviewed is None or not validation.validate(source.strip(), reviewed.strip()).ok:
                reverted.append(chunk_id)
            else:
                body = reviewed
        bodies[chunk_id] = body
    return changed, reverted, bodies


def _reviewed_in_site(paper_workdir: Workdir, chunk_id: str) -> str | None:
    path = _site(paper_workdir) / REVIEWED_DIRNAME / f"{chunk_id}.tex"
    try:
        return path.read_text(encoding=ENCODING)
    except (OSError, UnicodeDecodeError):
        return None


def _write_reviewed(paper_workdir: Workdir, sources: dict[str, str], bodies: dict[str, str]) -> None:
    reviewed_dir = paper_workdir.build / REVIEWED_DIRNAME
    reviewed_dir.mkdir(parents=True, exist_ok=True)
    for chunk_id, body in bodies.items():
        content = chunks.restore_padding(sources[chunk_id], body)
        (reviewed_dir / f"{chunk_id}.tex").write_text(content, encoding=ENCODING)


def _read(paper_workdir: Workdir, dirname: str, chunk_id: str) -> str:
    return (paper_workdir.build / dirname / f"{chunk_id}.tex").read_text(encoding=ENCODING)


def _site(paper_workdir: Workdir) -> Path:
    return paper_workdir.build / SANDBOX_DIRNAME / STAGE_NAME
