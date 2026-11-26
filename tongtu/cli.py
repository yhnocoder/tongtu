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

from . import __version__, arxiv, validation
from .artifacts.common import Manifest
from .artifacts.compile import CompileManifest
from .artifacts.fetch import FetchManifest, PaperMeta
from .artifacts.mask import MaskManifest
from .artifacts.precompile import PrecompileManifest
from .artifacts.status import ABSENT_STATUS, PaperStatus, RunCommand, RunMarker, StageState
from .artifacts.translate import TranslateManifest
from .arxiv import PaperArgumentError, PaperInput, parse_paper_argument
from .config import DEV_HOME, HOME_ENV, config_path, home_dir, legacy_dirs_present, papers_dir
from .console import console, error_console
from .fonts import FONTS_DIR, family_files
from .manifests import describe_error, load_manifest, write_manifest
from .model.config import (
    FontFamily,
    FontsConfig,
    ModelsConfig,
    ModeName,
    Target,
    config_entries,
    ensure_config_file,
    load_config,
    mode_target,
    updated_config,
    write_config,
)
from .model.work import AGENTRUN_EXECUTABLE, agentrun_executable
from .pipeline import STAGES, clean, clean_from, downstream, first_pending, outputs_present
from .processes import OUTPUT_EXCERPT_CHARS
from .stages import compile, fetch, mask, precompile, translate
from .workdir import ENCODING, Workdir, WorkdirError, resolve

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
    "precompile": _precompile_entry,
    "mask": _mask_entry,
    "translate": _translate_entry,
    "compile": _compile_entry,
}


PaperArg = Annotated[str, typer.Argument(metavar="PAPER", help="arXiv id / arXiv URL / local source directory")]
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
    if paper_input.source_dir is not None:
        return paper_input.source_dir.name
    if paper_input.arxiv_id is not None and paper_input.version is not None:
        return arxiv.versioned_id(paper_input.arxiv_id, paper_input.version)
    return paper_input.arxiv_id or ""


def _describe_paper(arxiv_id: str | None, version: int | None) -> str:
    if arxiv_id is None:
        return "a local source directory"
    if version is None:
        return arxiv_id
    return arxiv.versioned_id(arxiv_id, version)


def _check_fetched(paper_input: PaperInput, workdir: Path | None, fetched: FetchManifest | None) -> None:
    if fetched is None:
        return
    if fetched.arxiv_id is None:
        source_dir = paper_input.source_dir
        same_paper = source_dir is not None and source_dir.resolve() == Path(fetched.source).resolve()
    else:
        same_paper = paper_input.arxiv_id == fetched.arxiv_id
    same_version = paper_input.version is None or fetched.version is None or paper_input.version == fetched.version
    if same_paper and same_version:
        return
    raise typer.BadParameter(
        f"--workdir {workdir} holds {_describe_paper(fetched.arxiv_id, fetched.version)}, "
        f"but the command asks for {_describe_paper(paper_input.arxiv_id, paper_input.version)}; "
        "name the paper that this directory holds, or choose another --workdir"
    )


def _query_latest(arxiv_id: str) -> PaperMeta:
    try:
        return arxiv.query(arxiv_id)
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
            paper_input = PaperInput(source_dir=Path(fetched.source))
        _check_fetched(paper_input, workdir, fetched)
    if paper_input.arxiv_id is not None and paper_input.version is None:
        if latest:
            version = None
        elif workdir is None:
            version = arxiv.latest_local_version(papers_dir(), paper_input.arxiv_id)
        else:
            version = None if fetched is None else fetched.version
        if version is None and may_query:
            meta = _query_latest(paper_input.arxiv_id)
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
    if isinstance(manifest, PrecompileManifest):
        parts = [f"{manifest.report.pages} pages"] if manifest.report else []
        if manifest.fix_session is not None:
            parts.append("1 fix session")
        return ", ".join(parts)
    if isinstance(manifest, MaskManifest):
        return f"{manifest.blocks_total} blocks masked"
    if isinstance(manifest, TranslateManifest):
        summary = f"{manifest.paragraphs_translated} paragraphs, {manifest.glossary.added} new terms"
        return summary if manifest.session is not None else f"{summary}, reused existing translation"
    if isinstance(manifest, CompileManifest):
        parts = [f"{manifest.report.pages} pages"] if manifest.report else []
        if manifest.baseline is not None:
            parts.append(f"baseline {manifest.baseline.pages}")
        if manifest.fix_session is not None:
            parts.append("1 fix session")
        return ", ".join(parts)
    return ""


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
                console.print("all five stages have finished ok; nothing to run. Use --from STAGE to redo.")
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
        str | None, typer.Argument(metavar="PAPER", help="arXiv id / arXiv URL / local source directory")
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
        raise typer.BadParameter("missing argument PAPER (arXiv id / arXiv URL / local source directory)")
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
    write_manifest(
        workdir.run_marker, RunMarker(pid=os.getpid(), started_at=datetime.now(UTC), command=command, mode=mode)
    )

    def terminate(signum: int, frame: FrameType | None) -> None:
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
        return marker
    except (ProcessLookupError, OverflowError):
        return None
    return marker


