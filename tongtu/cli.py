from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, validation
from .artifacts.chunk import ChunkManifest, ChunkStatus
from .artifacts.fetch import FetchStatus
from .artifacts.flatten import FlattenManifest, FlattenStatus
from .artifacts.mask import MaskManifest, MaskStatus
from .artifacts.precompile import PrecompileManifest, PrecompileStatus
from .artifacts.survey import AbstractSource, GlossaryInputRecord, SurveyManifest, SurveyStatus
from .artifacts.translate import ChunkTranslateStatus, TranslateManifest, TranslateStatus
from .assets import asset_path
from .chunking import Part
from .masking import BlockCategory
from .model.config import DEFAULT_ASK_MODEL, MODELS_TEMPLATE, ModelsConfig, load_config, models_path, provider_key
from .stages import STAGES
from .stages import chunk as chunk_stage
from .stages import fetch as fetch_stage
from .stages import flatten as flatten_stage
from .stages import mask as mask_stage
from .stages import precompile as precompile_stage
from .stages import survey as survey_stage
from .stages import translate as translate_stage
from .workdir import WorkdirError

EXIT_FAILURE = 1

EXIT_USAGE = 2

EXIT_PDF_ONLY = 3

EXIT_STUB = 99

_CHUNK_ID_RE = re.compile(r"^c[0-9]{3,}$")

TOOLCHAIN_CHECKS: tuple[tuple[str, str], ...] = (
    ("xelatex", "编译引擎（latexmk -xelatex）"),
    ("latexmk", "编译回环驱动"),
    ("latexpand", "展开多文件源码"),
)

FONT_CHECK_NAME = "中文字体"
CONFIG_CHECK_NAME = "models.toml"

FONTS_DIR = asset_path("fonts")

REQUIRED_FONT_FILENAMES: tuple[str, ...] = ("LXGWWenKai-Light.ttf", "LXGWWenKai-Medium.ttf")

StageName = Enum("StageName", {name: name for name in STAGES}, type=str)

console = Console(markup=False, soft_wrap=True)
error_console = Console(stderr=True, markup=False, soft_wrap=True)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="基于 LaTeX 源码的 arXiv 论文英译中引擎。",
)

tex_app = typer.Typer(
    no_args_is_help=True,
    help="编译修复会话可调用的命令，不面向人；会话现场见 stages/compile.md。",
)
app.add_typer(tex_app, name="tex", hidden=True)


PaperArg = Annotated[str, typer.Argument(metavar="ID", help="arXiv id（或本地源码目录名）")]
GlossaryOpt = Annotated[
    list[Path] | None,
    typer.Option("--glossary", metavar="FILE", help="input glossary，可多次；优先级高于论文目录内与全局表"),
]
WorkdirOpt = Annotated[
    Path | None,
    typer.Option(
        "--workdir", metavar="DIR", help="论文工作目录（默认 $TONGTU_HOME/<id> 或 ~/.local/share/tongtu/<id>）"
    ),
]
JsonOpt = Annotated[
    bool,
    typer.Option("--json", help="向 stdout 输出机器可读事件流"),
]
AgentOpt = Annotated[
    str | None, typer.Option("--agent", metavar="NAME", help="agent 运行时适配器（注册表在 tongtu/agent/）")
]
ModelOpt = Annotated[
    str | None,
    typer.Option(
        "--model", metavar="ID", help="模型标识，透传给 agent 运行时；translate 用它做跳过判定（换模型即整篇重翻）"
    ),
]
JobsOpt = Annotated[
    int,
    typer.Option("--jobs", min=1, metavar="N", help="translate 的 chunk 级并发度（默认 4，上限由 API 速率限制决定）"),
]
MaxFallbackRatioOpt = Annotated[
    float,
    typer.Option(
        "--max-fallback-ratio",
        min=0.0,
        max=1.0,
        metavar="R",
        help="translate 的回退比例阈值（默认 0.2），超过它整体判失败、不进入 compile",
    ),
]


def _print_version(value: bool) -> None:
    if value:
        console.print(f"tongtu {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool, typer.Option("--version", help="打印版本号并退出", callback=_print_version, is_eager=True)
    ] = False,
) -> None:
    return None


