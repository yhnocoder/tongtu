from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from .. import compiling, masking, pipeline, validation
from ..artifacts.common import CompileReport, FixSession
from ..artifacts.compile import CompileManifest, CompileStatus
from ..artifacts.mask import BlocksFile
from ..artifacts.precompile import PrecompileManifest
from ..artifacts.survey import BriefFile
from ..manifests import describe_error, load_manifest, write_manifest
from ..workdir import ENCODING, Workdir

STAGE_NAME = "compile"

ROLE = "compile_fix"

PRECOMPILE_STAGE_NAME = "precompile"

FONTS_DIRNAME = "fonts"

PAGE_RATIO_MIN = 0.7

PAGE_RATIO_MAX = 1.3

COUNT_FIELDS: tuple[str, ...] = (
    "overfull_hboxes",
    "undefined_references",
    "undefined_citations",
    "missing_characters",
)


def run(
    paper_workdir: Workdir,
    *,
    model_override: str | None = None,
    effort: str | None = None,
    report: Callable[[str, str], None] | None = None,
) -> CompileManifest:
    paper_workdir.create()
    pipeline.clean(paper_workdir, STAGE_NAME)
    manifest = _execute(paper_workdir, model_override, effort, report or (lambda status, summary: None))
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _execute(
    paper_workdir: Workdir, model_override: str | None, effort: str | None, report: Callable[[str, str], None]
) -> CompileManifest:
    warnings: list[str] = []
    precompile_manifest = load_manifest(paper_workdir.manifest_path(PRECOMPILE_STAGE_NAME), PrecompileManifest)
    if precompile_manifest is None or precompile_manifest.report is None:
        return _failed("build/manifests/precompile.json is missing or carries no report; run precompile first.")
    baseline = precompile_manifest.report
    fonts_dir = paper_workdir.fonts
    if not fonts_dir.is_dir():
        return _failed(
            f"{fonts_dir} does not exist; the compile tree takes its fonts from the precompile output build/fonts/. "
            f"Rerun with --from {PRECOMPILE_STAGE_NAME}.",
            baseline=baseline,
        )
    if not any(paper_workdir.reviewed.glob("*.tex")):
        return _failed(
            f"build/{paper_workdir.reviewed.name}/ holds no chunk file; run review first.", baseline=baseline
        )
    try:
        brief = BriefFile.model_validate_json(_read(paper_workdir.brief))
        blocks_file = BlocksFile.model_validate_json(_read(paper_workdir.blocks))
        masked = _read(paper_workdir.masked)
        chunk_ids = [chunk.id for chunk in brief.chunks]
        sources = [_read(paper_workdir.chunks / f"{chunk_id}.tex") for chunk_id in chunk_ids]
        reviewed = [_read(paper_workdir.reviewed / f"{chunk_id}.tex") for chunk_id in chunk_ids]
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        return _failed(describe_error(error), baseline=baseline)
    if "".join(sources) != masked:
        return _failed(
            f"build/{paper_workdir.chunks.name}/ concatenated in the order of {paper_workdir.brief.name} does not equal "
            f"{paper_workdir.masked.name} character for character; rerun with --from survey.",
            baseline=baseline,
        )

    try:
        unmasked = masking.unmask("".join(reviewed), blocks_file.blocks, blocks_file.captions)
    except masking.MaskError as error:
        return _failed(f"unmask failed: {error}", baseline=baseline)
    if unmasked.fallbacks:
        warnings.append(
            f"{len(unmasked.fallbacks)} captions kept their original text because their tokens are absent "
            f"from the translation: {', '.join(unmasked.fallbacks)}"
        )

    tree = paper_workdir.sandbox(STAGE_NAME)
    zh_name = paper_workdir.zh_tex.name
    warnings.extend(compiling.copy_src_tree(paper_workdir.src, tree, zh_name))
    (tree / zh_name).write_text(unmasked.text, encoding=ENCODING)
    shutil.copytree(fonts_dir, tree / FONTS_DIRNAME, dirs_exist_ok=True)

    final, fix_session, failure = compiling.compile_with_fix(
        ROLE,
        tree,
        zh_name,
        paper_workdir.compile_fix_log,
        warnings,
        model_override,
        effort,
        report,
    )
    if final is None or failure:
        return _failed(failure, warnings, baseline, fix_session)

    compile_report = final.report
    warnings.extend(_count_increases(compile_report, baseline))
    zh_final = (tree / zh_name).read_text(encoding=ENCODING)
    problems = _exit_problems(paper_workdir, unmasked.text, zh_final, compile_report, baseline)
    if problems:
        return _failed("; ".join(problems), warnings, baseline, fix_session, compile_report)

    paper_workdir.zh_tex.write_text(zh_final, encoding=ENCODING)
    paper_workdir.out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tree / paper_workdir.zh_pdf.name, paper_workdir.zh_pdf)
    return CompileManifest(
        status=CompileStatus.OK,
        report=compile_report,
        baseline=baseline,
        fix_session=fix_session,
        warnings=warnings,
    )


def _exit_problems(
    paper_workdir: Workdir, unmasked: str, zh_final: str, compile_report: CompileReport, baseline: CompileReport
) -> list[str]:
    problems: list[str] = []
    failure = validation.check_control_sequences(validation.scan(unmasked), validation.scan(zh_final))
    if failure is not None:
        problems.append(
            f"control sequences in {paper_workdir.zh_tex.name} differ from the unmasked translation: {failure.message}"
        )
    ratio = compile_report.pages / baseline.pages
    if not PAGE_RATIO_MIN <= ratio <= PAGE_RATIO_MAX:
        problems.append(
            f"{compile_report.pages} pages against a baseline of {baseline.pages} (ratio {ratio:.2f}) "
            f"falls outside [{PAGE_RATIO_MIN}, {PAGE_RATIO_MAX}]"
        )
    return problems


def _count_increases(compile_report: CompileReport, baseline: CompileReport) -> list[str]:
    return [
        f"{name} rose from {getattr(baseline, name)} in the baseline to {getattr(compile_report, name)}"
        for name in COUNT_FIELDS
        if getattr(compile_report, name) > getattr(baseline, name)
    ]


def _failed(
    message: str,
    warnings: list[str] | None = None,
    baseline: CompileReport | None = None,
    fix_session: FixSession | None = None,
    report: CompileReport | None = None,
) -> CompileManifest:
    return CompileManifest(
        status=CompileStatus.COMPILE_FAILED,
        report=report,
        baseline=baseline,
        fix_session=fix_session,
        warnings=warnings or [],
        message=message,
    )


def _read(path: Path) -> str:
    return path.read_text(encoding=ENCODING)
