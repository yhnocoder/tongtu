from __future__ import annotations

import shutil
import traceback
from collections.abc import Callable, Mapping, Sized
from dataclasses import dataclass
from pathlib import Path

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
from ..model.config import OmlxConfig, load_config
from ..reconstruct import checks, formula, omlx, packing, recognition, review, table
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

SESSION_STATUS = "{name} session"

OMLX_STATUS = "omlx"

Report = Callable[[str, str], None]

Local = Callable[[Path, dict[str, RegionKind], Report, list[str]], tuple[dict[str, Verdict], dict[str, RegionKind]]]


@dataclass(frozen=True)
class Recognizer[KeysT: Sized]:
    skill: NoteSession
    noun: str
    log: Callable[[Workdir], Path]
    recognizable: Callable[[str, RegionsFile], KeysT]
    prepare: Callable[[Path, Path, KeysT], None]
    verify_all: Callable[[Path, Mapping[str, str], KeysT], dict[str, Verdict]]
    write_back: Callable[[str, Mapping[str, Verdict], Path], tuple[str, int]]


FORMULAS = Recognizer[dict[str, RegionKind]](
    skill=FORMULA_SKILL,
    noun="formulas",
    log=lambda workdir: workdir.reconstruct_formula_log,
    recognizable=formula.recognizable,
    prepare=formula.prepare,
    verify_all=formula.verify_all,
    write_back=lambda tex, verdicts, tree: formula.write_back(tex, verdicts),
)

TABLES = Recognizer[list[str]](
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
    omlx_config = _omlx_config()
    local: Local | None = None
    if omlx_config.enabled:
        report("checking", "oMLX")
        model_id, problem = omlx.check(omlx_config)
        if problem:
            return ReconstructManifest(status=ReconstructStatus.OMLX_UNAVAILABLE, rebuilt=True, message=problem)
        local = _local_formulas(paper_workdir, omlx_config, model_id)
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
    formulas, failure = _recognize(FORMULAS, paper_workdir, tree, regions, mode, report, warnings, local)
    tables = None
    if not failure:
        tables, failure = _recognize(TABLES, paper_workdir, tree, regions, mode, report, warnings, None)
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


def _recognize[KeysT: Sized](
    recognizer: Recognizer[KeysT],
    paper_workdir: Workdir,
    tree: Path,
    regions: RegionsFile,
    mode: str,
    report: Report,
    warnings: list[str],
    local: Callable[[Path, KeysT, Report, list[str]], tuple[dict[str, Verdict], KeysT]] | None,
) -> tuple[RecognitionReport | None, str]:
    main = tree / MAIN_FILENAME
    tex = main.read_text(encoding="utf-8")
    keys = recognizer.recognizable(tex, regions)
    if not keys:
        return None, ""
    result = RecognitionReport(total=len(keys), passed=0)
    try:
        verdicts: dict[str, Verdict] = {}
        remaining = keys
        if local is not None:
            verdicts, remaining = local(tree, keys, report, warnings)
            result = result.model_copy(update={"local_passed": len(verdicts)})
        if remaining:
            session_dir = _session_dir(paper_workdir, recognizer.skill)
            recognizer.prepare(tree, session_dir, remaining)
            session = _recognition_session(
                recognizer.skill, session_dir, recognizer.log(paper_workdir), paper_workdir, mode, report, warnings
            )
            result = result.model_copy(update={"session": session})
            transcriptions, problem = recognition.read_transcriptions(session_dir, recognizer.skill)
            if problem:
                warnings.append(problem)
            report("checking", recognizer.noun)
            verdicts |= recognizer.verify_all(tree, transcriptions, remaining)
        tex, passed = recognizer.write_back(tex, verdicts, tree)
    except CropError as error:
        return result, str(error)
    main.write_text(tex, encoding="utf-8")
    return result.model_copy(update={"passed": passed}), ""


def _omlx_config() -> OmlxConfig:
    config, _detail = load_config()
    return config.omlx if config is not None else OmlxConfig()


def _local_formulas(paper_workdir: Workdir, config: OmlxConfig, model_id: str) -> Local:
    def recognize(
        tree: Path, keys: dict[str, RegionKind], report: Report, warnings: list[str]
    ) -> tuple[dict[str, Verdict], dict[str, RegionKind]]:
        session_dir = _session_dir(paper_workdir, FORMULA_SKILL)
        formula.prepare(tree, session_dir, keys)
        images = {key: recognition.image_path(session_dir, key) for key in keys}
        report(OMLX_STATUS, f"0/{len(keys)}")
        replies, failures = omlx.transcribe_all(
            config, model_id, images, lambda done: report(OMLX_STATUS, f"{done}/{len(keys)}")
        )
        if failures:
            warnings.append(omlx.failure_warning(failures, len(keys)))
        report("checking", FORMULAS.noun)
        passed = {key: verdict for key, verdict in omlx.verify_replies(tree, replies, keys).items() if verdict.passed}
        return passed, {key: kind for key, kind in keys.items() if key not in passed}

    return recognize


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
    status = SESSION_STATUS.format(name=skill.removeprefix("reconstruct_"))
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