def _stub_exit(command: str, **fields: object) -> typer.Exit:
    console.print(f"tongtu {command}：占位实现，未执行任何操作（退出码 {EXIT_STUB}）")
    table = Table(show_header=False, box=None, pad_edge=False)
    for key, value in fields.items():
        table.add_row(f"  {key}", "—" if value in (None, [], ()) else str(value))
    console.print(table)
    return typer.Exit(EXIT_STUB)


@app.command()
def run(
    paper: Annotated[str, typer.Argument(metavar="ARXIV_ID|DIR", help="arXiv id 或本地源码目录")],
    glossary: GlossaryOpt = None,
    workdir: WorkdirOpt = None,
    force: Annotated[bool, typer.Option("--force", help="无视缓存 full rerun")] = False,
    json_output: JsonOpt = False,
    agent: AgentOpt = None,
    model: ModelOpt = None,
) -> None:
    raise _stub_exit(
        "run",
        paper=paper,
        workdir=workdir,
        glossary=glossary,
        force=force,
        json=json_output,
        agent=agent,
        model=model,
    )


@app.command()
def retranslate(
    paper: PaperArg,
    chunks: Annotated[str | None, typer.Option("--chunks", metavar="c012,c045", help="指定 chunk id，逗号分隔")] = None,
    term: Annotated[str | None, typer.Option("--term", metavar="WORD", help="重翻命中该术语的 chunk")] = None,
    all_chunks: Annotated[
        bool, typer.Option("--all", help="full retranslation（改 style rules / 换模型时的显式操作）")
    ] = False,
    glossary: GlossaryOpt = None,
    workdir: WorkdirOpt = None,
    json_output: JsonOpt = False,
    agent: AgentOpt = None,
    model: ModelOpt = None,
) -> None:
    if sum([chunks is not None, term is not None, all_chunks]) != 1:
        raise typer.BadParameter("--chunks / --term / --all 三者必须恰好给一个")
    chunk_ids: list[str] = []
    if chunks is not None:
        chunk_ids = [part.strip() for part in chunks.split(",") if part.strip()]
        if not chunk_ids:
            raise typer.BadParameter("--chunks 要求至少一个 chunk id（形如 c012,c045）")
        bad = [c for c in chunk_ids if not _CHUNK_ID_RE.fullmatch(c)]
        if bad:
            raise typer.BadParameter(f"chunk id 形如 c012，不合法：{'、'.join(bad)}")
    raise _stub_exit(
        "retranslate",
        paper=paper,
        chunks=chunk_ids or None,
        term=term,
        all=all_chunks,
        glossary=glossary,
        workdir=workdir,
        json=json_output,
        agent=agent,
        model=model,
    )


@app.command()
def stage(
    name: Annotated[StageName, typer.Argument(help="阶段名")],
    paper: Annotated[str, typer.Argument(metavar="PAPER", help="arXiv 编号 / arXiv 链接 / 本地源码目录")],
    glossary: GlossaryOpt = None,
    workdir: WorkdirOpt = None,
    force: Annotated[bool, typer.Option("--force", help="无视已有 manifest 结论重新执行")] = False,
    json_output: JsonOpt = False,
    agent: AgentOpt = None,
    model: ModelOpt = None,
    jobs: JobsOpt = translate_stage.DEFAULT_JOBS,
    max_fallback_ratio: MaxFallbackRatioOpt = translate_stage.DEFAULT_MAX_FALLBACK_RATIO,
) -> None:
    if name.value == fetch_stage.STAGE_NAME:
        raise _run_stage_fetch(paper, workdir, force, json_output)
    if name.value == flatten_stage.STAGE_NAME:
        raise _run_stage_flatten(paper, workdir, force, json_output)
    if name.value == precompile_stage.STAGE_NAME:
        raise _run_stage_precompile(paper, workdir, force, json_output, model)
    if name.value == mask_stage.STAGE_NAME:
        raise _run_stage_mask(paper, workdir, force, json_output)
    if name.value == survey_stage.STAGE_NAME:
        raise _run_stage_survey(paper, glossary or [], workdir, force, json_output)
    if name.value == chunk_stage.STAGE_NAME:
        raise _run_stage_chunk(paper, workdir, force, json_output)
    if name.value == translate_stage.STAGE_NAME:
        raise _run_stage_translate(paper, workdir, force, json_output, model, jobs, max_fallback_ratio)
    raise _stub_exit(
        "stage",
        name=name.value,
        paper=paper,
        glossary=glossary,
        workdir=workdir,
        force=force,
        json=json_output,
        agent=agent,
        model=model,
    )


