from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.live import Live
from rich.progress import (
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Column
from rich.text import Text

from . import __version__, arxiv, processes, validation
from .artifacts.common import Manifest
from .artifacts.compile import CompileManifest
from .artifacts.fetch import FetchManifest, PaperMeta
from .artifacts.mask import MaskManifest
from .artifacts.precompile import PrecompileManifest
from .artifacts.reconstruct import ReconstructManifest
from .artifacts.status import (
    ABSENT_STATUS,
    PaperListing,
    PaperState,
    PaperStatus,
    PaperSummary,
    RunCommand,
    RunMarker,
    RunRecord,
    StageState,
)
from .artifacts.translate import TranslateManifest
from .arxiv import PaperArgumentError, PaperInput, parse_paper_argument
from .config import DEV_HOME, HOME_ENV, config_path, home_dir, legacy_dirs_present, papers_dir
from .console import console, error_console
from .fonts import FONTS_DIR, family_files
from .manifests import describe_error, load_manifest, write_manifest
from .model import chat
from .model.config import (
    SECRET_KEYS,
    FontFamily,
    FontsConfig,
    ModelsConfig,
    ModeName,
    Target,
    ensure_config_file,
    load_config,
    mode_target,
    shown_entries,
    updated_config,
    validation_lines,
    write_config,
)
from .model.work import AGENTRUN_EXECUTABLE, agentrun_executable
from .pipeline import STAGES, clean, clean_from, downstream, first_pending, implied_complete, outputs_present
from .processes import OUTPUT_EXCERPT_CHARS
from .stages import compile, fetch, mask, precompile, reconstruct, translate
from .workdir import ENCODING, Workdir, WorkdirError, normalize_arxiv_id, resolve

EXIT_FAILURE = 1

EXIT_USAGE = 2

EXIT_NOT_CONFIGURED = 3

HEADER_STYLE = "bold"

MARK_OK = "✓"

MARK_FAILED = "✗"

ABSENT_CELL = "—"

NAME_WIDTH = 12

STATUS_WIDTH = 18

SUMMARY_WIDTH = 44

OUTPUTS_WIDTH = 9

COLUMN_GAP = 2

RECENT_ACTIONS = 8

STOP_WAIT_SECONDS = 10

STOP_POLL_SECONDS = 0.1

MARKER_CLOCK_SLACK_SECONDS = 2

PS_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"

XELATEX = "xelatex"

TEX_EXECUTABLES: tuple[str, ...] = (XELATEX, "latexmk", "latexpand")

MIN_TEXLIVE_YEAR = 2026

TEXLIVE_INSTALL_HINTS: dict[str, str] = {
    "Darwin": "install MacTeX: brew install --cask mactex-no-gui",
    "Linux": "install TeX Live scheme-full with install-tl: https://tug.org/texlive/quickinstall.html",
}

VERSION_TIMEOUT_SECONDS = 30

TEXLIVE_YEAR_PATTERN = re.compile(r"\(TeX Live (\d{4})\)")

CONFIG_CHECK_NAME = "config.toml"

StageName = Enum("StageName", {name: name for name in STAGES}, type=str)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Translate arXiv papers from LaTeX source into Chinese.",
)


@dataclass(frozen=True)
class RunOptions:
    paper: PaperInput
    workdir: Workdir
    meta: PaperMeta | None
    glossary: tuple[Path, ...]
    mode: str


@dataclass
class StageDisplay:
    progress: Progress
    task: TaskID
    name: str
    printed_status: str = ""

    def line(self, status: str, summary: str) -> None:
        console.print(f"    {status}: {summary}")

    def action(self, status: str, summary: str = "") -> None:
        if not console.is_terminal:
            if status != self.printed_status:
                self.line(status, summary)
            self.printed_status = status
            return
        width = STATUS_WIDTH + SUMMARY_WIDTH
        text = f"{status:<{STATUS_WIDTH}}{summary[:SUMMARY_WIDTH]}" if summary else status
        self.progress.update(self.task, description=f"{self.name:<{NAME_WIDTH}}{text[:width]:<{width}}")


def _fetch_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return fetch.run(options.paper, options.workdir, options.meta)


def _reconstruct_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return reconstruct.run(options.workdir, mode=options.mode, report=display.action)


def _precompile_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return precompile.run(options.workdir, mode=options.mode, report=display.action)


def _mask_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return mask.run(options.workdir)


def _translate_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return translate.run(
        options.workdir,
        mode=options.mode,
        glossary_files=options.glossary,
        report=display.action,
    )


def _compile_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return compile.run(options.workdir, mode=options.mode, report=display.action)


STAGE_ENTRIES: dict[str, Callable[[RunOptions, StageDisplay], Manifest]] = {
    "fetch": _fetch_entry,
    "reconstruct": _reconstruct_entry,
    "precompile": _precompile_entry,
    "mask": _mask_entry,
    "translate": _translate_entry,
    "compile": _compile_entry,
}


PaperArg = Annotated[
    str, typer.Argument(metavar="PAPER", help="arXiv id / arXiv URL / local source directory / local PDF file")
]
FromOpt = Annotated[
    StageName | None,
    typer.Option(
        "--from",
        help=(
            "redo everything from this stage on, removing its outputs and those downstream first; without it "
            f"the run starts at the first stage that has not finished ok. Stage order: {' → '.join(STAGES)}"
        ),
    ),
]
GlossaryOpt = Annotated[
    list[Path] | None,
    typer.Option("--glossary", metavar="FILE", help="CLI-layer glossary file; repeatable, later files win"),
]
WorkdirOpt = Annotated[
    Path | None,
    typer.Option(
        "--workdir",
        metavar="DIR",
        help="paper working directory (default $TONGTU_HOME/papers/<id>, $TONGTU_HOME defaults to ~/.tongtu)",
    ),
]
ModeOpt = Annotated[
    ModeName | None,
    typer.Option("--mode", help="mode of the agent sessions in this run; defaults to [modes] default in config.toml"),
]
LatestOpt = Annotated[bool, typer.Option("--latest", help="query arXiv for the latest version and use it")]
DevOpt = Annotated[
    bool, typer.Option("--dev", help="use ~/.tongtu-dev as TONGTU_HOME (config.toml, glossary.json and papers/)")
]


