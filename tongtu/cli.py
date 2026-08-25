from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__, validation
from .artifacts.common import Manifest
from .assets import asset_path
from .console import console, error_console
from .manifests import describe_error, load_manifest
from .model.config import DEFAULT_ASK_MODEL, MODELS_TEMPLATE, ModelsConfig, load_config, models_path, provider_key
from .pipeline import STAGES, clean_from, downstream, first_pending, outputs_present
from .processes import OUTPUT_EXCERPT_CHARS
from .stages import fetch, mask, precompile, review, survey, translate
from .stages.fetch import PaperArgumentError, PaperInput, parse_paper_argument
from .workdir import Workdir, WorkdirError, resolve

EXIT_FAILURE = 1

EXIT_USAGE = 2

STATUS_OK = "ok"

DEFAULT_JOBS = 4

XELATEX = "xelatex"

TOOLCHAIN_CHECKS: tuple[tuple[str, str], ...] = (
    (XELATEX, "编译引擎（latexmk -xelatex）"),
    ("latexmk", "编译回环驱动"),
    ("latexpand", "展开多文件源码"),
)

MIN_TEXLIVE_YEAR = 2026

VERSION_TIMEOUT_SECONDS = 30

TEXLIVE_CHECK_NAME = "TeX Live"

TEXLIVE_YEAR_PATTERN = re.compile(r"\(TeX Live (\d{4})\)")

FONT_CHECK_NAME = "中文字体"
CONFIG_CHECK_NAME = "models.toml"

FONTS_DIR = asset_path("fonts")

REQUIRED_FONT_FILENAMES: tuple[str, ...] = ("LXGWWenKai-Light.ttf", "LXGWWenKai-Medium.ttf")

StageName = Enum("StageName", {name: name for name in STAGES}, type=str)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="基于 LaTeX 源码的 arXiv 论文英译中引擎。",
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


def _pending_stage(name: str) -> Callable[[RunOptions], Manifest]:
    def entry(options: RunOptions) -> Manifest:
        error_console.print(f"阶段 {name} 尚未按提案图重写，流水线在此停止（重构步骤 3–8 逐个接入）。")
        raise typer.Exit(EXIT_FAILURE)

    return entry


def _fetch_entry(options: RunOptions) -> Manifest:
    return fetch.run(options.paper, options.workdir)


def _precompile_entry(options: RunOptions) -> Manifest:
    return precompile.run(options.workdir, model_override=options.work_model, effort=options.work_effort)


def _mask_entry(options: RunOptions) -> Manifest:
    return mask.run(options.workdir)


def _survey_entry(options: RunOptions) -> Manifest:
    return survey.run(
        options.workdir,
        glossary=options.glossary,
        no_terms=options.no_terms,
        ask_model=options.ask_model,
        ask_effort=options.ask_effort,
    )


def _translate_entry(options: RunOptions) -> Manifest:
    return translate.run(
        options.workdir,
        jobs=options.jobs,
        ask_model=options.ask_model,
        ask_effort=options.ask_effort,
    )


def _review_entry(options: RunOptions) -> Manifest:
    return review.run(
        options.workdir,
        skip=options.no_review,
        model_override=options.work_model,
        effort=options.work_effort,
    )


STAGE_ENTRIES: dict[str, Callable[[RunOptions], Manifest]] = {name: _pending_stage(name) for name in STAGES}
STAGE_ENTRIES["fetch"] = _fetch_entry
STAGE_ENTRIES["precompile"] = _precompile_entry
STAGE_ENTRIES["mask"] = _mask_entry
STAGE_ENTRIES["survey"] = _survey_entry
STAGE_ENTRIES["translate"] = _translate_entry
STAGE_ENTRIES["review"] = _review_entry


