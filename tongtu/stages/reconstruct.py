from __future__ import annotations

import shutil
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import compiling, model, notes, pipeline
from ..artifacts.common import NoteSession, Session, StopReason
from ..artifacts.reconstruct import (
    CheckReport,
    RecognitionReport,
    ReconstructManifest,
    ReconstructStatus,
    RegionKind,
    RegionsFile,
)
from ..manifests import describe_error, timeout_warning, write_manifest
from ..reconstruct import checks, formula, packing, recognition, review, table
from ..reconstruct.crops import CropError
from ..reconstruct.latex import ToolError
from ..reconstruct.rebuild import MAIN_FILENAME, rebuild
from ..reconstruct.recognition import Verdict
from ..workdir import Workdir

STAGE_NAME = "reconstruct"

SKILL = "reconstruct_fix"

FORMULA_SKILL: NoteSession = "reconstruct_formula"

TABLE_SKILL: NoteSession = "reconstruct_table"

ORIGINAL_FILENAME = "original.pdf"

PACKED_DIRNAME = "packed"

LOST_GLYPH_EXAMPLES = 8

SESSION_STATUS = "{skill} session"

Report = Callable[[str, str], None]


@dataclass(frozen=True)
class Recognizer:
    skill: NoteSession
    noun: str
    log: Callable[[Workdir], Path]
    recognizable: Callable[[str, RegionsFile], Any]
    prepare: Callable[[Path, Path, Any], None]
    verify_all: Callable[[Path, Mapping[str, str], Any], dict[str, Verdict]]
    write_back: Callable[[str, Mapping[str, Verdict], Path], tuple[str, int]]


FORMULAS = Recognizer(
    skill=FORMULA_SKILL,
    noun="formulas",
    log=lambda workdir: workdir.reconstruct_formula_log,
    recognizable=formula.recognizable,
    prepare=formula.prepare,
    verify_all=formula.verify_all,
    write_back=lambda tex, verdicts, tree: formula.write_back(tex, verdicts),
)

TABLES = Recognizer(
    skill=TABLE_SKILL,
    noun="tables",
    log=lambda workdir: workdir.reconstruct_table_log,
    recognizable=table.recognizable,
    prepare=table.prepare,
    verify_all=table.verify_all,
    write_back=table.write_back,
)


def run(paper_workdir: Workdir, *, mode: str, report: Report | None = None) -> ReconstructManifest:
    paper_workdir.create()
    pipeline.clean(paper_workdir, STAGE_NAME)
    paper_workdir.create()
    if pipeline.fetched_route(paper_workdir) == "pdf":
        manifest = _rebuild(paper_workdir, mode, report or (lambda status, summary: None))
    else:
        manifest = ReconstructManifest(status=ReconstructStatus.OK, rebuilt=False)
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _rebuild(paper_workdir: Workdir, mode: str, report: Report) -> ReconstructManifest:
    tree = paper_workdir.sandbox(STAGE_NAME)
    tree.mkdir(parents=True, exist_ok=True)
    original = tree / ORIGINAL_FILENAME
    try:
        shutil.copyfile(paper_workdir.original_pdf, original)
        words, _pages = checks.pdf_words(original)
    except (OSError, RuntimeError, ValueError) as error:
        return _no_text_layer(f"cannot open {paper_workdir.original_pdf} as a PDF ({describe_error(error)}).")
    if not words:
        return _no_text_layer(
            f"{paper_workdir.original_pdf} has no English word in its text layer; scanned PDFs are not supported."
        )
    report("rebuilding", ORIGINAL_FILENAME)
    try:
        regions = rebuild(original, tree)
    except ToolError as error:
        return _failed(f"the rule rebuild could not run an external command: {error}")
    except Exception as error:
        return _failed(f"the rule rebuild raised {describe_error(error)} at {_location(error)}")
    counts = {kind.value: 0 for kind in RegionKind}
    for region in regions.regions:
        counts[region.kind.value] += 1
    warnings: list[str] = []
    formulas, failure = _recognize(FORMULAS, paper_workdir, tree, regions, mode, report, warnings)
    tables = None
    if not failure:
        tables, failure = _recognize(TABLES, paper_workdir, tree, regions, mode, report, warnings)
    if failure:
        return ReconstructManifest(
            status=ReconstructStatus.RECONSTRUCT_FAILED,
            message=failure,
            warnings=warnings,
            rebuilt=True,
            regions=counts,
            formulas=formulas,
            tables=tables,
        )
    rule_check, failure = _compile_and_check(tree, original, regions, report, warnings, fixed=False)
    final_check = rule_check
    fix_session = None
    if not failure and not rule_check.passed:
        fix_session = _fix(paper_workdir, tree, mode, report, warnings)
        final_check, failure = _compile_and_check(tree, original, regions, report, warnings, fixed=True)
    if not failure and not final_check.passed:
        stage = "after the fix session" if fix_session is not None else "after the rule rebuild"
        failure = f"{stage} the rebuilt PDF still fails the checks: {'; '.join(checks.failed_items(final_check))}"
    lost: list[tuple[str, str]] = []
    if not failure:
        try:
            lost = checks.lost_glyphs(tree)
            if lost:
                examples = ", ".join(f"{name} lacks {glyphs}" for name, glyphs in lost[:LOST_GLYPH_EXAMPLES])
                warnings.append(f"{len(lost)} inline crops lack glyphs of their text-layer hints: {examples}")
            failure = _pack(paper_workdir, tree, final_check.pages, report, warnings)
        except CropError as error:
            lost = []
            failure = str(error)
    return ReconstructManifest(
        status=ReconstructStatus.RECONSTRUCT_FAILED if failure else ReconstructStatus.OK,
        message=failure,
        warnings=warnings,
        rebuilt=True,
        regions=counts,
        rule_check=rule_check,
        final_check=final_check,
        crop_lost_glyphs=len(lost),
        fix_session=fix_session,
        formulas=formulas,
        tables=tables,
    )


