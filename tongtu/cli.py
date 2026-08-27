from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from . import __version__, validation
from .artifacts.common import Manifest
from .artifacts.compile import CompileManifest
from .artifacts.fetch import FetchManifest
from .artifacts.mask import MaskManifest
from .artifacts.precompile import PrecompileManifest
from .artifacts.survey import SurveyManifest
from .artifacts.translate import ChunkTranslateStatus, TranslateManifest
from .assets import asset_path
from .console import console, error_console
from .manifests import describe_error, load_manifest
from .model.config import DEFAULT_ASK_MODEL, MODELS_TEMPLATE, ModelsConfig, load_config, models_path, provider_key
from .pipeline import STAGES, clean_from, downstream, first_pending, outputs_present
from .processes import OUTPUT_EXCERPT_CHARS
from .stages import compile, fetch, mask, precompile, review, survey, translate
from .stages.fetch import PaperArgumentError, PaperInput, parse_paper_argument
from .workdir import Workdir, WorkdirError, resolve

EXIT_FAILURE = 1

EXIT_USAGE = 2

STATUS_OK = "ok"

DEFAULT_JOBS = 4

CHUNKED_STAGES = frozenset({"translate", "review"})

INFLIGHT_SHOWN = 4

BAR_WIDTH = 16

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
CONFIG_CHECK_NAME = "models.toml"

FONTS_DIR = asset_path("fonts")

REQUIRED_FONT_FILENAMES: tuple[str, ...] = ("LXGWWenKai-Light.ttf", "LXGWWenKai-Medium.ttf")

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
    ask_model: str | None
    ask_effort: str | None
    work_model: str | None
    work_effort: str | None
    glossary: tuple[Path, ...]
    jobs: int
    no_terms: bool
    no_review: bool