PaperArg = Annotated[str, typer.Argument(metavar="PAPER", help="arXiv 编号 / arXiv 链接 / 本地源码目录")]
FromOpt = Annotated[
    StageName | None,
    typer.Option(
        "--from",
        help=(f"从该阶段起全部重做，下游产物先删；不给则从第一个产物不在的阶段开始。阶段按序：{' → '.join(STAGES)}"),
    ),
]
AskModelOpt = Annotated[
    str | None,
    typer.Option(
        "--ask-model",
        metavar="PROVIDER/MODEL",
        help="覆盖本次涉及的全部 ask 类角色（survey_terms、translate）；PROVIDER 是 models.toml \\[provider.*] 的名字",
    ),
]
AskEffortOpt = Annotated[
    str | None, typer.Option("--ask-effort", metavar="LEVEL", help="推理强度，覆盖全部 ask 类角色")
]
WorkModelOpt = Annotated[
    str | None,
    typer.Option(
        "--work-model",
        metavar="RUNTIME/MODEL",
        help=(
            "覆盖本次涉及的全部 work 类角色（review、precompile_fix、compile_fix）；"
            "RUNTIME 是 models.toml \\[runtime.*] 的名字"
        ),
    ),
]
WorkEffortOpt = Annotated[
    str | None, typer.Option("--work-effort", metavar="LEVEL", help="推理强度，覆盖全部 work 类角色")
]
GlossaryOpt = Annotated[
    list[Path] | None,
    typer.Option("--glossary", metavar="FILE", help="命令行层术语表，可多次给，靠后优先"),
]
WorkdirOpt = Annotated[
    Path | None,
    typer.Option(
        "--workdir", metavar="DIR", help="论文工作目录（默认 $TONGTU_HOME/<编号>，再默认 ~/.local/share/tongtu/<编号>）"
    ),
]
JobsOpt = Annotated[int, typer.Option("--jobs", min=1, metavar="N", help="translate 并发度")]
NoTermsOpt = Annotated[bool, typer.Option("--no-terms", help="survey 不调模型提议术语表，只用你写的三层")]
NoReviewOpt = Annotated[bool, typer.Option("--no-review", help="跳过审校会话，译文原样进 compile")]


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


def _print_stage_result(name: str, manifest: Manifest, workdir: Workdir) -> None:
    console.print(f"{name}：状态 {manifest.status}")
    if manifest.message:
        console.print(f"  message  {manifest.message}")
    for line in manifest.warnings:
        console.print(f"  warning  {line}")
    if manifest.status != STATUS_OK:
        console.print(f"  manifest  {workdir.manifest_path(name)}")


def _run_stage(name: str, options: RunOptions) -> Manifest:
    with Progress(
        SpinnerColumn(), TextColumn("{task.description}"), TimeElapsedColumn(), console=console, transient=True
    ) as progress:
        progress.add_task(f"{name} 运行中…")
        manifest = STAGE_ENTRIES[name](options)
    _print_stage_result(name, manifest, options.workdir)
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
    if from_stage is not None:
        clean_from(options.workdir, from_stage.value)
        start = from_stage.value
        console.print(f"--from {start}：已删除 {start} 及其下游的产物")
    else:
        pending = first_pending(options.workdir)
        if pending is None:
            console.print(f"七个阶段的产物都在，本次不执行任何阶段；要重做，给 --from。工作目录 {options.workdir.path}")
            return
        start = pending
        if start != STAGES[0]:
            console.print(f"从 {start} 开始（更早阶段的产物已在）")
    options.workdir.create()
    raise _run_stages(start, options)


