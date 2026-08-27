from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from .. import compiling, masking, validation
from ..artifacts.common import CompileReport, FixSession
from ..artifacts.compile import CompileManifest, CompileStatus
from ..artifacts.mask import BlocksFile
from ..artifacts.precompile import PrecompileManifest
from ..artifacts.survey import BriefFile
from ..manifests import describe_error, load_manifest, write_manifest
from ..workdir import Workdir

STAGE_NAME = "compile"

ROLE = "compile_fix"

PRECOMPILE_STAGE_NAME = "precompile"

SANDBOX_DIRNAME = "sandbox"

FONTS_DIRNAME = "fonts"

CHUNKS_DIRNAME = "chunks"

REVIEWED_DIRNAME = "reviewed"

MASKED_FILENAME = "masked.tex"

BLOCKS_FILENAME = "blocks.json"

BRIEF_FILENAME = "brief.json"

ZH_FILENAME = "zh.tex"

PDF_FILENAME = "zh.pdf"

TRACE_FILENAME = "compile-fix.jsonl"

ENCODING = "utf-8"

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
    _reset_outputs(paper_workdir)
    manifest = _execute(paper_workdir, model_override, effort, report or (lambda status, summary: None))
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _reset_outputs(paper_workdir: Workdir) -> None:
    shutil.rmtree(_tree(paper_workdir), ignore_errors=True)
    (paper_workdir.build / ZH_FILENAME).unlink(missing_ok=True)
    (paper_workdir.out / PDF_FILENAME).unlink(missing_ok=True)
    (paper_workdir.logs / TRACE_FILENAME).unlink(missing_ok=True)


def _execute(
    paper_workdir: Workdir, model_override: str | None, effort: str | None, report: Callable[[str, str], None]
) -> CompileManifest:
    warnings: list[str] = []
    precompile_manifest = load_manifest(paper_workdir.manifest_path(PRECOMPILE_STAGE_NAME), PrecompileManifest)
    if precompile_manifest is None or precompile_manifest.report is None:
        return _failed("build/manifests/precompile.json is missing or carries no report; run precompile first.")
    baseline = precompile_manifest.report
    fonts_dir = paper_workdir.build / SANDBOX_DIRNAME / PRECOMPILE_STAGE_NAME / FONTS_DIRNAME
    if not fonts_dir.is_dir():
        return _failed(
            f"{fonts_dir} does not exist; the compile tree takes its fonts from the precompile tree. "
            f"Rerun with --from {PRECOMPILE_STAGE_NAME}.",
            baseline=baseline,
        )
    if not any((paper_workdir.build / REVIEWED_DIRNAME).glob("*.tex")):
        return _failed(f"build/{REVIEWED_DIRNAME}/ holds no chunk file; run review first.", baseline=baseline)
    try:
        brief = BriefFile.model_validate_json(_read(paper_workdir, BRIEF_FILENAME))
        blocks_file = BlocksFile.model_validate_json(_read(paper_workdir, BLOCKS_FILENAME))
        masked = _read(paper_workdir, MASKED_FILENAME)
        chunk_ids = [chunk.id for chunk in brief.chunks]
        sources = [_read(paper_workdir, f"{CHUNKS_DIRNAME}/{chunk_id}.tex") for chunk_id in chunk_ids]
        reviewed = [_read(paper_workdir, f"{REVIEWED_DIRNAME}/{chunk_id}.tex") for chunk_id in chunk_ids]
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        return _failed(describe_error(error), baseline=baseline)
    if "".join(sources) != masked:
        return _failed(
            f"build/{CHUNKS_DIRNAME}/ concatenated in the order of {BRIEF_FILENAME} does not equal "
            f"{MASKED_FILENAME} character for character; rerun with --from survey.",
            baseline=baseline,
        )

    try:
        unmasked = masking.unmask(
            "".join(reviewed),
            [masking.Block(**block.model_dump()) for block in blocks_file.blocks],
            [masking.Caption(**caption.model_dump()) for caption in blocks_file.captions],
        )
    except masking.MaskError as error:
        return _failed(f"unmask failed: {error}", baseline=baseline)
    if unmasked.fallbacks:
        warnings.append(
            f"{len(unmasked.fallbacks)} captions kept their original text because their tokens are absent "
            f"from the translation: {', '.join(unmasked.fallbacks)}"
        )

    tree = _tree(paper_workdir)
    warnings.extend(compiling.copy_src_tree(paper_workdir.src, tree, ZH_FILENAME))
    (tree / ZH_FILENAME).write_text(unmasked.text, encoding=ENCODING)
    shutil.copytree(fonts_dir, tree / FONTS_DIRNAME, dirs_exist_ok=True)

    report("compiling", ZH_FILENAME)
    try:
        first = compiling.attempt_compile(tree, ZH_FILENAME)
    except OSError as error:
        return _failed(f"failed to run latexmk ({describe_error(error)}).", warnings, baseline)
    if first.outcome.timed_out:
        return _failed(compiling.timeout_message(first), warnings, baseline)

    fix_session: FixSession | None = None
    final = first
    if not first.passed:
        report("fix session", "running")
        fix_session = compiling.fix(
            ROLE,
            paper_workdir.src,
            tree,
            paper_workdir.logs / TRACE_FILENAME,
            ZH_FILENAME,
            warnings,
            model_override,
            effort,
            report=lambda action: report("fix session", action),
        )
        warnings.extend(compiling.clean_tree(tree, ZH_FILENAME))
        report("verifying", ZH_FILENAME)
        try:
            final = compiling.attempt_compile(tree, ZH_FILENAME)
        except OSError as error:
            return _failed(
                f"failed to run latexmk for the verify compile ({describe_error(error)}).",
                warnings,
                baseline,
                fix_session,
            )
        if final.outcome.timed_out:
            return _failed(compiling.timeout_message(final), warnings, baseline, fix_session)
        if not final.passed:
            return _failed(
                f"after the fix session the verify compile still fails the exit checks: "
                f"{compiling.failure_message(final)}",
                warnings,
                baseline,
                fix_session,
            )

    compile_report = compiling.compile_report(final)
    warnings.extend(_count_increases(compile_report, baseline))
    zh_final = (tree / ZH_FILENAME).read_text(encoding=ENCODING)
    problems = _exit_problems(unmasked.text, zh_final, compile_report, baseline)
    if problems:
        return _failed("; ".join(problems), warnings, baseline, fix_session, compile_report)

    (paper_workdir.build / ZH_FILENAME).write_text(zh_final, encoding=ENCODING)
    paper_workdir.out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tree / PDF_FILENAME, paper_workdir.out / PDF_FILENAME)
    return CompileManifest(
        status=CompileStatus.OK,
        report=compile_report,
        baseline=baseline,
        fix_session=fix_session,
        warnings=warnings,
    )


def _exit_problems(unmasked: str, zh_final: str, compile_report: CompileReport, baseline: CompileReport) -> list[str]:
    problems: list[str] = []
    failure = validation.check_control_sequences(validation.scan(unmasked), validation.scan(zh_final))
    if failure is not None:
        problems.append(f"control sequences in {ZH_FILENAME} differ from the unmasked translation: {failure.message}")
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


def _read(paper_workdir: Workdir, relative: str) -> str:
    return (paper_workdir.build / relative).read_text(encoding=ENCODING)


def _tree(paper_workdir: Workdir) -> Path:
    return paper_workdir.build / SANDBOX_DIRNAME / STAGE_NAME