def _warn_json_ignored(json_output: bool) -> None:
    if json_output:
        error_console.print("--json：事件流 schema 尚未定义，本次忽略该选项")


def _workdir_name_from_paper(paper: str) -> str:
    paper_input = fetch_stage.parse_paper_argument(paper)
    if paper_input.source_dir is not None:
        return paper_input.source_dir.name
    return paper_input.arxiv_id


def _print_skipped(stage_name: str, status: str, manifest_path: Path) -> None:
    console.print(f"{stage_name} 跳过：manifest 已有结论（状态 {status}），--force 可重新执行")
    console.print(f"  manifest  {manifest_path}")


def _upstream_exit_code(*, ok: bool, pdf_only_chain: bool) -> int:
    if ok:
        return 0
    if pdf_only_chain:
        return EXIT_PDF_ONLY
    return EXIT_FAILURE


def _run_stage_fetch(paper: str, workdir: Path | None, force: bool, json_output: bool) -> typer.Exit:
    _warn_json_ignored(json_output)
    try:
        paper_input = fetch_stage.parse_paper_argument(paper)
        if paper_input.source_dir is not None:
            result = fetch_stage.fetch_local(paper_input.source_dir, workdir)
        else:
            result = fetch_stage.fetch_remote(paper_input.arxiv_id, workdir, force=force)
    except (fetch_stage.PaperArgumentError, WorkdirError) as error:
        raise typer.BadParameter(str(error)) from error
    manifest = result.manifest
    manifest_path = result.workdir.manifest_path(fetch_stage.STAGE_NAME)
    if result.skipped:
        _print_skipped(fetch_stage.STAGE_NAME, manifest.status, manifest_path)
        return typer.Exit(_fetch_exit_code(manifest.status))
    console.print(f"fetch：状态 {manifest.status}")
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column()
    table.add_column(overflow="fold")
    table.add_row("  kind", manifest.kind or "—")
    table.add_row("  src", str(result.workdir.src))
    table.add_row(
        "  文件数", f"{len(manifest.files)}（.tex {len(manifest.tex_files)} 个，共 {manifest.tex_chars} 字符）"
    )
    table.add_row("  manifest", str(manifest_path))
    if manifest.message:
        table.add_row("  message", manifest.message)
    for line in manifest.warnings:
        table.add_row("  warning", line)
    for member_name in manifest.rejected:
        table.add_row("  rejected", member_name)
    console.print(table)
    return typer.Exit(_fetch_exit_code(manifest.status))


def _fetch_exit_code(status: FetchStatus) -> int:
    if status is FetchStatus.OK:
        return 0
    if status is FetchStatus.PDF_ONLY:
        return EXIT_PDF_ONLY
    return EXIT_FAILURE


def _run_stage_flatten(paper: str, workdir: Path | None, force: bool, json_output: bool) -> typer.Exit:
    _warn_json_ignored(json_output)
    try:
        result = flatten_stage.flatten(_workdir_name_from_paper(paper), workdir, force=force)
    except (fetch_stage.PaperArgumentError, WorkdirError) as error:
        raise typer.BadParameter(str(error)) from error
    manifest = result.manifest
    manifest_path = result.workdir.manifest_path(flatten_stage.STAGE_NAME)
    if result.skipped:
        _print_skipped(flatten_stage.STAGE_NAME, manifest.status, manifest_path)
        return typer.Exit(_flatten_exit_code(manifest))
    console.print(f"flatten：状态 {manifest.status}")
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column()
    table.add_column(overflow="fold")
    table.add_row("  main_file", manifest.main_file or "—")
    table.add_row("  bbl", f"已内联 {manifest.bbl_file}" if manifest.bbl_file else "未内联")
    flat_path = result.workdir.build / flatten_stage.FLAT_FILENAME
    if manifest.status is FlattenStatus.OK:
        table.add_row("  flat.tex", f"{flat_path}（{manifest.flat_bytes} 字节）")
    else:
        table.add_row("  flat.tex", "未写出")
    table.add_row("  manifest", str(manifest_path))
    if manifest.message:
        table.add_row("  message", manifest.message)
    for line in manifest.warnings:
        table.add_row("  warning", line)
    console.print(table)
    return typer.Exit(_flatten_exit_code(manifest))