JsonOpt = Annotated[
    bool, typer.Option("--json", help="print one JSON object (UTF-8) with the same content instead of the table")
]


@app.command()
def status(paper: PaperArg, workdir: WorkdirOpt = None, dev: DevOpt = False, as_json: JsonOpt = False) -> None:
    _apply_dev(dev)
    paper_input, paper_workdir, _ = _paper_workdir(paper, workdir)
    stages = []
    for name in STAGES:
        manifest_path = paper_workdir.manifest_path(name)
        manifest = load_manifest(manifest_path, Manifest)
        stages.append(
            StageState(
                name=name,
                status=manifest.status if manifest is not None else ABSENT_STATUS,
                outputs_present=outputs_present(paper_workdir, name),
                message=manifest.message if manifest is not None else "",
                warnings=manifest.warnings if manifest is not None else [],
                manifest=manifest_path if manifest_path.is_file() else None,
            )
        )
    marker = _live_marker(paper_workdir)
    translate_manifest = load_manifest(paper_workdir.manifest_path(translate.STAGE_NAME), TranslateManifest)
    if marker is not None:
        mode = marker.mode
    else:
        mode = translate_manifest.mode if translate_manifest is not None else None
    snapshot = PaperStatus(
        workdir=paper_workdir.path,
        arxiv_id=paper_input.arxiv_id,
        version=paper_input.version,
        running=marker is not None,
        pid=marker.pid if marker is not None else None,
        started_at=marker.started_at if marker is not None else None,
        mode=mode,
        stages=stages,
        pdf=_existing(paper_workdir.zh_pdf),
        meta=_existing(paper_workdir.meta),
        glossary=_existing(paper_workdir.paper_glossary),
        notes=_existing(paper_workdir.notes),
    )
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


def _existing(path: Path) -> Path | None:
    return path if path.is_file() else None


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
    for detail in error.errors():
        error_console.print(f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}")


def _entry_lines(entries: list[tuple[str, object]]) -> list[str]:
    width = max(len(key) for key, _value in entries) + 2
    return [f"{key:<{width}}{_display(value)}" for key, value in entries]


def _parse_value(text: str) -> object:
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


def _saved(config: ModelsConfig, key: str) -> None:
    write_config(config)
    console.print(f"{key} = {_display(dict(config_entries(config))[key])}")


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
            changed = updated_config(config, key, _parse_value(text))
        except ValidationError as error:
            _print_validation_error(error)
            continue
        _saved(changed, key)
        return changed


def _edit_entry(config: ModelsConfig, key: str, value: object) -> ModelsConfig:
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
        entries = config_entries(config)
        cursor = _choose([*_entry_lines(entries), MENU_EXIT], cursor)
        if cursor == len(entries):
            return
        key, value = entries[cursor]
        config = _edit_entry(config, key, value)


@config_app.command("list")
def config_list(dev: DevOpt = False) -> None:
    _apply_dev(dev)
    for line in _entry_lines(config_entries(_loaded_config(load_config()))):
        console.print(line)


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(metavar="KEY", help="dotted key as tongtu config list prints it")],
    value: Annotated[
        str,
        typer.Argument(
            metavar="VALUE", help="a TOML value, or plain text taken as a string; an empty string clears the field"
        ),
    ],
    dev: DevOpt = False,
) -> None:
    _apply_dev(dev)
    config = _loaded_config(load_config())
    try:
        changed = updated_config(config, key, _parse_value(value))
    except KeyError:
        error_console.print(f"config key {key} does not exist; run tongtu config list to see the keys")
        raise typer.Exit(EXIT_USAGE) from None
    except ValidationError as error:
        _print_validation_error(error)
        raise typer.Exit(EXIT_USAGE) from None
    _saved(changed, key)


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