def _print_version(value: bool) -> None:
    if value:
        console.print(f"tongtu {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool, typer.Option("--version", help="print the version and exit", callback=_print_version, is_eager=True)
    ] = False,
) -> None:
    return None


def _apply_dev(dev: bool) -> None:
    if dev:
        os.environ[HOME_ENV] = str(DEV_HOME.expanduser())


def _workdir_name(paper_input: PaperInput) -> str:
    if paper_input.source_pdf is not None:
        return arxiv.pdf_workdir_name(paper_input.source_pdf)
    if paper_input.source_dir is not None:
        return paper_input.source_dir.name
    if paper_input.arxiv_id is not None and paper_input.version is not None:
        return arxiv.versioned_id(paper_input.arxiv_id, paper_input.version)
    return paper_input.arxiv_id or ""


def _describe_paper(arxiv_id: str | None, version: int | None, pdf: bool = False) -> str:
    if arxiv_id is None:
        return "a local PDF file" if pdf else "a local source directory"
    if version is None:
        return arxiv_id
    return arxiv.versioned_id(arxiv_id, version)


def _same_arxiv_id(asked: str, held: str) -> bool:
    try:
        return normalize_arxiv_id(asked) == normalize_arxiv_id(held)
    except WorkdirError:
        return False


def _check_fetched(paper_input: PaperInput, workdir: Path | None, fetched: FetchManifest | None) -> None:
    if fetched is None:
        return
    if fetched.arxiv_id is None:
        local = paper_input.source_pdf or paper_input.source_dir
        same_paper = local is not None and local.resolve() == Path(fetched.source).resolve()
    else:
        same_paper = paper_input.arxiv_id is not None and _same_arxiv_id(paper_input.arxiv_id, fetched.arxiv_id)
    same_version = paper_input.version is None or fetched.version is None or paper_input.version == fetched.version
    if same_paper and same_version:
        return
    raise typer.BadParameter(
        f"--workdir {workdir} holds {_describe_paper(fetched.arxiv_id, fetched.version, fetched.kind == 'local_pdf')}, "
        f"but the command asks for "
        f"{_describe_paper(paper_input.arxiv_id, paper_input.version, paper_input.source_pdf is not None)}; "
        "name the paper that this directory holds, or choose another --workdir"
    )


def _query_latest(arxiv_id: str, refresh: bool) -> PaperMeta:
    try:
        return arxiv.find_meta(arxiv_id, refresh=refresh).meta
    except arxiv.ArxivError as error:
        error_console.print(f"cannot determine the version of {arxiv_id}: {error}")
        raise typer.Exit(EXIT_FAILURE) from error


def _paper_workdir(
    paper: str, workdir: Path | None, *, latest: bool = False, may_query: bool = False
) -> tuple[PaperInput, Workdir, PaperMeta | None]:
    try:
        paper_input = parse_paper_argument(paper)
    except (PaperArgumentError, WorkdirError) as error:
        raise typer.BadParameter(str(error)) from error
    if latest and paper_input.version is not None:
        raise typer.BadParameter("--latest cannot be combined with an arXiv id that names a version")
    meta = None
    fetched = None
    if workdir is not None:
        fetched = load_manifest(Workdir(resolve(workdir=workdir)).manifest_path(fetch.STAGE_NAME), FetchManifest)
        if fetched is not None and fetched.arxiv_id is None and paper == Path(fetched.source).name:
            local = Path(fetched.source)
            paper_input = PaperInput(source_pdf=local) if fetched.kind == "local_pdf" else PaperInput(source_dir=local)
        _check_fetched(paper_input, workdir, fetched)
        if fetched is not None and fetched.arxiv_id is not None:
            paper_input = replace(paper_input, arxiv_id=fetched.arxiv_id)
    if paper_input.arxiv_id is not None and paper_input.version is None:
        if latest:
            version = None
        elif workdir is None:
            version = arxiv.latest_local_version(papers_dir(), paper_input.arxiv_id)
        else:
            version = None if fetched is None else fetched.version
        if version is None and may_query:
            meta = _query_latest(paper_input.arxiv_id, latest)
            version = meta.version
        if version is None and workdir is None:
            error_console.print(
                f"no working directory for {paper_input.arxiv_id} in {papers_dir()}; "
                f"run tongtu run {paper_input.arxiv_id} first"
            )
            raise typer.Exit(EXIT_FAILURE)
        paper_input = replace(paper_input, version=version)
        _check_fetched(paper_input, workdir, fetched)
    try:
        path = resolve(_workdir_name(paper_input), workdir)
    except WorkdirError as error:
        raise typer.BadParameter(str(error)) from error
    return paper_input, Workdir(path), meta


def _options(
    paper: str,
    workdir: Path | None,
    glossary: list[Path] | None,
    mode: str,
    *,
    latest: bool = False,
    may_query: bool = False,
) -> RunOptions:
    paper_input, paper_workdir, meta = _paper_workdir(paper, workdir, latest=latest, may_query=may_query)
    return RunOptions(
        paper=paper_input,
        workdir=paper_workdir,
        meta=meta,
        glossary=tuple(glossary or ()),
        mode=mode,
    )


def _mode(option: ModeName | None) -> tuple[str, str]:
    config, detail = load_config()
    if option is not None:
        name = option.value
    elif config is not None:
        name = config.modes.default.value
    else:
        name = ModeName.SMART.value
    if config is None:
        return name, f"{name}  ({detail})"
    target, _detail = mode_target(config, name)
    return name, "  ".join(part for part in (name, str(target), target.effort) if part)