def _recognize(
    recognizer: Recognizer,
    paper_workdir: Workdir,
    tree: Path,
    regions: RegionsFile,
    mode: str,
    report: Report,
    warnings: list[str],
) -> tuple[RecognitionReport | None, str]:
    main = tree / MAIN_FILENAME
    tex = main.read_text(encoding="utf-8")
    keys = recognizer.recognizable(tex, regions)
    if not keys:
        return None, ""
    result = RecognitionReport(total=len(keys), passed=0)
    try:
        session_dir = _session_dir(paper_workdir, recognizer.skill)
        recognizer.prepare(tree, session_dir, keys)
        session = _recognition_session(
            recognizer.skill, session_dir, recognizer.log(paper_workdir), paper_workdir, mode, report, warnings
        )
        result = result.model_copy(update={"session": session})
        transcriptions, problem = recognition.read_transcriptions(session_dir, recognizer.skill)
        if problem:
            warnings.append(problem)
        report("checking", recognizer.noun)
        tex, passed = recognizer.write_back(tex, recognizer.verify_all(tree, transcriptions, keys), tree)
    except CropError as error:
        return result, str(error)
    main.write_text(tex, encoding="utf-8")
    return result.model_copy(update={"passed": passed}), ""


def _session_dir(paper_workdir: Workdir, skill: str) -> Path:
    session_dir = paper_workdir.sandbox(skill)
    shutil.rmtree(session_dir, ignore_errors=True)
    session_dir.mkdir(parents=True)
    return session_dir


def _recognition_session(
    skill: NoteSession,
    session_dir: Path,
    log_path: Path,
    paper_workdir: Workdir,
    mode: str,
    report: Report,
    warnings: list[str],
) -> Session:
    status = SESSION_STATUS.format(skill=skill)
    report(status, "running")
    outcome = model.work(
        skill,
        session_dir,
        mode=mode,
        log_path=log_path,
        report=lambda action: report(status, action),
        tongtu_python=True,
    )
    if outcome.session.stop_reason is StopReason.TIMEOUT:
        warnings.append(timeout_warning(skill))
    if outcome.session.stop_reason is StopReason.ERROR:
        warnings.append(
            f"the {skill} session ended with error ({outcome.detail}); the verdict still comes from the scripted checks"
        )
    warnings.extend(notes.collect(session_dir, skill, paper_workdir.notes))
    return outcome.session


def _compile_and_check(
    tree: Path,
    original: Path,
    regions: RegionsFile,
    report: Report,
    warnings: list[str],
    *,
    fixed: bool,
) -> tuple[CheckReport, str]:
    if fixed:
        warnings.extend(compiling.clean_tree(tree, MAIN_FILENAME))
    report("verifying" if fixed else "compiling", MAIN_FILENAME)
    try:
        attempt = compiling.attempt_compile(tree, MAIN_FILENAME)
    except OSError as error:
        empty = checks.check(original, tree, None, regions).report
        return empty, (
            f"failed to run latexmk ({describe_error(error)}). latexmk ships with the TeX "
            "distribution; run tongtu doctor to see how to install it."
        )
    report("checking", MAIN_FILENAME)
    detail = checks.check(original, tree, attempt.report, regions)
    if detail.draft_failed:
        warnings.append(review.DRAFT_FAILED_LINE)
    return detail.report, ""


def _fix(paper_workdir: Workdir, tree: Path, mode: str, report: Report, warnings: list[str]) -> Session:
    report("fix session", "running")
    return compiling.fix(
        SKILL,
        tree,
        paper_workdir.reconstruct_fix_log,
        MAIN_FILENAME,
        warnings,
        mode=mode,
        notes_path=paper_workdir.notes,
        report=lambda action: report("fix session", action),
        tongtu_python=True,
    )


def _pack(paper_workdir: Workdir, tree: Path, pages: int, report: Report, warnings: list[str]) -> str:
    report("packing", packing.PACKED_CROPS_FILENAME)
    packed = tree / PACKED_DIRNAME
    missing = packing.missing_crops(tree)
    if missing:
        return f"main.tex references crops that do not exist: {', '.join(missing)}"
    packing.pack(tree, packed)
    try:
        attempt = compiling.attempt_compile(packed, MAIN_FILENAME)
    except OSError as error:
        return f"failed to run latexmk for the packed source ({describe_error(error)})."
    if attempt.report.pages != pages:
        return (
            f"the packed source compiles to {attempt.report.pages} pages, the unpacked one to {pages}; "
            f"log: {attempt.log_path}"
        )
    warnings.extend(compiling.clean_tree(packed, MAIN_FILENAME))
    shutil.rmtree(paper_workdir.src, ignore_errors=True)
    paper_workdir.src.mkdir(parents=True)
    for name in (MAIN_FILENAME, packing.PACKED_CROPS_FILENAME):
        if (packed / name).is_file():
            shutil.copyfile(packed / name, paper_workdir.src / name)
    return ""


def _location(error: Exception) -> str:
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return "an unknown location"
    frame = frames[-1]
    return f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"


def _no_text_layer(message: str) -> ReconstructManifest:
    return ReconstructManifest(status=ReconstructStatus.NO_TEXT_LAYER, rebuilt=True, message=message)


def _failed(message: str) -> ReconstructManifest:
    return ReconstructManifest(status=ReconstructStatus.RECONSTRUCT_FAILED, rebuilt=True, message=message)
