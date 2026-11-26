from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import fonts, model, notes, processes, texlog
from .artifacts.common import CompileReport, NoteSession, Session, StopReason
from .manifests import describe_error, timeout_warning

COMPILE_TIMEOUT_SECONDS = 600

CLEAN_TIMEOUT_SECONDS = 60

ERROR_LINE_LIMIT = 5

LATEXMK_COMMAND: tuple[str, ...] = ("latexmk", "-norc", "-xelatex", "-interaction=nonstopmode", "-synctex=1")

LATEXMK_CLEAN_COMMAND: tuple[str, ...] = ("latexmk", "-norc", "-C")

STALE_OUTPUT_SUFFIXES: tuple[str, ...] = (".aux", ".fdb_latexmk", ".fls", ".log", ".pdf", ".xdv")

FIX_PROMPT_FILES = "主文件是 {main}，编译日志是 {log}。"


@dataclass(frozen=True)
class CompileAttempt:
    outcome: processes.ProcessOutcome
    log_path: Path
    log_text: str | None
    report: CompileReport

    @property
    def passed(self) -> bool:
        return (
            not self.outcome.timed_out
            and self.outcome.returncode == 0
            and self.report.pdf_bytes > 0
            and self.report.pages > 0
        )

    @property
    def pdf_name(self) -> str:
        return self.log_path.with_suffix(".pdf").name


def copy_src_tree(src: Path, tree: Path, main_filename: str) -> list[str]:
    shutil.copytree(src, tree, dirs_exist_ok=True)
    jobname = Path(main_filename).stem
    stale = [tree / f"{jobname}{suffix}" for suffix in STALE_OUTPUT_SUFFIXES]
    removed = [path.name for path in stale if path.is_file()]
    for path in stale:
        path.unlink(missing_ok=True)
    if removed:
        return [
            f"removed compile outputs shipped in src/ that would be read as this compile's own: {', '.join(removed)}"
        ]
    return []


def attempt_compile(tree: Path, main_filename: str) -> CompileAttempt:
    outcome = processes.run_in_process_group(
        [*LATEXMK_COMMAND, main_filename],
        tree,
        COMPILE_TIMEOUT_SECONDS,
        env=fonts.environment(fonts.configured(), os.environ),
    )
    log_path = tree / Path(main_filename).with_suffix(".log").name
    log_text = texlog.read_log(log_path)
    pdf_path = log_path.with_suffix(".pdf")
    report = CompileReport(
        pdf_bytes=pdf_path.stat().st_size if pdf_path.is_file() else 0,
        duration_seconds=outcome.duration_seconds,
        **texlog.parse_counts(log_text),
    )
    return CompileAttempt(outcome=outcome, log_path=log_path, log_text=log_text, report=report)


def clean_tree(tree: Path, main_filename: str) -> list[str]:
    try:
        outcome = processes.run_in_process_group([*LATEXMK_CLEAN_COMMAND, main_filename], tree, CLEAN_TIMEOUT_SECONDS)
    except OSError as error:
        return [f"failed to clean compile outputs ({describe_error(error)})"]
    if outcome.timed_out:
        return [f"cleaning compile outputs hit the {CLEAN_TIMEOUT_SECONDS}s timeout; the process group was terminated"]
    if outcome.returncode != 0:
        return [f"latexmk exited with code {outcome.returncode} while cleaning compile outputs"]
    return []


def fix(
    skill: NoteSession,
    tree: Path,
    log_path: Path,
    main_filename: str,
    warnings: list[str],
    *,
    mode: str,
    notes_path: Path,
    report: Callable[[str], None] | None = None,
) -> Session:
    log_name = Path(main_filename).with_suffix(".log").name
    outcome = model.work(
        skill,
        tree,
        mode=mode,
        log_path=log_path,
        report=report,
        extra_prompt=FIX_PROMPT_FILES.format(main=main_filename, log=log_name),
    )
    if outcome.session.stop_reason is StopReason.ERROR:
        warnings.append(
            f"the fix session ended with error ({outcome.detail}); the verdict still comes from the scripted checks"
        )
    if outcome.session.stop_reason is StopReason.TIMEOUT:
        warnings.append(timeout_warning("fix"))
    warnings.extend(notes.collect(tree, skill, notes_path))
    return outcome.session


def compile_with_fix(
    skill: NoteSession,
    tree: Path,
    main_filename: str,
    log_path: Path,
    warnings: list[str],
    report: Callable[[str, str], None],
    *,
    mode: str,
    notes_path: Path,
    fix_session: Session | None = None,
) -> tuple[CompileAttempt | None, Session | None, str]:
    if fix_session is not None:
        warnings.extend(clean_tree(tree, main_filename))
    report("verifying" if fix_session is not None else "compiling", main_filename)
    try:
        first = attempt_compile(tree, main_filename)
    except OSError as error:
        return (
            None,
            fix_session,
            f"failed to run latexmk ({describe_error(error)}). latexmk ships with the TeX "
            "distribution; check that it is installed and in PATH.",
        )
    if first.outcome.timed_out:
        return first, fix_session, timeout_message(first)
    if first.passed:
        return first, fix_session, ""
    if fix_session is not None:
        return (
            first,
            fix_session,
            (f"after the fix session the verify compile still fails the exit checks: {failure_message(first)}"),
        )
    report("fix session", "running")
    session = fix(
        skill,
        tree,
        log_path,
        main_filename,
        warnings,
        mode=mode,
        notes_path=notes_path,
        report=lambda action: report("fix session", action),
    )
    warnings.extend(clean_tree(tree, main_filename))
    report("verifying", main_filename)
    try:
        final = attempt_compile(tree, main_filename)
    except OSError as error:
        return None, session, f"failed to run latexmk for the verify compile ({describe_error(error)})."
    if final.outcome.timed_out:
        return final, session, timeout_message(final)
    if not final.passed:
        return (
            final,
            session,
            f"after the fix session the verify compile still fails the exit checks: {failure_message(final)}",
        )
    return final, session, ""


def timeout_message(attempt: CompileAttempt) -> str:
    return (
        f"latexmk hit the {COMPILE_TIMEOUT_SECONDS}s timeout and the process group was terminated; "
        f"log: {attempt.log_path}"
    )


def failure_message(attempt: CompileAttempt) -> str:
    reasons: list[str] = []
    if attempt.outcome.returncode != 0:
        reasons.append(f"latexmk exited with code {attempt.outcome.returncode}")
    if attempt.report.pdf_bytes == 0:
        reasons.append(f"{attempt.pdf_name} is missing or empty")
    if attempt.report.pages <= 0:
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