def _flatten_exit_code(manifest: FlattenManifest) -> int:
    return _upstream_exit_code(
        ok=manifest.status is FlattenStatus.OK,
        pdf_only_chain=(
            manifest.status is FlattenStatus.FETCH_NOT_OK and manifest.fetch_status == FetchStatus.PDF_ONLY
        ),
    )


def _run_stage_precompile(
    paper: str, workdir: Path | None, force: bool, json_output: bool, model: str | None
) -> typer.Exit:
    _warn_json_ignored(json_output)
    try:
        result = precompile_stage.precompile(_workdir_name_from_paper(paper), workdir, force=force, model=model)
    except (fetch_stage.PaperArgumentError, WorkdirError) as error:
        raise typer.BadParameter(str(error)) from error
    manifest = result.manifest
    manifest_path = result.workdir.manifest_path(precompile_stage.STAGE_NAME)
    if result.skipped:
        _print_skipped(precompile_stage.STAGE_NAME, manifest.status, manifest_path)
        return typer.Exit(_precompile_exit_code(manifest))
    console.print(f"precompile：状态 {manifest.status}")
    compiled = manifest.status is PrecompileStatus.OK
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column()
    table.add_column(overflow="fold")
    table.add_row("  pages", str(manifest.pages) if compiled else "—")
    table.add_row("  overfull_hboxes", str(manifest.overfull_hboxes) if compiled else "—")
    table.add_row("  undefined_references", str(manifest.undefined_references) if compiled else "—")
    table.add_row("  undefined_citations", str(manifest.undefined_citations) if compiled else "—")
    table.add_row("  missing_characters", str(manifest.missing_characters) if compiled else "—")
    table.add_row("  耗时", f"{manifest.duration_seconds:.1f} 秒" if manifest.duration_seconds else "—")
    pdf_path = result.workdir.build / precompile_stage.PRECOMPILE_DIRNAME / precompile_stage.PDF_FILENAME
    precompile_path = result.workdir.build / precompile_stage.PRECOMPILE_FILENAME
    if compiled:
        table.add_row("  flat.pdf", f"{pdf_path}（{manifest.pdf_bytes} 字节）")
        table.add_row("  precompile.tex", f"{precompile_path}（{manifest.precompile_bytes} 字节）")
    else:
        table.add_row("  flat.pdf", "未产出")
        table.add_row("  precompile.tex", "未产出")
    if manifest.fix_session:
        table.add_row(
            "  修复会话",
            f"已拉起（{manifest.session_stop_reason}，{manifest.session_duration_seconds:.0f} 秒，"
            f"模型 {manifest.session_model}）",
        )
    else:
        table.add_row("  修复会话", "未拉起")
    for changed in manifest.changed_files:
        table.add_row("  changed_file", changed)
    table.add_row("  manifest", str(manifest_path))
    if manifest.message:
        table.add_row("  message", manifest.message)
    for line in manifest.warnings:
        table.add_row("  warning", line)
    console.print(table)
    return typer.Exit(_precompile_exit_code(manifest))


def _precompile_exit_code(manifest: PrecompileManifest) -> int:
    return _upstream_exit_code(
        ok=manifest.status is PrecompileStatus.OK,
        pdf_only_chain=(
            manifest.status is PrecompileStatus.FLATTEN_NOT_OK and manifest.fetch_status == FetchStatus.PDF_ONLY
        ),
    )


