from __future__ import annotations

import shutil
import traceback
from collections.abc import Callable
from pathlib import Path

from .. import compiling, pipeline
from ..artifacts.common import Session
from ..artifacts.reconstruct import CheckReport, ReconstructManifest, ReconstructStatus, RegionKind, RegionsFile
from ..manifests import describe_error, write_manifest
from ..reconstruct import checks, packing
from ..reconstruct.rebuild import MAIN_FILENAME, rebuild
from ..workdir import Workdir

STAGE_NAME = "reconstruct"

SKILL = "reconstruct_fix"

ORIGINAL_FILENAME = "original.pdf"

PACKED_DIRNAME = "packed"

LOST_GLYPH_EXAMPLES = 8

Report = Callable[[str, str], None]


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
    except Exception as error:
        return _failed(f"the rule rebuild raised {describe_error(error)} at {_location(error)}")
    counts = {kind.value: 0 for kind in RegionKind}
    for region in regions.regions:
        counts[region.kind.value] += 1
    warnings: list[str] = []
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
        lost = checks.lost_glyphs(tree)
        if lost:
            examples = ", ".join(f"{name} lacks {glyphs}" for name, glyphs in lost[:LOST_GLYPH_EXAMPLES])
            warnings.append(f"{len(lost)} inline crops lack glyphs of their text-layer hints: {examples}")
        failure = _pack(paper_workdir, tree, final_check.pages, report, warnings)
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
    )


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
    return checks.check(original, tree, attempt.report, regions).report, ""


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
