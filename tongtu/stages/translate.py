from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import glossary, model, notes, validation
from ..artifacts.common import Session, StopReason
from ..artifacts.translate import GlossaryCounts, TranslateManifest, TranslateStatus
from ..glossary import GlossaryError, Selection
from ..manifests import describe_error, timeout_warning, write_manifest
from ..model.work import skill_dir
from ..workdir import ENCODING, Workdir

STAGE_NAME = "translate"

SKILL = "translate"

SOURCE_FILENAME = "en.tex"

TRANSLATION_FILENAME = "zh.tex"

VALIDATE_FILENAME = "validate.py"

SESSION_STATUS = "session"


@dataclass(frozen=True)
class Verdict:
    written: bool
    problems: tuple[str, ...]
    paragraphs_total: int
    paragraphs_translated: int

    @property
    def ok(self) -> bool:
        return self.written and not self.problems


def run(
    paper_workdir: Workdir,
    *,
    mode: str,
    glossary_files: Sequence[Path] = (),
    report: Callable[[str, str], None] | None = None,
) -> TranslateManifest:
    paper_workdir.create()
    for path in (paper_workdir.manifest_path(STAGE_NAME), paper_workdir.translated, paper_workdir.paper_glossary):
        path.unlink(missing_ok=True)
    manifest = _execute(paper_workdir, mode, tuple(glossary_files), report or (lambda status, summary: None))
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _execute(
    paper_workdir: Workdir, mode: str, cli_glossaries: tuple[Path, ...], report: Callable[[str, str], None]
) -> TranslateManifest:
    try:
        masked = paper_workdir.masked.read_text(encoding=ENCODING)
    except (OSError, UnicodeDecodeError) as error:
        return TranslateManifest(
            status=TranslateStatus.TRANSLATE_FAILED,
            mode=mode,
            message=f"cannot read {paper_workdir.masked} ({describe_error(error)}); run mask first.",
        )
    try:
        selection = glossary.load_selection(masked, paper_workdir.path, cli_glossaries)
    except GlossaryError as error:
        return TranslateManifest(status=TranslateStatus.GLOSSARY_INVALID, mode=mode, message=str(error))
    warnings = list(selection.unmatched)
    counts = GlossaryCounts(user=selection.user_count, learned=selection.learned_count)
    session_dir = paper_workdir.sandbox(STAGE_NAME)

    if (session_dir / TRANSLATION_FILENAME).exists():
        verdict = _verify(masked, session_dir, selection)
        if verdict.ok:
            return _accept(paper_workdir, session_dir, selection, mode, None, verdict, counts, warnings)

    report(SESSION_STATUS, "running")
    session = _launch(paper_workdir, session_dir, selection, mode, warnings, report)
    verdict = _verify(masked, session_dir, selection)
    if verdict.ok:
        return _accept(paper_workdir, session_dir, selection, mode, session, verdict, counts, warnings)
    return TranslateManifest(
        status=TranslateStatus.TRANSLATE_FAILED,
        mode=mode,
        session=session,
        paragraphs_total=verdict.paragraphs_total,
        paragraphs_translated=verdict.paragraphs_translated,
        problems=list(verdict.problems),
        glossary=counts,
        warnings=warnings,
        message=_failure_message(verdict, session, session_dir),
    )


def _launch(
    paper_workdir: Workdir,
    session_dir: Path,
    selection: Selection,
    mode: str,
    warnings: list[str],
    report: Callable[[str, str], None],
) -> Session:
    session_dir.mkdir(parents=True, exist_ok=True)
    source = session_dir / SOURCE_FILENAME
    source.unlink(missing_ok=True)
    content = paper_workdir.masked.read_bytes()
    source.write_bytes(content if content.endswith(b"\n") else content + b"\n")
    source.chmod(glossary.READ_ONLY_MODE)
    glossary.write_session_files(session_dir, selection)
    validate_copy = session_dir / skill_dir(SKILL) / VALIDATE_FILENAME
    validate_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(validation.__file__), validate_copy)
    outcome = model.work(
        SKILL,
        session_dir,
        mode=mode,
        log_path=paper_workdir.translate_log,
        report=lambda action: report(SESSION_STATUS, action),
    )
    if outcome.session.stop_reason is StopReason.TIMEOUT:
        warnings.append(timeout_warning(STAGE_NAME))
    if outcome.session.stop_reason is StopReason.ERROR:
        warnings.append(
            f"the {STAGE_NAME} session ended with error ({outcome.detail}); "
            "the verdict still comes from the scripted checks"
        )
    warnings.extend(notes.collect(session_dir, SKILL, paper_workdir.notes))
    return outcome.session


def _verify(masked: str, session_dir: Path, selection: Selection) -> Verdict:
    total = validation.paragraphs_to_translate(masked)
    translation_path = session_dir / TRANSLATION_FILENAME
    if not translation_path.is_file():
        return Verdict(written=False, problems=(), paragraphs_total=total, paragraphs_translated=0)
    try:
        translation = translation_path.read_text(encoding=ENCODING)
    except (OSError, UnicodeDecodeError) as error:
        problem = f"[{validation.Category.STRUCTURE}] cannot read {translation_path} ({describe_error(error)})"
        return Verdict(written=True, problems=(problem,), paragraphs_total=total, paragraphs_translated=0)
    problems: list[str] = []
    try:
        session_glossary = glossary.read_session_glossary(session_dir)
    except GlossaryError as error:
        session_glossary = validation.Glossary()
        problems.append(f"[{validation.Category.GLOSSARY}] {error}")
    merged = glossary.validation_glossary(selection, session_glossary)
    checked = validation.check(masked, translation, dict(merged.terms), merged.do_not_translate)
    problems.extend(f"[{problem.category}] {problem.message}" for problem in checked.problems)
    return Verdict(
        written=True,
        problems=tuple(problems),
        paragraphs_total=checked.paragraphs_total,
        paragraphs_translated=checked.paragraphs_translated,
    )


def _accept(
    paper_workdir: Workdir,
    session_dir: Path,
    selection: Selection,
    mode: str,
    session: Session | None,
    verdict: Verdict,
    counts: GlossaryCounts,
    warnings: list[str],
) -> TranslateManifest:
    shutil.copyfile(session_dir / TRANSLATION_FILENAME, paper_workdir.translated)
    added = glossary.publish(session_dir, selection, paper_workdir.paper_glossary)
    return TranslateManifest(
        status=TranslateStatus.OK,
        mode=mode,
        session=session,
        paragraphs_total=verdict.paragraphs_total,
        paragraphs_translated=verdict.paragraphs_translated,
        glossary=counts.model_copy(update={"added": added}),
        warnings=warnings,
    )


def _failure_message(verdict: Verdict, session: Session, session_dir: Path) -> str:
    if verdict.written:
        found = f"the scripted checks report {len(verdict.problems)} problems in {TRANSLATION_FILENAME}"
    else:
        found = f"{TRANSLATION_FILENAME} was not written"
    return (
        f"the {STAGE_NAME} session ended with {session.stop_reason} and {found}; "
        f"the session directory {session_dir} is kept, and running tongtu run again continues there."
    )
