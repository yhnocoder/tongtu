from __future__ import annotations

import hashlib
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import model, processes, texlog
from .artifacts.common import CompileReport, FixSession
from .manifests import describe_error
from .model.config import RoleTable, load_config, resolve_role
from .model.work import StopReason

COMPILE_TIMEOUT_SECONDS = 600

CLEAN_TIMEOUT_SECONDS = 60

ERROR_LINE_LIMIT = 5

LATEXMK_COMMAND: tuple[str, ...] = ("latexmk", "-xelatex", "-interaction=nonstopmode")

LATEXMK_CLEAN_COMMAND: tuple[str, ...] = ("latexmk", "-C")


@dataclass(frozen=True)
class CompileAttempt:
    outcome: processes.ProcessOutcome
    log_path: Path
    log_text: str | None
    pdf_bytes: int
    counts: texlog.LogCounts

    @property
    def passed(self) -> bool:
        return (
            not self.outcome.timed_out and self.outcome.returncode == 0 and self.pdf_bytes > 0 and self.counts.pages > 0
        )

    @property
    def pdf_name(self) -> str:
        return self.log_path.with_suffix(".pdf").name


def copy_src_tree(src: Path, tree: Path, main_filename: str) -> list[str]:
    shutil.copytree(src, tree, dirs_exist_ok=True)
    if (tree / main_filename).exists():
        return [f"src/ already contains {main_filename}; the copy in the compile tree is overwritten"]
    return []


def attempt_compile(tree: Path, main_filename: str) -> CompileAttempt:
    outcome = processes.run_in_process_group([*LATEXMK_COMMAND, main_filename], tree, COMPILE_TIMEOUT_SECONDS)
    log_path = tree / Path(main_filename).with_suffix(".log").name
    log_text = texlog.read_log(log_path)
    pdf_path = log_path.with_suffix(".pdf")
    return CompileAttempt(
        outcome=outcome,
        log_path=log_path,
        log_text=log_text,
        pdf_bytes=pdf_path.stat().st_size if pdf_path.is_file() else 0,
        counts=texlog.parse_counts(log_text),
    )


def compile_report(attempt: CompileAttempt) -> CompileReport:
    return CompileReport(
        pages=attempt.counts.pages,
        pdf_bytes=attempt.pdf_bytes,
        overfull_hboxes=attempt.counts.overfull_hboxes,
        undefined_references=attempt.counts.undefined_references,
        undefined_citations=attempt.counts.undefined_citations,
        missing_characters=attempt.counts.missing_characters,
        duration_seconds=attempt.outcome.duration_seconds,
    )


def clean_tree(tree: Path, main_filename: str) -> list[str]:
    try:
        outcome = processes.run_in_process_group([*LATEXMK_CLEAN_COMMAND, main_filename], tree, CLEAN_TIMEOUT_SECONDS)
    except OSError as error:
        return [f"failed to clean compile outputs before the verify compile ({describe_error(error)})"]
    if outcome.timed_out:
        return [
            f"cleaning compile outputs before the verify compile hit the {CLEAN_TIMEOUT_SECONDS}s timeout; "
            "the process group was terminated"
        ]
    if outcome.returncode != 0:
        return [f"latexmk exited with code {outcome.returncode} while cleaning before the verify compile"]
    return []


def fix(
    role: str,
    src: Path,
    tree: Path,
    trace_path: Path,
    main_filename: str,
    warnings: list[str],
    model_override: str | None,
    effort: str | None,
    report: Callable[[str], None] | None = None,
) -> FixSession:
    snapshot = _snapshot_tree_files(src, tree, main_filename)
    started = time.monotonic()
    outcome = model.work(role, tree, trace_path=trace_path, model=model_override, effort=effort, report=report)
    session = FixSession(
        stop_reason=str(outcome.stop_reason),
        model=session_model(role, model_override, effort),
        duration_seconds=time.monotonic() - started,
    )
    changed = _detect_changed_files(tree, snapshot)
    if changed:
        warnings.append(
            f"the fix session modified {len(changed)} files besides {main_filename}: {', '.join(changed)}; "
            "these changes stay in the compile tree and do not propagate to src/"
        )
    if outcome.stop_reason is StopReason.ERROR:
        warnings.append(
            f"the fix session ended with error ({outcome.detail}); the verdict still comes from the scripted checks"
        )
    if outcome.stop_reason is StopReason.TIMEOUT:
        warnings.append("the fix session ended with timeout; the verdict still comes from the scripted checks")
    return session


def session_model(role: str, model_override: str | None, effort: str | None) -> str:
    config, _ = load_config()
    if config is not None:
        resolved, _ = resolve_role(config, role, RoleTable.RUNTIME, model_override, effort)
        if resolved is not None:
            return f"{resolved.runtime}/{resolved.model}"
    return model_override or ""


def _snapshot_tree_files(src: Path, tree: Path, main_filename: str) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(src).as_posix()
        if relative == main_filename:
            continue
        tree_path = tree / relative
        if tree_path.is_file():
            snapshot[relative] = _file_sha256(tree_path)
    return snapshot


def _detect_changed_files(tree: Path, snapshot: dict[str, str]) -> list[str]:
    changed: list[str] = []
    for relative, digest in snapshot.items():
        tree_path = tree / relative
        if not tree_path.is_file() or _file_sha256(tree_path) != digest:
            changed.append(relative)
    return changed


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def timeout_message(attempt: CompileAttempt) -> str:
    return (
        f"latexmk hit the {COMPILE_TIMEOUT_SECONDS}s timeout and the process group was terminated; "
        f"log: {attempt.log_path}"
    )


def failure_message(attempt: CompileAttempt) -> str:
    reasons: list[str] = []
    if attempt.outcome.returncode != 0:
        reasons.append(f"latexmk exited with code {attempt.outcome.returncode}")
    if attempt.pdf_bytes == 0:
        reasons.append(f"{attempt.pdf_name} is missing or empty")
    if attempt.counts.pages <= 0:
        reasons.append(f"no page count can be parsed from {attempt.log_path.name}")
    if attempt.log_text is None:
        stderr = attempt.outcome.stderr_text.strip()[: processes.OUTPUT_EXCERPT_CHARS]
        detail = f"cannot read {attempt.log_path}; latexmk stderr: {stderr}"
    else:
        error_lines = texlog.error_lines(attempt.log_text, ERROR_LINE_LIMIT)
        if error_lines:
            excerpt = " | ".join(error_lines)
            detail = f"error lines from the log (at most {ERROR_LINE_LIMIT}): {excerpt}; full log: {attempt.log_path}"
        else:
            detail = f"no lines starting with {texlog.ERROR_LINE_PREFIX} in the log; full log: {attempt.log_path}"
    return f"{'; '.join(reasons)}. {detail}"