def _run_stage_mask(paper: str, workdir: Path | None, force: bool, json_output: bool) -> typer.Exit:
    _warn_json_ignored(json_output)
    try:
        result = mask_stage.mask(_workdir_name_from_paper(paper), workdir, force=force)
    except (fetch_stage.PaperArgumentError, WorkdirError) as error:
        raise typer.BadParameter(str(error)) from error
    manifest = result.manifest
    manifest_path = result.workdir.manifest_path(mask_stage.STAGE_NAME)
    if result.skipped:
        _print_skipped(mask_stage.STAGE_NAME, manifest.status, manifest_path)
        return typer.Exit(_mask_exit_code(manifest))
    console.print(f"mask：状态 {manifest.status}")
    masked = manifest.status is MaskStatus.OK
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column()
    table.add_column(overflow="fold")
    table.add_row("  blocks", str(manifest.blocks_total) if masked else "—")
    table.add_row("  captions", str(manifest.captions_total) if masked else "—")
    table.add_row("  unknown 环境", _unknown_environments(manifest) if masked else "—")
    table.add_row("  掩码保留比", f"{manifest.masked_chars_ratio:.1%}" if masked else "—")
    if masked:
        table.add_row(
            "  precompile.tex",
            f"{mask_stage.precompile_path(result.workdir)}（{manifest.precompile_chars} 字符）",
        )
        table.add_row("  masked.tex", f"{mask_stage.masked_path(result.workdir)}（{manifest.masked_chars} 字符）")
        table.add_row("  blocks.json", str(mask_stage.blocks_path(result.workdir)))
    else:
        table.add_row("  precompile.tex", "—")
        table.add_row("  masked.tex", "未产出")
        table.add_row("  blocks.json", "未产出")
    table.add_row("  manifest", str(manifest_path))
    if manifest.message:
        table.add_row("  message", manifest.message)
    for line in manifest.warnings:
        table.add_row("  warning", line)
    console.print(table)
    return typer.Exit(_mask_exit_code(manifest))


def _unknown_environments(manifest: MaskManifest) -> str:
    listed = [
        f"{name}（{decision.blocks} 块）"
        for name, decision in manifest.environments.items()
        if decision.category == BlockCategory.UNKNOWN and decision.blocks > 0
    ]
    return "、".join(listed) if listed else "—"


def _mask_exit_code(manifest: MaskManifest) -> int:
    return _upstream_exit_code(
        ok=manifest.status is MaskStatus.OK,
        pdf_only_chain=(
            manifest.status is MaskStatus.PRECOMPILE_NOT_OK and manifest.fetch_status == FetchStatus.PDF_ONLY
        ),
    )


def _run_stage_survey(
    paper: str, glossary_paths: list[Path], workdir: Path | None, force: bool, json_output: bool
) -> typer.Exit:
    _warn_json_ignored(json_output)
    try:
        result = survey_stage.survey(
            _workdir_name_from_paper(paper), workdir, glossary_paths=glossary_paths, force=force
        )
    except (fetch_stage.PaperArgumentError, WorkdirError) as error:
        raise typer.BadParameter(str(error)) from error
    manifest = result.manifest
    manifest_path = result.workdir.manifest_path(survey_stage.STAGE_NAME)
    if result.skipped:
        _print_skipped(survey_stage.STAGE_NAME, manifest.status, manifest_path)
        return typer.Exit(_survey_exit_code(manifest))
    console.print(f"survey：状态 {manifest.status}")
    surveyed = manifest.status is SurveyStatus.OK
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column()
    table.add_column(overflow="fold")
    for record in manifest.glossary_inputs:
        table.add_row(f"  {record.layer} 术语表", _glossary_input(record))
    table.add_row("  terms", str(manifest.terms_total) if surveyed else "—")
    table.add_row("  do_not_translate", str(manifest.do_not_translate_total) if surveyed else "—")
    table.add_row("  过滤掉的词条", _filtered_terms(manifest) if surveyed else "—")
    if surveyed:
        table.add_row(
            "  abstract",
            f"{manifest.abstract_source}（{manifest.abstract_chars} 字符）"
            if manifest.abstract_source is not AbstractSource.ABSENT
            else "未提取到（不是失败）",
        )
        table.add_row("  glossary.json", str(survey_stage.glossary_file_path(result.workdir)))
        table.add_row("  brief.json", str(survey_stage.brief_path(result.workdir)))
    else:
        table.add_row("  abstract", "—")
        table.add_row("  glossary.json", "未产出")
        table.add_row("  brief.json", "未产出")
    table.add_row("  manifest", str(manifest_path))
    if manifest.message:
        table.add_row("  message", manifest.message)
    for line in manifest.warnings:
        table.add_row("  warning", line)
    console.print(table)
    return typer.Exit(_survey_exit_code(manifest))


