from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer
from rich.progress import (
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Column

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
from .model.config import CONFIG_TEMPLATE, FontsConfig, ModeName, Target, load_config, mode_target
from .model.work import AGENTRUN_EXECUTABLE
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

TOOLCHAIN_CHECKS: tuple[tuple[str, str], ...] = (
    (XELATEX, "compile engine (latexmk -xelatex)"),
    ("latexmk", "compile loop driver"),
    ("latexpand", "flattens multi-file sources"),
)

MIN_TEXLIVE_YEAR = 2026

VERSION_TIMEOUT_SECONDS = 30

TEXLIVE_CHECK_NAME = "TeX Live"

TEXLIVE_YEAR_PATTERN = re.compile(r"\(TeX Live (\d{4})\)")

FONT_CHECK_NAME = "CJK fonts"
CONFIG_CHECK_NAME = "config.toml"

REQUIRED_FONT_FILENAMES: tuple[str, ...] = tuple(
    name for family in (FontsConfig().main, FontsConfig().sans) for name in family_files(family) if name
)

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
        glossary=options.glossary,
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
    absent_toolchain = _print_doctor_rows(_toolchain_rows())
    absent_config = _print_doctor_rows(_config_rows())
    home = home_dir()
    for path in legacy_dirs_present():
        if path.resolve() == home.resolve():
            console.print(
                f"  {'note':<10}TONGTU_HOME points at the legacy directory {path}; papers now go to "
                f"{path}/papers/<id>/ and the config is read from {path}/config.toml"
            )
        else:
            console.print(
                f"  {'note':<10}legacy directory {path} exists and is no longer read; move its contents under "
                f"{home} (config.toml, glossary.json, papers/<id>/) and delete it"
            )
    if absent_toolchain:
        console.print(f"environment incomplete: {', '.join(absent_toolchain)}")
        raise typer.Exit(EXIT_FAILURE)
    if absent_config:
        console.print(
            f"toolchain and fonts complete; {', '.join(absent_config)} not usable, so translate and the fix "
            "sessions cannot run; fetch, precompile and mask still can."
        )
        raise typer.Exit(EXIT_NOT_CONFIGURED)
    console.print("environment complete.")


def _print_doctor_rows(rows: list[tuple[str, str, bool, str]]) -> list[str]:
    for name, purpose, found, detail in rows:
        mark = "[ok]" if found else "[missing]"
        console.print(f"  {mark:<10}{name:<14}{purpose:<42}{detail}")
    return [name for name, _purpose, found, _detail in rows if not found]


def _toolchain_rows() -> list[tuple[str, str, bool, str]]:
    rows: list[tuple[str, str, bool, str]] = []
    for name, purpose in TOOLCHAIN_CHECKS:
        rows.append((name, purpose, *_check_executable(name)))
        if name == XELATEX:
            rows.append((TEXLIVE_CHECK_NAME, f"distribution year >= {MIN_TEXLIVE_YEAR}", *_check_texlive()))
    rows.append((FONT_CHECK_NAME, "bundled font files (LXGW WenKai, Source Han Sans SC)", *_check_fonts()))
    return rows


def _config_rows() -> list[tuple[str, str, bool, str]]:
    config, detail = load_config()
    if config is None:
        return [
            (CONFIG_CHECK_NAME, "modes, fonts and pdf options", False, detail),
            ("runtimes", "runtimes and models of the modes", False, "cannot check: config.toml is unreadable"),
        ]
    rows = [(CONFIG_CHECK_NAME, "modes, fonts and pdf options", True, str(config_path()))]
    targets: dict[str, Target] = {}
    for name in ModeName:
        target, _detail = mode_target(config, name)
        if target is not None:
            targets.setdefault(str(target), target)
    for target in targets.values():
        rows += _runtime_rows(target)
    return rows


def _runtime_rows(target: Target) -> list[tuple[str, str, bool, str]]:
    found, detail = _check_executable(AGENTRUN_EXECUTABLE)
    if not found:
        return [(str(target), "agentrun, which starts every runtime", False, detail)]
    command = [detail, "doctor", target.runtime, "--json"] + (["--model", target.model] if target.model else [])
    try:
        completed = subprocess.run(command, capture_output=True, text=True)
    except OSError as error:
        return [(str(target), "agentrun doctor", False, describe_error(error))]
    try:
        checks = json.loads(completed.stdout)
        return [
            (
                str(target),
                f"agentrun doctor: {check['check']}",
                check["status"] != "fail",
                check["detail"] if check["status"] != "skip" else f"skipped: {check['detail']}",
            )
            for check in checks
        ]
    except (ValueError, TypeError, KeyError):
        stderr = completed.stderr.strip()[:OUTPUT_EXCERPT_CHARS]
        return [(str(target), "agentrun doctor", False, stderr or "agentrun doctor printed no check list")]


def _check_executable(name: str) -> tuple[bool, str]:
    path = shutil.which(name)
    if path is None:
        return False, f"{name} not found in PATH"
    return True, path


def _check_texlive() -> tuple[bool, str]:
    if shutil.which(XELATEX) is None:
        return False, f"{XELATEX} is not in PATH; cannot check"
    try:
        completed = subprocess.run(
            [XELATEX, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        return False, f"failed to run {XELATEX} --version: {describe_error(error)}"
    text = completed.stdout.strip()
    first_line = text.splitlines()[0] if text else ""
    match = TEXLIVE_YEAR_PATTERN.search(first_line)
    if match is None:
        return False, f"no TeX Live year in the {XELATEX} --version output; output: {text[:OUTPUT_EXCERPT_CHARS]}"
    year = int(match.group(1))
    if year < MIN_TEXLIVE_YEAR:
        return False, (
            f"TeX Live {year} is below the required {MIN_TEXLIVE_YEAR}; "
            "install with install-tl, not the distro apt package"
        )
    return True, first_line


def _check_fonts() -> tuple[bool, str]:
    absent = [name for name in REQUIRED_FONT_FILENAMES if not (FONTS_DIR / name).is_file()]
    if absent:
        return False, f"missing {', '.join(absent)} under {FONTS_DIR}"
    return True, str(FONTS_DIR)


@app.command()
def setup(dev: DevOpt = False) -> None:
    _apply_dev(dev)
    path = config_path()
    if path.exists():
        console.print(f"config file {path} already exists; not overwriting. Edit that file to change the config.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    path.chmod(0o600)
    console.print(f"wrote {path}.")


def main() -> None:
    if os.environ.get("TONGTU_DISABLE"):
        error_console.print("tongtu cannot run inside an agent session (TONGTU_DISABLE is set)")
        raise SystemExit(EXIT_USAGE)
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