def _kilo(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k"


class TokenEtaColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        start = task.fields.get("rate_start")
        start_tokens = task.fields.get("rate_start_tokens")
        if start is None or task.total is None:
            return Text("eta -:--:--")
        advanced = task.completed - start_tokens
        elapsed = time.monotonic() - start
        if advanced <= 0 or elapsed <= 0:
            return Text("eta -:--:--")
        remaining = max(0.0, task.total - task.completed)
        return Text(f"eta {timedelta(seconds=int(remaining * elapsed / advanced))}")


@dataclass
class StageDisplay:
    progress: Progress
    task: TaskID
    name: str
    rate_baseline: tuple[float, int] | None = None

    def chunks(self, done: int, total: int, inflight: tuple[str, ...], done_tokens: int, total_tokens: int) -> None:
        if self.rate_baseline is None:
            self.rate_baseline = (time.monotonic(), done_tokens)
        listed = " ".join(inflight[:INFLIGHT_SHOWN])
        if len(inflight) > INFLIGHT_SHOWN:
            listed = f"{listed} +{len(inflight) - INFLIGHT_SHOWN} more"
        self.progress.update(
            self.task,
            completed=done_tokens,
            total=total_tokens,
            chunks=f"{done}/{total}",
            tokens=f"{_kilo(done_tokens)}/{_kilo(total_tokens)} tok",
            inflight=f"inflight {listed}" if listed else "",
            rate_start=self.rate_baseline[0],
            rate_start_tokens=self.rate_baseline[1],
        )

    def action(self, text: str) -> None:
        self.progress.update(self.task, description=f"{self.name}  {text}")


def _fetch_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return fetch.run(options.paper, options.workdir)


def _precompile_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return precompile.run(
        options.workdir, model_override=options.work_model, effort=options.work_effort, report=display.action
    )


def _mask_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return mask.run(options.workdir)


def _survey_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return survey.run(
        options.workdir,
        glossary=options.glossary,
        no_terms=options.no_terms,
        ask_model=options.ask_model,
        ask_effort=options.ask_effort,
    )


def _translate_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return translate.run(
        options.workdir,
        jobs=options.jobs,
        ask_model=options.ask_model,
        ask_effort=options.ask_effort,
        report=display.chunks,
    )


def _review_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return review.run(
        options.workdir,
        skip=options.no_review,
        model_override=options.work_model,
        effort=options.work_effort,
        report=display.action,
    )


def _compile_entry(options: RunOptions, display: StageDisplay) -> Manifest:
    return compile.run(
        options.workdir, model_override=options.work_model, effort=options.work_effort, report=display.action
    )


STAGE_ENTRIES: dict[str, Callable[[RunOptions, StageDisplay], Manifest]] = {
    "fetch": _fetch_entry,
    "precompile": _precompile_entry,
    "mask": _mask_entry,
    "survey": _survey_entry,
    "translate": _translate_entry,
    "review": _review_entry,
    "compile": _compile_entry,
}


PaperArg = Annotated[str, typer.Argument(metavar="PAPER", help="arXiv id / arXiv URL / local source directory")]
FromOpt = Annotated[
    StageName | None,
    typer.Option(
        "--from",
        help=(
            "redo everything from this stage on, removing downstream outputs first; without it the run "
            f"starts at the first stage whose outputs are absent. Stage order: {' → '.join(STAGES)}"
        ),
    ),
]
AskModelOpt = Annotated[
    str | None,
    typer.Option(
        "--ask-model",
        metavar="PROVIDER/MODEL",
        help=(
            "override every ask role involved in this run (survey_terms, translate); "
            "PROVIDER is a \\[provider.*] name in models.toml"
        ),
    ),
]
AskEffortOpt = Annotated[
    str | None, typer.Option("--ask-effort", metavar="LEVEL", help="reasoning effort, overrides every ask role")
]
WorkModelOpt = Annotated[
    str | None,
    typer.Option(
        "--work-model",
        metavar="RUNTIME/MODEL",
        help=(
            "override every work role involved in this run (review, precompile_fix, compile_fix); "
            "RUNTIME is a \\[runtime.*] name in models.toml"
        ),
    ),
]
WorkEffortOpt = Annotated[
    str | None, typer.Option("--work-effort", metavar="LEVEL", help="reasoning effort, overrides every work role")
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
        help="paper working directory (default $TONGTU_HOME/<id>, then ~/.local/share/tongtu/<id>)",
    ),
]
JobsOpt = Annotated[int, typer.Option("--jobs", min=1, metavar="N", help="translate concurrency")]
NoTermsOpt = Annotated[
    bool,
    typer.Option("--no-terms", help="survey skips model term proposals and uses only your three glossary layers"),
]
NoReviewOpt = Annotated[
    bool, typer.Option("--no-review", help="skip the review session; the translation enters compile unchanged")
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


def _workdir_name(paper_input: PaperInput) -> str:
    if paper_input.source_dir is not None:
        return paper_input.source_dir.name
    return paper_input.arxiv_id or ""


def _paper_workdir(paper: str, workdir: Path | None) -> tuple[PaperInput, Workdir]:
    try:
        paper_input = parse_paper_argument(paper)
        path = resolve(_workdir_name(paper_input), workdir)
    except (PaperArgumentError, WorkdirError) as error:
        raise typer.BadParameter(str(error)) from error
    return paper_input, Workdir(path)


def _options(
    paper: str,
    workdir: Path | None,
    ask_model: str | None,
    ask_effort: str | None,
    work_model: str | None,
    work_effort: str | None,
    glossary: list[Path] | None,
    jobs: int,
    no_terms: bool,
    no_review: bool = False,
) -> RunOptions:
    paper_input, paper_workdir = _paper_workdir(paper, workdir)
    return RunOptions(
        paper=paper_input,
        workdir=paper_workdir,
        ask_model=ask_model,
        ask_effort=ask_effort,
        work_model=work_model,
        work_effort=work_effort,
        glossary=tuple(glossary or ()),
        jobs=jobs,
        no_terms=no_terms,
        no_review=no_review,
    )


def _elapsed_text(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def _stage_summary(manifest: Manifest) -> str:
    if isinstance(manifest, FetchManifest):
        files = f"{len(manifest.tex_files)} tex files"
        return f"{manifest.kind}, {files}" if manifest.kind else files
    if isinstance(manifest, PrecompileManifest):
        parts = [f"{manifest.report.pages} pages"] if manifest.report else []
        if manifest.fix_session is not None:
            parts.append("1 fix session")
        return ", ".join(parts)
    if isinstance(manifest, MaskManifest):
        return f"{manifest.blocks_total} blocks masked"
    if isinstance(manifest, SurveyManifest):
        return f"{manifest.chunks_total} chunks, {manifest.terms_total} terms"
    if isinstance(manifest, TranslateManifest):
        fallback = sum(1 for record in manifest.chunks.values() if record.status is ChunkTranslateStatus.FALLBACK)
        chunks = f"{len(manifest.chunks)} chunks"
        return f"{chunks}, {fallback} fallback" if fallback else chunks
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
    ok = manifest.status == STATUS_OK
    mark = MARK_OK if ok else MARK_FAILED
    summary = _stage_summary(manifest) if ok else ""
    line = f"{mark} {name:<{NAME_WIDTH}}{manifest.status:<{STATUS_WIDTH}}{summary:<{SUMMARY_WIDTH}}"
    console.print(f"{line}{_elapsed_text(seconds)}")
    if manifest.message:
        console.print(f"    {manifest.message}")
    for warning in manifest.warnings:
        console.print(f"    warning: {warning}")
    if not ok:
        console.print(f"    manifest  {workdir.manifest_path(name)}")


def _progress_columns(name: str) -> tuple:
    columns = [SpinnerColumn(), TextColumn("{task.description}")]
    if name in CHUNKED_STAGES:
        columns += [
            BarColumn(bar_width=BAR_WIDTH),
            TextColumn("{task.fields[chunks]}"),
            TextColumn("{task.fields[tokens]}"),
            TextColumn("{task.fields[inflight]}"),
        ]
    columns.append(TimeElapsedColumn())
    if name in CHUNKED_STAGES:
        columns.append(TokenEtaColumn())
    return tuple(columns)


def _run_stage(name: str, options: RunOptions) -> Manifest:
    started = time.monotonic()
    with Progress(
        *_progress_columns(name), console=console, transient=True, disable=not console.is_terminal
    ) as progress:
        task = progress.add_task(name, total=None, chunks="", tokens="", inflight="")
        display = StageDisplay(progress=progress, task=task, name=name)
        manifest = STAGE_ENTRIES[name](options, display)
    _print_stage_result(name, manifest, options.workdir, time.monotonic() - started)
    return manifest


def _run_stages(start: str, options: RunOptions) -> typer.Exit:
    for name in downstream(start):
        if _run_stage(name, options).status != STATUS_OK:
            return typer.Exit(EXIT_FAILURE)
    return typer.Exit(0)


@app.command()
def run(
    paper: PaperArg,
    from_stage: FromOpt = None,
    ask_model: AskModelOpt = None,
    ask_effort: AskEffortOpt = None,
    work_model: WorkModelOpt = None,
    work_effort: WorkEffortOpt = None,
    glossary: GlossaryOpt = None,
    workdir: WorkdirOpt = None,
    jobs: JobsOpt = DEFAULT_JOBS,
    no_terms: NoTermsOpt = False,
    no_review: NoReviewOpt = False,
) -> None:
    options = _options(
        paper, workdir, ask_model, ask_effort, work_model, work_effort, glossary, jobs, no_terms, no_review
    )
    console.print(f"workdir {options.workdir.path}")
    if from_stage is not None:
        clean_from(options.workdir, from_stage.value)
        start = from_stage.value
        console.print(f"--from {start}: removed {start} and downstream outputs")
    else:
        pending = first_pending(options.workdir)
        if pending is None:
            console.print("all seven stage outputs are present; nothing to run. Use --from STAGE to redo.")
            return
        start = pending
        if start != STAGES[0]:
            console.print(f"resuming from {start} (upstream outputs present)")
    options.workdir.create()
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
    ask_model: AskModelOpt = None,
    ask_effort: AskEffortOpt = None,
    work_model: WorkModelOpt = None,
    work_effort: WorkEffortOpt = None,
    glossary: GlossaryOpt = None,
    workdir: WorkdirOpt = None,
    jobs: JobsOpt = DEFAULT_JOBS,
    no_terms: NoTermsOpt = False,
) -> None:
    if name is None:
        console.print(" → ".join(STAGES))
        return
    if paper is None:
        raise typer.BadParameter("missing argument PAPER (arXiv id / arXiv URL / local source directory)")
    options = _options(paper, workdir, ask_model, ask_effort, work_model, work_effort, glossary, jobs, no_terms)
    missing = [
        upstream for upstream in STAGES[: STAGES.index(name.value)] if not outputs_present(options.workdir, upstream)
    ]
    if missing:
        error_console.print(
            f"upstream outputs absent: {', '.join(missing)}. "
            f"Run tongtu run first, or tongtu run --from {missing[0]} to redo."
        )
        raise typer.Exit(EXIT_USAGE)
    _print_stage_header()
    manifest = _run_stage(name.value, options)
    raise typer.Exit(0 if manifest.status == STATUS_OK else EXIT_FAILURE)


@app.command()
def status(paper: PaperArg, workdir: WorkdirOpt = None) -> None:
    _paper_input, paper_workdir = _paper_workdir(paper, workdir)
    console.print(f"workdir {paper_workdir.path}")
    console.print("")
    console.print(
        f"{'stage':<{NAME_WIDTH}}{'status':<{STATUS_WIDTH}}{'outputs':<{OUTPUTS_WIDTH}}manifest", style=HEADER_STYLE
    )
    for name in STAGES:
        manifest_path = paper_workdir.manifest_path(name)
        manifest = load_manifest(manifest_path, Manifest)
        status_cell = manifest.status if manifest is not None else ABSENT_CELL
        outputs_cell = "present" if outputs_present(paper_workdir, name) else "absent"
        manifest_cell = str(manifest_path.relative_to(paper_workdir.path)) if manifest_path.is_file() else ABSENT_CELL
        console.print(
            f"{name:<{NAME_WIDTH}}{status_cell:<{STATUS_WIDTH}}{outputs_cell:<{OUTPUTS_WIDTH}}{manifest_cell}"
        )
        for note in [manifest.message, *manifest.warnings] if manifest is not None else []:
            if note:
                console.print(f"{'':<{NAME_WIDTH}}  {note}")


@app.command()
def validate(
    src: Annotated[Path, typer.Argument(help="source chunk file")],
    dst: Annotated[Path, typer.Argument(help="translated file")],
) -> None:
    raise typer.Exit(validation.main([str(src), str(dst)]))


@app.command()
def doctor() -> None:
    absent_toolchain = _print_doctor_rows(_toolchain_rows())
    absent_config = _print_doctor_rows(_config_rows())
    if absent_toolchain:
        console.print(f"environment incomplete: {', '.join(absent_toolchain)}")
        raise typer.Exit(EXIT_FAILURE)
    if absent_config:
        console.print(
            f"toolchain and fonts complete; {', '.join(absent_config)} not configured, "
            "stages from survey on cannot run."
        )
        return
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
    rows.append((FONT_CHECK_NAME, "font fallback chain (LXGW WenKai bundled)", *_check_fonts()))
    return rows


def _config_rows() -> list[tuple[str, str, bool, str]]:
    config, detail = load_config()
    if config is None:
        return [
            (CONFIG_CHECK_NAME, "providers, runtimes and roles", False, detail),
            ("keys", "provider API keys", False, "cannot check: models.toml is unreadable"),
            ("runtimes", "runtime executables", False, "cannot check: models.toml is unreadable"),
        ]
    rows = [(CONFIG_CHECK_NAME, "providers, runtimes and roles", True, str(models_path()))]
    runtimes = _roles_refer_to(config, "runtime")
    providers = _roles_refer_to(config, "provider")
    for name in runtimes:
        runtime = config.runtime.get(name)
        if runtime is not None and runtime.provider is not None and runtime.provider not in providers:
            providers.append(runtime.provider)
    for name in providers:
        provider = config.provider.get(name)
        if provider is None:
            rows.append(
                (
                    f"key {name}",
                    "provider referenced by a role",
                    False,
                    f"provider {name} is not declared in models.toml",
                )
            )
            continue
        key, detail = provider_key(name, provider)
        rows.append((f"key {name}", "provider API key", key is not None, detail))
    for name in runtimes:
        runtime = config.runtime.get(name)
        if runtime is None:
            rows.append(
                (
                    f"runtime {name}",
                    "runtime referenced by a role",
                    False,
                    f"runtime {name} is not declared in models.toml",
                )
            )
            continue
        rows.append((f"runtime {name}", "session runtime executable", *_check_executable(runtime.command[0])))
    return rows


def _roles_refer_to(config: ModelsConfig, field: str) -> list[str]:
    return list(dict.fromkeys(name for entry in config.roles.values() if (name := getattr(entry, field))))


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
def setup(
    interactive: Annotated[bool, typer.Option("-i", help="interactively pick providers and fill API keys")] = False,
) -> None:
    path = models_path()
    if path.exists():
        console.print(f"config file {path} already exists; not overwriting. Edit that file to change the config.")
        return
    text = _interactive_models_toml() if interactive else MODELS_TEMPLATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    console.print(f"wrote {path}.")


def _interactive_models_toml() -> str:
    template = tomllib.loads(MODELS_TEMPLATE)
    keys: dict[str, str] = {}
    for name in template["provider"]:
        if typer.confirm(f"configure {name}?", default=False):
            keys[name] = typer.prompt(f"API key for {name}", hide_input=True)
    if not keys:
        console.print("no provider chosen. At least one is needed to call models; run tongtu setup -i again.")
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
        elif (
            section == "roles" and stripped.split("=")[0].strip() in ask_roles and f'provider = "{chosen}"' not in line
        ):
            line = re.sub(r'provider = "[^"]*"', f'provider = "{chosen}"', line)
            line = re.sub(r'model = "[^"]*"', f'model = "{DEFAULT_ASK_MODEL[chosen]}"', line)
        lines.append(line)
    return "\n".join(lines) + "\n"


def main() -> None:
    if os.environ.get("TONGTU_DISABLE"):
        error_console.print("tongtu cannot run inside an agent session (TONGTU_DISABLE is set)")
        raise SystemExit(EXIT_USAGE)
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