@app.command()
def stage(
    name: Annotated[
        StageName | None, typer.Argument(metavar="STAGE", help=f"阶段名，按序：{'、'.join(STAGES)}")
    ] = None,
    paper: Annotated[str | None, typer.Argument(metavar="PAPER", help="arXiv 编号 / arXiv 链接 / 本地源码目录")] = None,
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
        raise typer.BadParameter("缺参数 PAPER（arXiv 编号 / arXiv 链接 / 本地源码目录）")
    options = _options(paper, workdir, ask_model, ask_effort, work_model, work_effort, glossary, jobs, no_terms)
    missing = [
        upstream for upstream in STAGES[: STAGES.index(name.value)] if not outputs_present(options.workdir, upstream)
    ]
    if missing:
        error_console.print(
            f"上游产物不在：{'、'.join(missing)}。先跑 tongtu run，或用 tongtu run --from {missing[0]} 重做。"
        )
        raise typer.Exit(EXIT_USAGE)
    manifest = _run_stage(name.value, options)
    raise typer.Exit(0 if manifest.status == STATUS_OK else EXIT_FAILURE)


@app.command()
def status(paper: PaperArg, workdir: WorkdirOpt = None) -> None:
    _paper_input, paper_workdir = _paper_workdir(paper, workdir)
    console.print(f"工作目录 {paper_workdir.path}")
    table = Table(box=None, pad_edge=False)
    table.add_column("阶段")
    table.add_column("状态")
    table.add_column("message", overflow="fold")
    table.add_column("产物")
    table.add_column("manifest", overflow="fold")
    for name in STAGES:
        manifest_path = paper_workdir.manifest_path(name)
        manifest = load_manifest(manifest_path, Manifest)
        table.add_row(
            name,
            manifest.status if manifest is not None else "—",
            _status_message(manifest),
            "在" if outputs_present(paper_workdir, name) else "不在",
            str(manifest_path) if manifest_path.is_file() else "—",
        )
    console.print(table)


def _status_message(manifest: Manifest | None) -> str:
    if manifest is None:
        return "—"
    parts = [part for part in (manifest.message, *manifest.warnings) if part]
    return "；".join(parts) if parts else "—"


@app.command()
def validate(
    src: Annotated[Path, typer.Argument(help="原文 chunk 文件")],
    dst: Annotated[Path, typer.Argument(help="译文文件")],
) -> None:
    raise typer.Exit(validation.main([str(src), str(dst)]))


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
    rows: list[tuple[str, str, bool, str]] = []
    for name, purpose in TOOLCHAIN_CHECKS:
        rows.append((name, purpose, *_check_executable(name)))
        if name == XELATEX:
            rows.append((TEXLIVE_CHECK_NAME, f"TeX 发行版年份不低于 {MIN_TEXLIVE_YEAR}", *_check_texlive()))
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
    runtimes = _roles_refer_to(config, "runtime")
    providers = _roles_refer_to(config, "provider")
    for name in runtimes:
        runtime = config.runtime.get(name)
        if runtime is not None and runtime.provider is not None and runtime.provider not in providers:
            providers.append(runtime.provider)
    for name in providers:
        provider = config.provider.get(name)
        if provider is None:
            rows.append((f"密钥 {name}", "角色引用的服务商", False, f"models.toml 里没有声明服务商 {name}"))
            continue
        key, detail = provider_key(name, provider)
        rows.append((f"密钥 {name}", "服务商的 API 密钥", key is not None, detail))
    for name in runtimes:
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


def _check_texlive() -> tuple[bool, str]:
    if shutil.which(XELATEX) is None:
        return False, f"{XELATEX} 不在 PATH 里，无法检查"
    try:
        completed = subprocess.run(
            [XELATEX, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        return False, f"{XELATEX} --version 跑不起来：{describe_error(error)}"
    text = completed.stdout.strip()
    first_line = text.splitlines()[0] if text else ""
    match = TEXLIVE_YEAR_PATTERN.search(first_line)
    if match is None:
        return False, f"{XELATEX} --version 的输出里没有 TeX Live 年份；输出：{text[:OUTPUT_EXCERPT_CHARS]}"
    year = int(match.group(1))
    if year < MIN_TEXLIVE_YEAR:
        return False, f"TeX Live {year} 低于要求的 {MIN_TEXLIVE_YEAR}；用 install-tl 全量安装，不用发行版的 apt 包"
    return True, first_line


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
        elif (
            section == "roles" and stripped.split("=")[0].strip() in ask_roles and f'provider = "{chosen}"' not in line
        ):
            line = re.sub(r'provider = "[^"]*"', f'provider = "{chosen}"', line)
            line = re.sub(r'model = "[^"]*"', f'model = "{DEFAULT_ASK_MODEL[chosen]}"', line)
        lines.append(line)
    return "\n".join(lines) + "\n"


def main() -> None:
    if os.environ.get("TONGTU_DISABLE"):
        error_console.print("tongtu 不能在 agent 会话内运行（TONGTU_DISABLE 已设）")
        raise SystemExit(EXIT_USAGE)
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