def _glossary_input(record: GlossaryInputRecord) -> str:
    if not record.path:
        return "未给出"
    return record.path if record.present else f"{record.path}（不存在）"


def _filtered_terms(manifest: SurveyManifest) -> str:
    listed = [f"{entry.word}（{entry.decided_by}）" for entry in manifest.filtered]
    return "、".join(listed) if listed else "—"


def _survey_exit_code(manifest: SurveyManifest) -> int:
    return _upstream_exit_code(
        ok=manifest.status is SurveyStatus.OK,
        pdf_only_chain=(manifest.status is SurveyStatus.MASK_NOT_OK and manifest.fetch_status == FetchStatus.PDF_ONLY),
    )


def _run_stage_chunk(paper: str, workdir: Path | None, force: bool, json_output: bool) -> typer.Exit:
    _warn_json_ignored(json_output)
    try:
        result = chunk_stage.chunk(_workdir_name_from_paper(paper), workdir, force=force)
    except (fetch_stage.PaperArgumentError, WorkdirError) as error:
        raise typer.BadParameter(str(error)) from error
    manifest = result.manifest
    manifest_path = result.workdir.manifest_path(chunk_stage.STAGE_NAME)
    if result.skipped:
        _print_skipped(chunk_stage.STAGE_NAME, manifest.status, manifest_path)
        return typer.Exit(_chunk_exit_code(manifest))
    console.print(f"chunk：状态 {manifest.status}")
    chunked = manifest.status is ChunkStatus.OK
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column()
    table.add_column(overflow="fold")
    table.add_row("  chunk 数", _chunk_counts(manifest) if chunked else "—")
    table.add_row("  token 估算", _token_spread(manifest) if chunked else "—")
    table.add_row("  首选层级", (manifest.heading_level or "无标题（退化路径）") if chunked else "—")
    table.add_row("  透明环境", "、".join(manifest.transparent_environments) or "—" if chunked else "—")
    table.add_row("  appendix", str(manifest.appendix_source) if chunked else "—")
    table.add_row("  chunks/", str(chunk_stage.chunks_dir(result.workdir)) if chunked else "未产出")
    table.add_row("  manifest", str(manifest_path))
    if manifest.message:
        table.add_row("  message", manifest.message)
    for line in manifest.warnings:
        table.add_row("  warning", line)
    console.print(table)
    return typer.Exit(_chunk_exit_code(manifest))


def _chunk_counts(manifest: ChunkManifest) -> str:
    counts = Counter(record.part for record in manifest.chunks)
    listed = [f"{part} {counts[part]}" for part in Part if counts[part]]
    return f"{manifest.chunks_total}（{'、'.join(listed)}）"


def _token_spread(manifest: ChunkManifest) -> str:
    if not manifest.chunks:
        return "—"
    estimates = sorted(record.token_estimate for record in manifest.chunks)
    return f"最小 {estimates[0]}、中位 {estimates[len(estimates) // 2]}、最大 {estimates[-1]}"


def _chunk_exit_code(manifest: ChunkManifest) -> int:
    return _upstream_exit_code(
        ok=manifest.status is ChunkStatus.OK,
        pdf_only_chain=(manifest.status is ChunkStatus.MASK_NOT_OK and manifest.fetch_status == FetchStatus.PDF_ONLY),
    )


def _run_stage_translate(
    paper: str,
    workdir: Path | None,
    force: bool,
    json_output: bool,
    model: str | None,
    jobs: int,
    max_fallback_ratio: float,
) -> typer.Exit:
    _warn_json_ignored(json_output)
    try:
        result = translate_stage.translate(
            _workdir_name_from_paper(paper),
            workdir,
            model=model,
            jobs=jobs,
            max_fallback_ratio=max_fallback_ratio,
            force=force,
        )
    except (fetch_stage.PaperArgumentError, WorkdirError) as error:
        raise typer.BadParameter(str(error)) from error
    manifest = result.manifest
    manifest_path = result.workdir.manifest_path(translate_stage.STAGE_NAME)
    if result.skipped:
        _print_skipped(translate_stage.STAGE_NAME, manifest.status, manifest_path)
        return typer.Exit(_translate_exit_code(manifest))
    console.print(f"translate：状态 {manifest.status}")
    translated = bool(manifest.chunks)
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column()
    table.add_column(overflow="fold")
    table.add_row("  模型", f"{manifest.model_id}（prompt 版本 {manifest.prompt_version}，并发 {manifest.jobs}）")
    table.add_row("  chunk", _translate_counts(manifest) if translated else "—")
    table.add_row("  ask 调用", _translate_attempts(manifest) if translated else "—")
    table.add_row("  回退比例", f"{manifest.fallback_ratio:.0%}（阈值 {manifest.max_fallback_ratio:.0%}）")
    table.add_row("  translated/", str(translate_stage.translated_dir(result.workdir)) if translated else "未产出")
    table.add_row("  manifest", str(manifest_path))
    console.print(table)
    for record in manifest.chunks:
        if record.status is ChunkTranslateStatus.FALLBACK:
            error_console.print(f"{record.id} 回退原文：{'；'.join(record.failures) or '没有可用的失败现场'}")
    if manifest.message:
        error_console.print(manifest.message)
    return typer.Exit(_translate_exit_code(manifest))