def _elapsed_text(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def stage_summary(manifest: Manifest) -> str:
    if isinstance(manifest, FetchManifest):
        parts = [f"v{manifest.version}"] if manifest.version is not None else []
        if manifest.kind:
            parts.append(manifest.kind)
        parts.append(f"{len(manifest.tex_files)} tex files")
        return ", ".join(parts)
    if isinstance(manifest, ReconstructManifest):
        return reconstruct_summary(manifest)
    if isinstance(manifest, PrecompileManifest):
        parts = [f"{manifest.report.pages} pages"] if manifest.report else []
        if manifest.fix_session is not None:
            parts.append("1 fix session")
        return ", ".join(parts)
    if isinstance(manifest, MaskManifest):
        return f"{manifest.blocks_total} blocks masked"
    if isinstance(manifest, TranslateManifest):
        parts = [f"{manifest.paragraphs_translated} paragraphs"]
        if manifest.kept:
            parts.append(f"{len(manifest.kept)} kept in English")
        parts.append(f"{manifest.glossary.added} new terms")
        summary = ", ".join(parts)
        return summary if manifest.session is not None else f"{summary}, reused existing translation"
    if isinstance(manifest, CompileManifest):
        parts = [f"{manifest.report.pages} pages"] if manifest.report else []
        if manifest.baseline is not None:
            parts.append(f"baseline {manifest.baseline.pages}")
        if manifest.fix_session is not None:
            parts.append("1 fix session")
        return ", ".join(parts)
    return ""


def reconstruct_summary(manifest: ReconstructManifest) -> str:
    check = manifest.final_check
    if not manifest.rebuilt or check is None:
        return "source input"
    parts = [f"{check.pages} pages"]
    if not manifest.ok:
        parts += [f"recall {check.recall:.3f}", f"order {check.order:.3f}"]
    else:
        if manifest.formulas is not None:
            parts.append(f"math {manifest.formulas.passed}/{manifest.formulas.total}")
        if manifest.tables is not None:
            parts.append(f"tables {manifest.tables.passed}/{manifest.tables.total}")
    summary = ", ".join(parts)
    return f"{summary} fixed" if manifest.fix_session is not None else summary


def _print_stage_header() -> None:
    console.print(
        f"  {'stage':<{NAME_WIDTH}}{'status':<{STATUS_WIDTH}}{'summary':<{SUMMARY_WIDTH}}elapsed", style=HEADER_STYLE
    )


def _print_stage_result(name: str, manifest: Manifest, workdir: Workdir, seconds: float) -> None:
    ok = manifest.ok
    mark = MARK_OK if ok else MARK_FAILED
    summary = stage_summary(manifest) if ok else ""
    line = f"{mark} {name:<{NAME_WIDTH}}{manifest.status:<{STATUS_WIDTH}}{summary:<{SUMMARY_WIDTH}}"
    console.print(f"{line}{_elapsed_text(seconds)}")
    if manifest.message:
        console.print(f"    {manifest.message}")
    for warning in manifest.warnings:
        console.print(f"    warning: {warning}")
    if not ok:
        console.print(f"    manifest  {workdir.manifest_path(name)}")


def _text_column(template: str) -> TextColumn:
    return TextColumn(template, table_column=Column(no_wrap=True))


def _run_stage(name: str, options: RunOptions) -> Manifest:
    started = time.monotonic()
    with Progress(
        SpinnerColumn(),
        _text_column("{task.description}"),
        TimeElapsedColumn(table_column=Column(no_wrap=True)),
        console=console,
        transient=True,
        disable=not console.is_terminal,
    ) as progress:
        task = progress.add_task(name, total=None)
        display = StageDisplay(progress=progress, task=task, name=name)
        manifest = STAGE_ENTRIES[name](options, display)
    _print_stage_result(name, manifest, options.workdir, time.monotonic() - started)
    return manifest


def _run_stages(start: str, options: RunOptions) -> typer.Exit:
    for name in downstream(start):
        if not _run_stage(name, options).ok:
            return typer.Exit(EXIT_FAILURE)
    return typer.Exit(0)


@app.command()
def run(
    paper: PaperArg,
    from_stage: FromOpt = None,
    glossary: GlossaryOpt = None,
    workdir: WorkdirOpt = None,
    mode: ModeOpt = None,
    latest: LatestOpt = False,
    dev: DevOpt = False,
) -> None:
    _apply_dev(dev)
    mode_name, mode_line = _mode(mode)
    options = _options(paper, workdir, glossary, mode_name, latest=latest, may_query=True)
    console.print(f"workdir {options.workdir.path}")
    console.print(f"mode    {mode_line}")
    with _running(options.workdir, RunCommand.RUN, mode_name):
        if from_stage is not None:
            clean_from(options.workdir, from_stage.value)
            start = from_stage.value
            console.print(f"--from {start}: removed {start} and downstream outputs")
        else:
            pending = first_pending(options.workdir)
            if pending is None:
                console.print("all six stages have finished ok; nothing to run. Use --from STAGE to redo.")
                return
            start = pending
            if start != STAGES[0]:
                console.print(f"resuming from {start} (upstream stages finished ok)")
        console.print("")
        _print_stage_header()
        raise _run_stages(start, options)


@app.command()
def stage(
    name: Annotated[
        StageName | None, typer.Argument(metavar="STAGE", help=f"stage name, in order: {', '.join(STAGES)}")
    ] = None,
    paper: Annotated[
        str | None,
        typer.Argument(metavar="PAPER", help="arXiv id / arXiv URL / local source directory / local PDF file"),
    ] = None,
    glossary: GlossaryOpt = None,
    workdir: WorkdirOpt = None,
    mode: ModeOpt = None,
    dev: DevOpt = False,
) -> None:
    _apply_dev(dev)
    if name is None:
        console.print(" → ".join(STAGES))
        return
    if paper is None:
        raise typer.BadParameter(
            "missing argument PAPER (arXiv id / arXiv URL / local source directory / local PDF file)"
        )
    options = _options(paper, workdir, glossary, _mode(mode)[0], may_query=name.value == "fetch")
    missing = [
        upstream for upstream in STAGES[: STAGES.index(name.value)] if not outputs_present(options.workdir, upstream)
    ]
    if missing:
        error_console.print(
            f"upstream outputs absent: {', '.join(missing)}. "
            f"Run tongtu run first, or tongtu run --from {missing[0]} to redo."
        )
        raise typer.Exit(EXIT_USAGE)
    with _running(options.workdir, RunCommand.STAGE, options.mode):
        clean(options.workdir, name.value)
        _print_stage_header()
        manifest = _run_stage(name.value, options)
    raise typer.Exit(0 if manifest.ok else EXIT_FAILURE)


@contextmanager
def _running(workdir: Workdir, command: RunCommand, mode: str) -> Iterator[None]:
    started_at = datetime.now(UTC)
    write_manifest(workdir.run_marker, RunMarker(pid=os.getpid(), started_at=started_at, command=command, mode=mode))
    write_manifest(workdir.last_run, RunRecord(started_at=started_at, command=command, mode=mode))

    def terminate(signum: int, frame: FrameType | None) -> None:
        processes.terminate_active()
        _remove_run_marker(workdir)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
        _remove_run_marker(workdir)


def _remove_run_marker(workdir: Workdir) -> None:
    marker = load_manifest(workdir.run_marker, RunMarker)
    if marker is not None and marker.pid == os.getpid():
        workdir.run_marker.unlink(missing_ok=True)


def _live_marker(workdir: Workdir) -> RunMarker | None:
    marker = load_manifest(workdir.run_marker, RunMarker)
    if marker is None:
        return None
    try:
        os.kill(marker.pid, 0)
    except PermissionError:
        pass
    except (ProcessLookupError, OverflowError):
        return None
    process_started = _process_started_at(marker.pid)
    if process_started is None:
        return None
    if process_started > marker.started_at + timedelta(seconds=MARKER_CLOCK_SLACK_SECONDS):
        return None
    return marker


def _process_started_at(pid: int) -> datetime | None:
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except OSError:
        return None
    try:
        return datetime.strptime(" ".join(completed.stdout.split()), PS_LSTART_FORMAT).astimezone()
    except ValueError:
        return None


JsonOpt = Annotated[
    bool, typer.Option("--json", help="print one JSON object (UTF-8) with the same content instead of the table")
]


@app.command()
def status(paper: PaperArg, workdir: WorkdirOpt = None, dev: DevOpt = False, as_json: JsonOpt = False) -> None:
    _apply_dev(dev)
    paper_input, paper_workdir, _ = _paper_workdir(paper, workdir)
    snapshot = _status_snapshot(paper_workdir, paper_input.arxiv_id, paper_input.version)
    if as_json:
        typer.echo(snapshot.model_dump_json(indent=2).encode(ENCODING))
        return
    console.print(f"workdir {snapshot.workdir}")
    if snapshot.mode is not None:
        console.print(f"mode    {snapshot.mode}")
    if snapshot.running and snapshot.started_at is not None:
        console.print(f"running pid {snapshot.pid}, started {snapshot.started_at.isoformat(timespec='seconds')}")
    console.print("")
    console.print(
        f"{'stage':<{NAME_WIDTH}}{'status':<{STATUS_WIDTH}}{'outputs':<{OUTPUTS_WIDTH}}manifest", style=HEADER_STYLE
    )
    for state in snapshot.stages:
        status_cell = ABSENT_CELL if state.status == ABSENT_STATUS else state.status
        outputs_cell = "present" if state.outputs_present else "absent"
        manifest_cell = str(state.manifest.relative_to(snapshot.workdir)) if state.manifest else ABSENT_CELL
        console.print(
            f"{state.name:<{NAME_WIDTH}}{status_cell:<{STATUS_WIDTH}}{outputs_cell:<{OUTPUTS_WIDTH}}{manifest_cell}"
        )
        for note in [state.message, *state.warnings]:
            if note:
                console.print(f"{'':<{NAME_WIDTH}}  {note}")


def _status_snapshot(paper_workdir: Workdir, arxiv_id: str | None, version: int | None) -> PaperStatus:
    stages = []
    for name in STAGES:
        manifest_path = paper_workdir.manifest_path(name)
        manifest = load_manifest(manifest_path, Manifest)
        if manifest is not None:
            status = manifest.status
        else:
            status = "ok" if implied_complete(paper_workdir, name) else ABSENT_STATUS
        stages.append(
            StageState(
                name=name,
                status=status,
                outputs_present=outputs_present(paper_workdir, name),
                message=manifest.message if manifest is not None else "",
                warnings=manifest.warnings if manifest is not None else [],
                manifest=manifest_path if manifest_path.is_file() else None,
            )
        )
    marker = _live_marker(paper_workdir)
    translate_manifest = load_manifest(paper_workdir.manifest_path(translate.STAGE_NAME), TranslateManifest)
    last_run = load_manifest(paper_workdir.last_run, RunRecord)
    if marker is not None:
        mode = marker.mode
    elif translate_manifest is not None:
        mode = translate_manifest.mode
    else:
        mode = last_run.mode if last_run is not None else None
    state, stage_name = _paper_state(stages, marker is not None)
    compiled = load_manifest(paper_workdir.manifest_path(compile.STAGE_NAME), CompileManifest)
    precompiled = load_manifest(paper_workdir.manifest_path(precompile.STAGE_NAME), PrecompileManifest)
    source_report = precompiled.report if precompiled is not None else None
    if source_report is None and compiled is not None:
        source_report = compiled.baseline
    session = translate_manifest.session if translate_manifest is not None else None
    log = SESSION_LOGS.get(stage_name or "")
    return PaperStatus(
        workdir=paper_workdir.path,
        arxiv_id=arxiv_id,
        version=version,
        running=marker is not None,
        pid=marker.pid if marker is not None else None,
        started_at=marker.started_at if marker is not None else None,
        mode=mode,
        state=state,
        stage=stage_name,
        stages=stages,
        pdf=_existing(paper_workdir.zh_pdf),
        meta=_existing(paper_workdir.meta),
        glossary=_existing(paper_workdir.paper_glossary),
        notes=_existing(paper_workdir.notes),
        pages=compiled.report.pages if compiled is not None and compiled.report is not None else None,
        source_pages=source_report.pages if source_report is not None else None,
        translate_seconds=session.duration_seconds if session is not None else None,
        translate_cost_usd=session.usage.cost_usd if session is not None and session.usage is not None else None,
        recent_actions=_recent_actions(log(paper_workdir)) if state == PaperState.RUNNING and log else [],
    )


def _paper_state(stages: list[StageState], running: bool) -> tuple[PaperState, str | None]:
    pending = next((stage for stage in stages if stage.status != "ok"), None)
    if running:
        return PaperState.RUNNING, pending.name if pending is not None else None
    if pending is None:
        return PaperState.OK, None
    if pending.status == ABSENT_STATUS:
        return PaperState.INTERRUPTED, pending.name
    return PaperState.FAILED, pending.name


def _existing(path: Path) -> Path | None:
    return path if path.is_file() else None


def _latest_reconstruct_log(workdir: Workdir) -> Path:
    logs = [workdir.reconstruct_formula_log, workdir.reconstruct_table_log, workdir.reconstruct_fix_log]
    existing = [path for path in logs if path.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime) if existing else workdir.reconstruct_fix_log


SESSION_LOGS: dict[str, Callable[[Workdir], Path]] = {
    "reconstruct": _latest_reconstruct_log,
    "precompile": lambda w: w.precompile_fix_log,
    "translate": lambda w: w.translate_log,
    "compile": lambda w: w.compile_fix_log,
}


@app.command("list")
def list_papers(dev: DevOpt = False, as_json: JsonOpt = False) -> None:
    _apply_dev(dev)
    papers = papers_dir()
    summaries = []
    for path in papers.iterdir() if papers.is_dir() else ():
        paper_workdir = Workdir(path)
        fetched = load_manifest(paper_workdir.manifest_path(fetch.STAGE_NAME), FetchManifest)
        if path.is_dir() and _listed(paper_workdir, fetched):
            summaries.append(_paper_summary(paper_workdir, fetched))
    listing = PaperListing(papers=sorted(summaries, key=lambda summary: summary.updated_at, reverse=True))
    if as_json:
        typer.echo(listing.model_dump_json().encode(ENCODING))
        return
    rows = [("paper", "state", "stage", "title")] + [
        (summary.name, summary.state.value, summary.stage or ABSENT_CELL, summary.title or "")
        for summary in listing.papers
    ]
    widths = [max(len(row[column]) for row in rows) + COLUMN_GAP for column in range(3)]
    for index, (name, state, stage_cell, title) in enumerate(rows):
        line = f"{name:<{widths[0]}}{state:<{widths[1]}}{stage_cell:<{widths[2]}}{title}".rstrip()
        console.print(line, style=HEADER_STYLE if index == 0 else None, highlight=False, markup=False)


def _listed(paper_workdir: Workdir, fetched: FetchManifest | None) -> bool:
    if fetched is None:
        return _live_marker(paper_workdir) is not None
    if fetched.arxiv_id is None:
        return True
    if fetched.version is None:
        return False
    try:
        expected = arxiv.versioned_id(normalize_arxiv_id(fetched.arxiv_id), fetched.version)
    except WorkdirError:
        return False
    return paper_workdir.path.name == expected


def _paper_summary(paper_workdir: Workdir, fetched: FetchManifest | None) -> PaperSummary:
    snapshot = _status_snapshot(
        paper_workdir,
        fetched.arxiv_id if fetched is not None else None,
        fetched.version if fetched is not None else None,
    )
    meta = load_manifest(paper_workdir.meta, PaperMeta)
    directory_time = _modified_at(paper_workdir.path)
    manifest_times = [_modified_at(stage.manifest) for stage in snapshot.stages if stage.manifest is not None]
    fetch_manifest = paper_workdir.manifest_path(fetch.STAGE_NAME)
    return PaperSummary(
        name=paper_workdir.path.name,
        workdir=snapshot.workdir,
        state=snapshot.state,
        stage=snapshot.stage,
        arxiv_id=snapshot.arxiv_id,
        version=snapshot.version,
        title=meta.title if meta is not None else None,
        primary_category=meta.primary_category if meta is not None else None,
        added_at=_modified_at(fetch_manifest) if fetch_manifest.is_file() else directory_time,
        updated_at=max(manifest_times, default=directory_time),
        started_at=snapshot.started_at,
        mode=snapshot.mode,
        pdf=snapshot.pdf,
    )


def _modified_at(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _recent_actions(log_path: Path) -> list[str]:
    try:
        lines = log_path.read_bytes().splitlines()
    except OSError:
        return []
    summaries = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "tool" and isinstance(event.get("summary"), str):
            summaries.append(event["summary"])
    return summaries[-RECENT_ACTIONS:]


@app.command()
def stop(paper: PaperArg, workdir: WorkdirOpt = None, dev: DevOpt = False) -> None:
    _apply_dev(dev)
    _paper_input, paper_workdir, _ = _paper_workdir(paper, workdir)
    name = paper_workdir.path.name
    marker = _live_marker(paper_workdir)
    if marker is None:
        console.print(f"{name} is not running")
        return
    try:
        os.kill(marker.pid, signal.SIGTERM)
    except ProcessLookupError:
        console.print(f"stopped {name}")
        return
    except PermissionError:
        error_console.print(f"{name} (pid {marker.pid}) belongs to another user; cannot send SIGTERM")
        raise typer.Exit(EXIT_FAILURE) from None
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while _live_marker(paper_workdir) is not None:
        if time.monotonic() >= deadline:
            error_console.print(f"{name} (pid {marker.pid}) did not stop within {STOP_WAIT_SECONDS} seconds")
            raise typer.Exit(EXIT_FAILURE)
        time.sleep(STOP_POLL_SECONDS)
    console.print(f"stopped {name}")


@app.command()
def info(
    paper: Annotated[str, typer.Argument(metavar="PAPER", help="arXiv id or arXiv URL, with or without a version")],
    as_json: Annotated[
        bool, typer.Option("--json", help="print the paper info as one JSON object (UTF-8), the format of meta.json")
    ] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="ask arXiv again instead of reading the cache")] = False,
    zh: Annotated[
        bool, typer.Option("--zh", help="translate the abstract into Chinese when it has no translation yet")
    ] = False,
    dev: DevOpt = False,
) -> None:
    _apply_dev(dev)
    try:
        paper_input = parse_paper_argument(paper)
    except (PaperArgumentError, WorkdirError) as error:
        raise typer.BadParameter(str(error)) from error
    if paper_input.arxiv_id is None:
        raise typer.BadParameter(f"{paper} is a local source directory or PDF file, which has no arXiv info")
    try:
        found = arxiv.find_meta(paper_input.arxiv_id, paper_input.version, refresh=refresh)
    except arxiv.ArxivError as error:
        name = _describe_paper(paper_input.arxiv_id, paper_input.version)
        reason = " ".join(str(error).split())
        error_console.print(f"cannot get info for {name}: {reason}", markup=False, highlight=False, soft_wrap=True)
        raise typer.Exit(EXIT_FAILURE) from error
    if zh and not found.meta.abstract_zh:
        found = _with_translated_abstract(found)
    if as_json:
        typer.echo(found.meta.model_dump_json(indent=2).encode(ENCODING))
        return
    for line in _info_lines(found, datetime.now(UTC), console.width):
        console.print(line, markup=False, highlight=False, soft_wrap=True)


