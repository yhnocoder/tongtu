from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import model, processes, texlog
from .artifacts.common import CompileReport, FixSession
from .manifests import describe_error, timeout_warning
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
    if (tree / main_filename).exists():
        return [f"src/ already contains {main_filename}; the copy in the compile tree is overwritten"]
    return []


def attempt_compile(tree: Path, main_filename: str) -> CompileAttempt:
    outcome = processes.run_in_process_group([*LATEXMK_COMMAND, main_filename], tree, COMPILE_TIMEOUT_SECONDS)
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
    role: str,
    tree: Path,
    trace_path: Path,
    main_filename: str,
    warnings: list[str],
    model_override: str | None,
    effort: str | None,
    report: Callable[[str], None] | None = None,
) -> FixSession:
    started = time.monotonic()
    outcome = model.work(role, tree, trace_path=trace_path, model=model_override, effort=effort, report=report)
    session = FixSession(
        stop_reason=str(outcome.stop_reason),
        model=outcome.model,
        duration_seconds=time.monotonic() - started,
    )
    if outcome.stop_reason is StopReason.ERROR:
        warnings.append(
            f"the fix session ended with error ({outcome.detail}); the verdict still comes from the scripted checks"
        )
    if outcome.stop_reason is StopReason.TIMEOUT:
        warnings.append(timeout_warning("fix"))
    return session


def compile_with_fix(
    role: str,
    tree: Path,
    main_filename: str,
    trace_path: Path,
    warnings: list[str],
    model_override: str | None,
    effort: str | None,
    report: Callable[[str, str], None],
) -> tuple[CompileAttempt | None, FixSession | None, str]:
    report("compiling", main_filename)
    try:
        first = attempt_compile(tree, main_filename)
    except OSError as error:
        return (
            None,
            None,
            f"failed to run latexmk ({describe_error(error)}). latexmk ships with the TeX "
            "distribution; check that it is installed and in PATH.",
        )
    if first.outcome.timed_out:
        return first, None, timeout_message(first)
    if first.passed:
        return first, None, ""
    report("fix session", "running")
    session = fix(
        role,
        tree,
        trace_path,
        main_filename,
        warnings,
        model_override,
        effort,
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