def _translate_counts(manifest: TranslateManifest) -> str:
    counts = Counter(record.status for record in manifest.chunks)
    listed = [f"{status}={counts[status]}" for status in ChunkTranslateStatus if counts[status]]
    return f"{manifest.chunks_total}（{'、'.join(listed)}）"


def _translate_attempts(manifest: TranslateManifest) -> str:
    total = sum(record.attempts for record in manifest.chunks)
    retried = [f"{record.id}×{record.attempts}" for record in manifest.chunks if record.attempts > 1]
    return f"{total} 次" + (f"（重试过：{'、'.join(retried)}）" if retried else "（无重试）")


def _translate_exit_code(manifest: TranslateManifest) -> int:
    return _upstream_exit_code(
        ok=manifest.status is TranslateStatus.OK,
        pdf_only_chain=(
            manifest.status in (TranslateStatus.CHUNK_NOT_OK, TranslateStatus.SURVEY_NOT_OK)
            and manifest.fetch_status == FetchStatus.PDF_ONLY
        ),
    )


@app.command()
def validate(
    src: Annotated[Path, typer.Argument(help="原文 chunk 文件")],
    dst: Annotated[Path, typer.Argument(help="译文文件")],
) -> None:
    try:
        source = src.read_text(encoding="utf-8")
        translation = dst.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        error_console.print(f"读不到文件：{error}")
        raise typer.Exit(EXIT_FAILURE) from error
    result = validation.validate(source.strip(), translation.strip())
    failures = {failure.check: failure.message for failure in result.failures}
    for layer in validation.CHECK_NAMES:
        if layer in failures:
            console.print(f"  [失败] {layer}：{failures[layer]}")
        else:
            console.print(f"  [通过] {layer}")
    raise typer.Exit(0 if result.ok else EXIT_FAILURE)


@app.command()
def doctor() -> None:
    absent_toolchain = _print_doctor_rows(_toolchain_rows())
    absent_config = _print_doctor_rows(_config_rows())
    if absent_toolchain:
        console.print(f"环境有缺失： {'、'.join(absent_toolchain)}")
        raise typer.Exit(EXIT_FAILURE)
    if absent_config:
        console.print(f"工具链与字体齐全； {'、'.join(absent_config)} 未配置， survey 起的阶段无法执行。")
        return
    console.print("环境齐全。")


def _print_doctor_rows(rows: list[tuple[str, str, bool, str]]) -> list[str]:
    for name, purpose, found, detail in rows:
        console.print(f"  [{'通过' if found else '缺失'}] {name} —— {purpose}  {detail}")
    return [name for name, _purpose, found, _detail in rows if not found]


def _toolchain_rows() -> list[tuple[str, str, bool, str]]:
    rows = [(name, purpose, *_check_executable(name)) for name, purpose in TOOLCHAIN_CHECKS]
    rows.append((FONT_CHECK_NAME, "font fallback chain（霞鹜文楷随仓库分发）", *_check_fonts()))
    return rows