def _with_translated_abstract(found: arxiv.FoundMeta) -> arxiv.FoundMeta:
    config, reason = load_config()
    if config is not None:
        try:
            abstract_zh = chat.translate_abstract(config.abstract, found.meta.abstract)
        except chat.ChatError as error:
            reason = str(error)
        except Exception as error:  # why(#191): 摘要翻译出任何异常时论文信息仍要输出，原因只写类型名，避免带出密钥
            reason = f"unexpected {type(error).__name__}"
        else:
            return arxiv.save_abstract_zh(found, abstract_zh)
    name = arxiv.versioned_id(found.meta.arxiv_id, found.meta.version)
    error_console.print(
        f"cannot translate the abstract of {name}: {' '.join(reason.split())}",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    return found


def _info_lines(found: arxiv.FoundMeta, now: datetime, width: int) -> list[str]:
    meta = found.meta
    others = [category for category in meta.categories if category != meta.primary_category]
    category = f"{meta.primary_category} ({', '.join(others)})" if others else meta.primary_category
    rows = [
        ("paper", arxiv.versioned_id(meta.arxiv_id, meta.version)),
        ("title", meta.title),
        ("authors", ", ".join(meta.authors)),
        ("category", category),
        ("published", meta.published.date().isoformat()),
        ("updated", meta.updated.date().isoformat()),
        ("comment", meta.comment),
        ("journal", meta.journal_ref),
        ("doi", meta.doi),
        ("url", meta.abs_url),
        ("source", _source_text(found, now)),
    ]
    present = [(label, value) for label, value in rows if value]
    label_width = max(len(label) for label, _value in present) + COLUMN_GAP
    lines = [f"{label:<{label_width}}{value}" for label, value in present]
    abstract = textwrap.fill(meta.abstract, width=width, initial_indent="  ", subsequent_indent="  ")
    if not meta.abstract_zh:
        return [*lines, "", "abstract", abstract]
    abstract_zh = [f"  {line.plain.rstrip()}" for line in Text(meta.abstract_zh).wrap(console, width - 2)]
    return [*lines, "", "abstract", abstract, "", "abstract (zh)", *abstract_zh]


def _source_text(found: arxiv.FoundMeta, now: datetime) -> str:
    if found.source == arxiv.MetaSource.ARXIV:
        return "arXiv"
    if found.source == arxiv.MetaSource.WORKDIR:
        return "working directory"
    minutes = int((now - found.fetched_at).total_seconds() // 60)
    if minutes < 60:
        age = f"{minutes} minutes"
    elif minutes < 60 * 24:
        age = f"{minutes // 60} hours"
    else:
        age = f"{minutes // (60 * 24)} days"
    return f"cache, fetched {age} ago"


@app.command()
def validate(
    src: Annotated[Path, typer.Argument(help="masked source file")],
    dst: Annotated[Path, typer.Argument(help="translated file")],
    glossary: Annotated[
        list[Path] | None,
        typer.Option(
            "--glossary", metavar="FILE", help="glossary file to check terms against; repeatable, later files win"
        ),
    ] = None,
) -> None:
    glossary_arguments = [argument for path in glossary or () for argument in (validation.GLOSSARY_OPTION, str(path))]
    raise typer.Exit(validation.main([str(src), str(dst), *glossary_arguments]))


@app.command()
def doctor(dev: DevOpt = False) -> None:
    _apply_dev(dev)
    config, config_detail = load_config()
    environment = [_tex_group(), _fonts_group(config)]
    executable, agentrun_detail = agentrun_executable()
    sessions = [
        _config_group(config, config_detail),
        _agentrun_group(executable, agentrun_detail),
        _modes_group(config, executable),
    ]
    groups = environment + sessions
    width = max(len(item.name) for group in groups for item in group.items)
    for group in groups:
        _print_doctor_group(group, width)
    home = home_dir()
    for path in legacy_dirs_present():
        if path.resolve() == home.resolve():
            console.print(
                f"note: TONGTU_HOME points at the legacy directory {path}; papers now go to "
                f"{path}/papers/<id>/ and the config is read from {path}/config.toml"
            )
        else:
            console.print(
                f"note: legacy directory {path} exists and is no longer read; move its contents under "
                f"{home} (config.toml, glossary.json, papers/<id>/) and delete it"
            )
    absent_environment = _failed(environment)
    absent_sessions = _failed(sessions)
    if absent_environment:
        console.print(f"environment incomplete: {', '.join(absent_environment)}")
        raise typer.Exit(EXIT_FAILURE)
    if absent_sessions:
        console.print(
            f"toolchain and fonts complete; {', '.join(absent_sessions)} not usable, so translate and the fix "
            "sessions cannot run; fetch, precompile and mask still can."
        )
        raise typer.Exit(EXIT_NOT_CONFIGURED)
    console.print("environment complete.")


class DoctorMark(StrEnum):
    OK = "✓"
    FAILED = "✗"
    SKIPPED = "-"


DOCTOR_MARK_STYLES: dict[DoctorMark, str] = {
    DoctorMark.OK: "green",
    DoctorMark.FAILED: "bold red",
    DoctorMark.SKIPPED: "dim",
}


@dataclass(frozen=True)
class DoctorItem:
    name: str
    mark: DoctorMark
    detail: str
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class DoctorGroup:
    title: str
    items: list[DoctorItem]
    qualified: bool = False


def _print_doctor_group(group: DoctorGroup, width: int) -> None:
    console.print(Text(group.title, style="bold"))
    for item in group.items:
        line = Text("  ")
        line.append(item.mark.value, style=DOCTOR_MARK_STYLES[item.mark])
        line.append(f" {item.name:<{width}}")
        if item.detail:
            line.append(f"  {item.detail}")
        console.print(line)
        for problem in item.problems:
            console.print(Text(f"      {problem}", style="red"))


def _failed(groups: list[DoctorGroup]) -> list[str]:
    return [
        f"{group.title} {item.name}" if group.qualified else item.name
        for group in groups
        for item in group.items
        if item.mark is DoctorMark.FAILED
    ]


def _home_relative(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def _tex_group() -> DoctorGroup:
    items = []
    hint = TEXLIVE_INSTALL_HINTS.get(platform.system())
    for name in TEX_EXECUTABLES:
        path = shutil.which(name)
        if path is None:
            problem = f"not found in PATH; {hint}" if hint else "not found in PATH"
            items.append(DoctorItem(name, DoctorMark.FAILED, "", (problem,)))
            continue
        if name != XELATEX:
            items.append(DoctorItem(name, DoctorMark.OK, path))
            continue
        year, problem = _texlive_year()
        if year is None:
            items.append(DoctorItem(name, DoctorMark.FAILED, path, (problem,)))
        else:
            items.append(DoctorItem(name, DoctorMark.OK, f"{path} (TeX Live {year})"))
    return DoctorGroup("TeX", items)


def _texlive_year() -> tuple[int | None, str]:
    try:
        completed = subprocess.run(
            [XELATEX, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        return None, f"failed to run {XELATEX} --version: {describe_error(error)}"
    text = completed.stdout.strip()
    first_line = text.splitlines()[0] if text else ""
    match = TEXLIVE_YEAR_PATTERN.search(first_line)
    if match is None:
        return None, f"no TeX Live year in the {XELATEX} --version output; output: {text[:OUTPUT_EXCERPT_CHARS]}"
    year = int(match.group(1))
    if year < MIN_TEXLIVE_YEAR:
        return None, (
            f"TeX Live {year} is below the required {MIN_TEXLIVE_YEAR}; "
            "install with install-tl, not the distro apt package"
        )
    return year, ""


def _fonts_group(config: ModelsConfig | None) -> DoctorGroup:
    fonts = config.fonts if config is not None else FontsConfig()
    roles: tuple[tuple[str, str | FontFamily | None], ...] = (
        ("main", fonts.main),
        ("sans", fonts.sans),
        ("mono", fonts.mono),
    )
    return DoctorGroup("fonts", [_font_item(role, value) for role, value in roles], qualified=True)


def _font_item(role: str, value: str | FontFamily | None) -> DoctorItem:
    if value is None:
        return DoctorItem(role, DoctorMark.SKIPPED, "not set; TeX default")
    files = [name for name in family_files(value) if name]
    shown = []
    absent = []
    for name in files:
        path = Path(name).expanduser()
        bundled = len(path.parts) == 1
        located = FONTS_DIR / name if bundled else path
        shown.append(name if bundled else _home_relative(str(path)))
        if not located.is_file():
            absent.append(
                f"{name} not found under {FONTS_DIR}" if bundled else f"{_home_relative(str(path))} not found"
            )
    detail = " / ".join(shown)
    if all(len(Path(name).expanduser().parts) == 1 for name in files):
        detail += " (bundled)"
    if absent:
        return DoctorItem(role, DoctorMark.FAILED, detail, tuple(absent))
    return DoctorItem(role, DoctorMark.OK, detail)


def _config_group(config: ModelsConfig | None, detail: str) -> DoctorGroup:
    path = config_path()
    if config is None:
        return DoctorGroup("config", [DoctorItem(CONFIG_CHECK_NAME, DoctorMark.FAILED, "", (detail,))])
    shown = _home_relative(str(path))
    if not path.exists():
        shown += " absent; using the defaults"
    return DoctorGroup("config", [DoctorItem(CONFIG_CHECK_NAME, DoctorMark.OK, shown)])


def _agentrun_group(executable: str | None, detail: str) -> DoctorGroup:
    if executable is None:
        return DoctorGroup("agentrun", [DoctorItem(AGENTRUN_EXECUTABLE, DoctorMark.FAILED, "", (detail,))])
    shown = detail if detail != executable else _home_relative(executable)
    return DoctorGroup("agentrun", [DoctorItem(AGENTRUN_EXECUTABLE, DoctorMark.OK, shown)])


def _modes_group(config: ModelsConfig | None, agentrun: str | None) -> DoctorGroup:
    items = []
    problems_by_target: dict[str, tuple[str, ...]] = {}
    for name in ModeName:
        if config is None:
            items.append(DoctorItem(name, DoctorMark.SKIPPED, "not checked: config.toml is unreadable"))
            continue
        target, detail = mode_target(config, name)
        if target is None:
            items.append(DoctorItem(name, DoctorMark.FAILED, "", (detail,)))
            continue
        if agentrun is None:
            items.append(DoctorItem(name, DoctorMark.SKIPPED, f"{target}  not checked: agentrun is missing"))
            continue
        key = str(target)
        if key not in problems_by_target:
            problems_by_target[key] = _runtime_problems(target, executable=agentrun)
        problems = problems_by_target[key]
        mark = DoctorMark.FAILED if problems else DoctorMark.OK
        items.append(DoctorItem(name, mark, key, problems))
    return DoctorGroup("modes", items, qualified=True)


def _runtime_problems(target: Target, executable: str) -> tuple[str, ...]:
    command = [executable, "doctor", target.runtime, "--json"] + (["--model", target.model] if target.model else [])
    try:
        completed = subprocess.run(command, capture_output=True, text=True)
    except OSError as error:
        return (f"agentrun doctor: {describe_error(error)}",)
    try:
        checks = json.loads(completed.stdout)
        return tuple(f"{check['check']}: {check['detail']}" for check in checks if check["status"] == "fail")
    except (ValueError, TypeError, KeyError):
        stderr = completed.stderr.strip()[:OUTPUT_EXCERPT_CHARS]
        return (f"agentrun doctor: {stderr or 'printed no check list'}",)


config_app = typer.Typer(
    invoke_without_command=True,
    help="Show and change config.toml; without a subcommand, open a menu driven by the arrow keys and Enter.",
)
app.add_typer(config_app, name="config")

MENU_UP = "\x1b[A"

MENU_DOWN = "\x1b[B"

MENU_ENTER = ("\r", "\n")

MENU_EXIT = "exit"

CURRENT_MARK = " (current)"


def _display(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _print_validation_error(error: ValidationError) -> None:
    for line in validation_lines(error):
        error_console.print(line, markup=False, highlight=False)


def _entry_lines(entries: list[tuple[str, object]]) -> list[str]:
    width = max(len(key) for key, _value in entries) + 2
    return [f"{key:<{width}}{_display(value)}" for key, value in entries]


def _parse_value(key: str, text: str) -> object:
    if key in SECRET_KEYS:
        return text
    if text == "":
        return None
    try:
        return tomllib.loads(f"value = {text}")["value"]
    except tomllib.TOMLDecodeError:
        return text


def _loaded_config(loaded: tuple[ModelsConfig | None, str]) -> ModelsConfig:
    config, detail = loaded
    if config is None:
        error_console.print(detail)
        raise typer.Exit(EXIT_FAILURE)
    return config


def _saved(config: ModelsConfig, *keys: str) -> None:
    write_config(config)
    values = dict(shown_entries(config))
    for key in dict.fromkeys(keys):
        console.print(f"{key} = {_display(values[key])}")


def _menu(lines: list[str], cursor: int) -> Text:
    text = Text()
    for index, line in enumerate(lines):
        if index == cursor:
            text.append(f"> {line}\n", style="bold")
        else:
            text.append(f"  {line}\n")
    return text


def _choose(lines: list[str], cursor: int) -> int:
    with Live(_menu(lines, cursor), console=console, auto_refresh=False, transient=True) as live:
        while True:
            key = typer.getchar()
            if key in MENU_ENTER:
                return cursor
            if key == MENU_UP:
                cursor = (cursor - 1) % len(lines)
            elif key == MENU_DOWN:
                cursor = (cursor + 1) % len(lines)
            else:
                continue
            live.update(_menu(lines, cursor), refresh=True)


def _edit_choice(config: ModelsConfig, key: str, value: object, options: list[object]) -> ModelsConfig:
    lines = [_display(option) + (CURRENT_MARK if option == value else "") for option in options]
    chosen = options[_choose(lines, options.index(value))]
    if chosen == value:
        return config
    try:
        changed = updated_config(config, key, chosen)
    except ValidationError as error:
        _print_validation_error(error)
        return config
    _saved(changed, key)
    return changed


def _edit_text(config: ModelsConfig, key: str, value: object) -> ModelsConfig:
    current = _display(value)
    while True:
        text = typer.prompt(key, default=current, show_default=True)
        if text == current:
            return config
        try:
            changed = updated_config(config, key, _parse_value(key, text))
        except ValidationError as error:
            _print_validation_error(error)
            continue
        _saved(changed, key)
        return changed


def _edit_secret(config: ModelsConfig, key: str) -> ModelsConfig:
    text = typer.prompt(key, default="", show_default=False, hide_input=True)
    if not text.strip():
        return config
    changed = updated_config(config, key, _parse_value(key, text))
    _saved(changed, key)
    return changed


def _edit_entry(config: ModelsConfig, key: str, value: object) -> ModelsConfig:
    if key in SECRET_KEYS:
        return _edit_secret(config, key)
    if isinstance(value, bool):
        return _edit_choice(config, key, value, [True, False])
    if isinstance(value, StrEnum):
        return _edit_choice(config, key, value, list(type(value)))
    return _edit_text(config, key, value)


@config_app.callback()
def config_menu(ctx: typer.Context, dev: DevOpt = False) -> None:
    _apply_dev(dev)
    if ctx.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty():
        error_console.print("tongtu config needs a terminal; use tongtu config list or tongtu config set KEY VALUE")
        raise typer.Exit(EXIT_USAGE)
    config = _loaded_config(ensure_config_file())
    cursor = 0
    while True:
        entries = shown_entries(config)
        cursor = _choose([*_entry_lines(entries), MENU_EXIT], cursor)
        if cursor == len(entries):
            return
        key, value = entries[cursor]
        config = _edit_entry(config, key, value)


@config_app.command("list")
def config_list(dev: DevOpt = False, as_json: JsonOpt = False) -> None:
    _apply_dev(dev)
    entries = shown_entries(_loaded_config(load_config()))
    if as_json:
        values = {key: value.value if isinstance(value, Enum) else value for key, value in entries}
        typer.echo(json.dumps(values, ensure_ascii=False, indent=2).encode(ENCODING))
        return
    for line in _entry_lines(entries):
        console.print(line)


@config_app.command("set")
def config_set(
    pairs: Annotated[
        list[str],
        typer.Argument(
            metavar="KEY VALUE [KEY VALUE ...]",
            help=(
                "dotted keys as tongtu config list prints them, each followed by a TOML value, or plain text taken "
                "as a string; an empty string clears the field; abstract.api_key always takes the text as it is, "
                "without surrounding whitespace"
            ),
        ),
    ],
    dev: DevOpt = False,
) -> None:
    _apply_dev(dev)
    if len(pairs) % 2:
        raise typer.BadParameter("expected KEY VALUE pairs; the last key has no value")
    config = _loaded_config(load_config())
    keys = pairs[::2]
    for key, value in zip(keys, pairs[1::2], strict=True):
        try:
            config = updated_config(config, key, _parse_value(key, value))
        except KeyError:
            error_console.print(f"config key {key} does not exist; run tongtu config list to see the keys")
            raise typer.Exit(EXIT_USAGE) from None
        except ValidationError as error:
            _print_validation_error(error)
            raise typer.Exit(EXIT_USAGE) from None
    _saved(config, *keys)


@config_app.command("open")
def config_open(dev: DevOpt = False) -> None:
    _apply_dev(dev)
    path = config_path()
    if not path.exists():
        write_config(ModelsConfig())
    console.print(str(path))
    editor = os.environ.get("EDITOR", "").strip()
    if editor:
        subprocess.run([*shlex.split(editor), str(path)])
    else:
        typer.launch(str(path))


def main() -> None:
    if os.environ.get("TONGTU_DISABLE"):
        error_console.print("tongtu cannot run inside an agent session (TONGTU_DISABLE is set)")
        raise SystemExit(EXIT_USAGE)
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