def _config_rows() -> list[tuple[str, str, bool, str]]:
    config, detail = load_config()
    if config is None:
        return [
            (CONFIG_CHECK_NAME, "服务商、运行时与角色的配置", False, detail),
            ("密钥", "各服务商的密钥环境变量", False, "models.toml 读不到，无法检查"),
            ("运行时", "各运行时的可执行文件", False, "models.toml 读不到，无法检查"),
        ]
    rows = [(CONFIG_CHECK_NAME, "服务商、运行时与角色的配置", True, str(models_path()))]
    for name in _roles_refer_to(config, "provider"):
        provider = config.provider.get(name)
        if provider is None:
            rows.append((f"密钥 {name}", "角色引用的服务商", False, f"models.toml 里没有声明服务商 {name}"))
            continue
        key, detail = provider_key(name, provider)
        rows.append((f"密钥 {name}", "服务商的 API 密钥", key is not None, detail))
    for name in _roles_refer_to(config, "runtime"):
        runtime = config.runtime.get(name)
        if runtime is None:
            rows.append((f"运行时 {name}", "角色引用的运行时", False, f"models.toml 里没有声明运行时 {name}"))
            continue
        rows.append((f"运行时 {name}", "会话运行时的可执行文件", *_check_executable(runtime.command[0])))
    return rows


def _roles_refer_to(config: ModelsConfig, field: str) -> list[str]:
    return list(dict.fromkeys(name for entry in config.roles.values() if (name := getattr(entry, field))))


def _check_executable(name: str) -> tuple[bool, str]:
    path = shutil.which(name)
    if path is None:
        return False, f"PATH 里找不到 {name}"
    return True, path


def _check_fonts() -> tuple[bool, str]:
    absent = [name for name in REQUIRED_FONT_FILENAMES if not (FONTS_DIR / name).is_file()]
    if absent:
        return False, f"{FONTS_DIR} 下缺 {'、'.join(absent)}"
    return True, str(FONTS_DIR)


@app.command()
def setup(
    interactive: Annotated[bool, typer.Option("-i", help="交互选服务商并填 API key")] = False,
) -> None:
    path = models_path()
    if path.exists():
        console.print(f"配置文件 {path} 已存在， 不覆盖。 要改配置直接编辑这个文件。")
        return
    text = _interactive_models_toml() if interactive else MODELS_TEMPLATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    console.print(f"已写出 {path} 。")


def _interactive_models_toml() -> str:
    template = tomllib.loads(MODELS_TEMPLATE)
    keys: dict[str, str] = {}
    for name in template["provider"]:
        if typer.confirm(f"配置 {name}？", default=False):
            keys[name] = typer.prompt(f"{name} 的 API key", hide_input=True)
    if not keys:
        console.print("一个服务商都没选。 至少选一个才能调模型， 重新运行 tongtu setup -i 。")
        raise typer.Exit(EXIT_USAGE)
    ask_roles = [role for role, entry in template["roles"].items() if "provider" in entry]
    return _fill_template(keys, ask_roles)


def _fill_template(keys: dict[str, str], ask_roles: list[str]) -> str:
    chosen = next(iter(keys))
    section = ""
    provider_name = ""
    lines = []
    for line in MODELS_TEMPLATE.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.strip("[]")
            if section.startswith("provider."):
                provider_name = section.split(".")[1]
        elif section.startswith("provider.") and stripped.startswith("api_key ") and provider_name in keys:
            line = line.replace('""', json.dumps(keys[provider_name]), 1)
        elif section == "roles" and stripped.split("=")[0].strip() in ask_roles:
            line = re.sub(r'provider = "[^"]*"', f'provider = "{chosen}"', line)
            line = re.sub(r'model = "[^"]*"', f'model = "{DEFAULT_ASK_MODEL[chosen]}"', line)
        lines.append(line)
    return "\n".join(lines) + "\n"


@app.command()
def preview(
    paper: PaperArg,
    workdir: WorkdirOpt = None,
    serve: Annotated[
        bool,
        typer.Option("--serve", help="起一个本地 http.server 打开（http 下页面走相对路径读 zh.pdf，大文件加载更快）"),
    ] = False,
) -> None:
    raise _stub_exit("preview", paper=paper, workdir=workdir, serve=serve)


@tex_app.command("compile")
def tex_compile() -> None:
    raise _stub_exit("tex compile")


def main() -> None:
    if os.environ.get("TONGTU_DISABLE"):
        error_console.print("tongtu 不能在 agent 会话内运行（TONGTU_DISABLE 已设）")
        raise SystemExit(EXIT_USAGE)
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
