"""`tongtu` CLI 命令面。

`stage fetch`、`stage flatten`、`stage precompile`、`stage mask` 与 `doctor` 已接线，走真实的阶段驱动器与环境检查；
其余命令为占位实现：只解析并校验参数、说明将要执行的动作，不运行 pipeline。run / validate /
`tex compile` 的退出码是机器判据，占位结果不得被误当成真实结论，故占位命令统一以 ``EXIT_STUB``
（99）退出；`--help` 退 0、用法错误退 2，这两类行为是真实的。
"""

from __future__ import annotations

import re
import shutil
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, config
from .agent import opencode
from .artifacts.fetch import FetchStatus
from .artifacts.flatten import FlattenManifest, FlattenStatus
from .artifacts.mask import MaskManifest, MaskStatus
from .artifacts.precompile import PrecompileManifest, PrecompileStatus
from .assets import asset_path
from .masking import BlockCategory
from .stages import STAGES
from .stages import fetch as fetch_stage
from .stages import flatten as flatten_stage
from .stages import mask as mask_stage
from .stages import precompile as precompile_stage
from .workdir import WorkdirError

# 模块级退出码常量是退出码的集中登记处：新增退出码在此定义并注释含义。

#: 一般失败：未能出包、下载或解包失败、校验有失败层、环境有缺失、编译失败。
EXIT_FAILURE = 1

#: 业务分支段（3–9，跨子命令同码同义）的首个登记：源是 PDF 而非 LaTeX 源码，
#: 走 degraded path。
EXIT_PDF_ONLY = 3

#: 占位实现的统一退出码。取值远离成功（0）、一般失败（1）、用法错误（2）与业务
#: 分支段（3–9），使占位结果在任何脚本化调用里都不可能被读成真实结论；命令逐个
#: 接线后此码退役。
EXIT_STUB = 99

#: chunk id 形状：`c` 后至少三位数字，如 c012。
_CHUNK_ID_RE = re.compile(r"^c[0-9]{3,}$")

#: doctor 检查项（架构 §6）第一组：工具链与字体。缺任一项则编译无法进行，计入退出码。
DOCTOR_TOOLCHAIN_CHECKS: tuple[tuple[str, str], ...] = (
    ("xelatex", "编译引擎（latexmk -xelatex）"),
    ("latexmk", "编译回环驱动"),
    ("latexpand", "flatten 阶段展开多文件源码"),
    ("pdftocairo", "figures 矢量源转位图"),
    ("epstopdf", "EPS 图源接入 xelatex 的转换链"),
    ("中文字体", "font fallback chain（霞鹜文楷随仓库分发）"),
)

#: doctor 检查项第二组：运行期凭证。如实报告缺失，但不计入退出码——参考镜像是可分发
#: 产物，构建它的机器不该需要凭证，而凭证缺失也不影响 survey 之前各阶段的编译路径。
DOCTOR_CREDENTIAL_CHECKS: tuple[tuple[str, str], ...] = (
    ("OpenCode 密钥", "ask 原语的 API 直调（环境变量 / 录入的密钥 / opencode 登录态，三处任一）"),
)

#: 全部检查项，按输出顺序。
DOCTOR_CHECKS: tuple[tuple[str, str], ...] = DOCTOR_TOOLCHAIN_CHECKS + DOCTOR_CREDENTIAL_CHECKS

#: DOCTOR_CHECKS 里两个特例检查项的名字：字体项查字体文件，密钥项按三级顺序解析，
#: 其余各项按可执行文件查 PATH。
FONT_CHECK_NAME = "中文字体"
KEY_CHECK_NAME = "OpenCode 密钥"

#: 字体目录；`fonts/` 随仓库分发，两种布局下的定位交给 assets。
FONTS_DIR = asset_path("fonts")

#: 字体目录里必须存在的字体文件（霞鹜文楷 Light / Medium，用途见 fonts/README.md）。
REQUIRED_FONT_FILENAMES: tuple[str, ...] = ("LXGWWenKai-Light.ttf", "LXGWWenKai-Medium.ttf")

#: `tongtu stage` 的阶段名选项，取值即 tongtu.stages.STAGES。
StageName = Enum("StageName", {name: name for name in STAGES}, type=str)

console = Console()
error_console = Console(stderr=True)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="基于 LaTeX 源码的 arXiv 论文英译中引擎。",
)

tex_app = typer.Typer(
    no_args_is_help=True,
    help="编译修复会话的工具面，不面向人；权限规则见架构 §3 compile 节。",
)
# 不面向人，故不出现在顶层 help；`tongtu tex --help` 仍可用。
app.add_typer(tex_app, name="tex", hidden=True)

# ------------------------------------------------------------- 共享参数类型

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
    typer.Option("--json", help="向 stdout 输出机器可读事件流（JSON Lines，架构 §6；事件类型随 run 接线定义）"),
]
AgentOpt = Annotated[
    str | None, typer.Option("--agent", metavar="NAME", help="agent 运行时适配器（注册表在 tongtu/agent/）")
]
ModelOpt = Annotated[
    str | None,
    typer.Option("--model", metavar="ID", help="模型标识，透传给 agent 运行时；模型标识进翻译缓存 key（架构 §4）"),
]
ChunkIdArg = Annotated[str, typer.Argument(metavar="CHUNK_ID", help="chunk id，形如 c012")]


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
    """基于 LaTeX 源码的 arXiv 论文英译中引擎。"""


def _stub_exit(command: str, **fields: object) -> typer.Exit:
    """打印占位说明与解析结果，返回统一退出码的 Exit。命令接线后随之删除。"""
    console.print(f"tongtu {command}：占位实现，未执行任何操作（退出码 {EXIT_STUB}）")
    table = Table(show_header=False, box=None, pad_edge=False)
    for key, value in fields.items():
        table.add_row(f"  {key}", "—" if value in (None, [], ()) else str(value))
    console.print(table)
    return typer.Exit(EXIT_STUB)


# --------------------------------------------------------------------- 主命令面


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
    """跑完整 pipeline。幂等：重复执行按 manifest 与翻译缓存跳过已完成部分。"""
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
    """chunk 级失效重算（incremental retranslation），失效语义见架构 §4 返工触发表。"""
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
    workdir: WorkdirOpt = None,
    force: Annotated[bool, typer.Option("--force", help="无视已有 manifest 结论重新执行")] = False,
    json_output: JsonOpt = False,
    agent: AgentOpt = None,
    model: ModelOpt = None,
) -> None:
    """单阶段入口，调试用：上游阶段从工作目录装载已有产物；重跑语义见各阶段设计，--force 强制重算。"""
    if name.value == fetch_stage.STAGE_NAME:
        # fetch 无 agent 介入，--agent 与 --model 不参与执行。
        raise _run_stage_fetch(paper, workdir, force, json_output)
    if name.value == flatten_stage.STAGE_NAME:
        # flatten 判定不出主文件时才需要 agent，该介入点推迟实现，--agent 与 --model 不参与执行。
        raise _run_stage_flatten(paper, workdir, force, json_output)
    if name.value == precompile_stage.STAGE_NAME:
        # precompile 编不过时拉起修复会话，模型标识透传给驱动器；agent 运行时目前唯一，
        # 适配层还没有注册表，--agent 无消费方。
        raise _run_stage_precompile(paper, workdir, force, json_output, model)
    if name.value == mask_stage.STAGE_NAME:
        # mask 是纯文本变换，未知环境交给 agent 分类的介入点推迟实现，--agent 与 --model 不参与执行。
        raise _run_stage_mask(paper, workdir, force, json_output)
    raise _stub_exit(
        "stage", name=name.value, paper=paper, workdir=workdir, force=force, json=json_output, agent=agent, model=model
    )


def _warn_json_ignored(json_output: bool) -> None:
    """`--json` 的事件流 schema 尚未定义，三个已接线的阶段都先提示忽略。"""
    if json_output:
        error_console.print("--json：事件流 schema 尚未定义，本次忽略该选项")


def _workdir_name_from_paper(paper: str) -> str:
    """由论文参数得出工作目录名：本地目录形态取 basename，编号与链接形态解析成编号。

    flatten 与 precompile 都不访问网络也不读源目录内容，论文参数只用来定位工作目录。参数
    不合法时抛 `PaperArgumentError` 或 `WorkdirError`，由调用方转 typer 的用法错误。
    """
    paper_input = fetch_stage.parse_paper_argument(paper)
    if paper_input.source_dir is not None:
        return paper_input.source_dir.name
    return paper_input.arxiv_id


def _print_skipped(stage_name: str, status: str, manifest_path: Path) -> None:
    """命中跳过时的两行人读输出：结论状态与 manifest 路径。"""
    console.print(f"{stage_name} 跳过：manifest 已有结论（状态 {status}），--force 可重新执行")
    console.print(f"  manifest  {manifest_path}")


def _upstream_exit_code(*, ok: bool, pdf_only_chain: bool) -> int:
    """flatten、precompile 与 mask 共用的退出码映射：ok 退 0，上游判定为 PDF 退 3，其余失败态退 1。

    `pdf_only_chain` 指本阶段的失败源自上游把源判定成 PDF 而非 LaTeX 源码，调用方据此改道
    degraded path；该退出码在业务分支段（3–9），跨子命令同码同义。
    """
    if ok:
        return 0
    if pdf_only_chain:
        return EXIT_PDF_ONLY
    return EXIT_FAILURE


def _run_stage_fetch(paper: str, workdir: Path | None, force: bool, json_output: bool) -> typer.Exit:
    """`stage fetch` 的接线：识别论文参数、调驱动器、打印结果、映射退出码。"""
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
    table.add_column(overflow="fold")  # 路径与 message 超宽时折行，不截断
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
    """fetch 状态到退出码的映射；命中跳过时对已存结论的状态取同样的映射。"""
    if status is FetchStatus.OK:
        return 0
    if status is FetchStatus.PDF_ONLY:
        return EXIT_PDF_ONLY
    return EXIT_FAILURE


def _run_stage_flatten(paper: str, workdir: Path | None, force: bool, json_output: bool) -> typer.Exit:
    """`stage flatten` 的接线：由论文参数定位工作目录、调驱动器、打印结果、映射退出码。

    flatten 不访问网络也不读源目录内容，论文参数只用来定位工作目录：本地目录形态取它的
    basename，编号与链接形态解析成编号，两者都作为工作目录名交给驱动器。
    """
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
    table.add_column(overflow="fold")  # 路径与 message 超宽时折行，不截断
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
    """flatten 状态到退出码的映射；命中跳过时对已存结论取同样的映射。"""
    return _upstream_exit_code(
        ok=manifest.status is FlattenStatus.OK,
        pdf_only_chain=(
            manifest.status is FlattenStatus.FETCH_NOT_OK and manifest.fetch_status == FetchStatus.PDF_ONLY
        ),
    )


def _run_stage_precompile(
    paper: str, workdir: Path | None, force: bool, json_output: bool, model: str | None
) -> typer.Exit:
    """`stage precompile` 的接线：定位工作目录、调驱动器、打印编译结果与基线数据、映射退出码。

    precompile 不访问网络也不读源目录内容，论文参数只用来定位工作目录：本地目录形态取它的
    basename，编号与链接形态解析成编号，两者都作为工作目录名交给驱动器。`--model` 透传给
    驱动器，由它交给修复会话的 agent 运行时。
    """
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
    table.add_column(overflow="fold")  # 路径与 message 超宽时折行，不截断
    # 五个计数只在编译通过时可信，失败态一律打「—」。
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
    """precompile 状态到退出码的映射；命中跳过时对已存结论取同样的映射。"""
    return _upstream_exit_code(
        ok=manifest.status is PrecompileStatus.OK,
        pdf_only_chain=(
            manifest.status is PrecompileStatus.FLATTEN_NOT_OK and manifest.fetch_status == FetchStatus.PDF_ONLY
        ),
    )


def _run_stage_mask(paper: str, workdir: Path | None, force: bool, json_output: bool) -> typer.Exit:
    """`stage mask` 的接线：定位工作目录、调驱动器、打印掩码结果、映射退出码。

    mask 不访问网络也不读源目录内容，论文参数只用来定位工作目录：本地目录形态取它的
    basename，编号与链接形态解析成编号，两者都作为工作目录名交给驱动器。
    """
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
    table.add_column(overflow="fold")  # 路径与 message 超宽时折行，不截断
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
    """列出走保守默认整块掩码的环境名与它们的成块数；成块数为 0 的不列（未损失覆盖率）。"""
    listed = [
        f"{name}（{decision.blocks} 块）"
        for name, decision in manifest.environments.items()
        if decision.category == BlockCategory.UNKNOWN and decision.blocks > 0
    ]
    return "、".join(listed) if listed else "—"


def _mask_exit_code(manifest: MaskManifest) -> int:
    """mask 状态到退出码的映射；命中跳过时对已存结论取同样的映射。"""
    return _upstream_exit_code(
        ok=manifest.status is MaskStatus.OK,
        pdf_only_chain=(
            manifest.status is MaskStatus.PRECOMPILE_NOT_OK and manifest.fetch_status == FetchStatus.PDF_ONLY
        ),
    )


@app.command()
def validate(
    src: Annotated[Path, typer.Argument(help="原文 chunk 文件")],
    dst: Annotated[Path, typer.Argument(help="译文文件")],
) -> None:
    """四层 validation，逐项报告失败。

    四层（架构 §3 translate 节）：placeholder multiset / control sequence multiset /
    括号与 inline math 计数 / 段落数。三个调用方共用同一份实现：agent 在翻译会话内
    自查、脚本在出口终审、开发者手工排查。
    """
    console.print(f"tongtu validate：占位实现，校验未执行（退出码 {EXIT_STUB}）  src={src}  dst={dst}")
    for layer in ("placeholders", "control_sequences", "braces_and_math", "paragraph_count"):
        console.print(f"  [未执行] {layer}")
    raise typer.Exit(EXIT_STUB)


@app.command()
def doctor() -> None:
    """检查 xelatex / latexmk / latexpand / pdftocairo / epstopdf、中文字体与 OpenCode 密钥，逐项报告缺失。

    退出码只对工具链与字体负责：那一组缺任一项则编译无法进行，退 1。运行期凭证缺失如实
    打印但退 0——参考镜像在构建期跑这个命令自检，而构建镜像的机器不该需要凭证。
    """
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column()
    table.add_column()
    table.add_column(overflow="fold")  # 路径超宽时折行，不截断
    missing_toolchain: list[str] = []
    missing_credentials: list[str] = []
    for name, purpose in DOCTOR_CHECKS:
        if name == FONT_CHECK_NAME:
            found, detail = _check_fonts()
        elif name == KEY_CHECK_NAME:
            found, detail = _check_opencode_key()
        else:
            found, detail = _check_executable(name)
        if not found:
            target = missing_credentials if (name, purpose) in DOCTOR_CREDENTIAL_CHECKS else missing_toolchain
            target.append(name)
        table.add_row(f"  [{'通过' if found else '缺失'}]", f"{name} —— {purpose}", detail)
    console.print(table)
    if missing_toolchain:
        console.print(f"环境有缺失：{'、'.join(missing_toolchain)}")
        raise typer.Exit(EXIT_FAILURE)
    if missing_credentials:
        console.print(f"工具链与字体齐全；{'、'.join(missing_credentials)}未配置，survey 起的阶段无法执行。")
        return
    console.print("环境齐全。")


def _check_executable(name: str) -> tuple[bool, str]:
    """在 PATH 里查可执行文件，返回（是否找到、找到的路径或缺失说明）。"""
    path = shutil.which(name)
    if path is None:
        return False, f"PATH 里找不到 {name}"
    return True, path


def _check_fonts() -> tuple[bool, str]:
    """查字体目录下的中文字体文件，返回（是否齐全、字体目录路径或缺失说明）。"""
    absent = [name for name in REQUIRED_FONT_FILENAMES if not (FONTS_DIR / name).is_file()]
    if absent:
        return False, f"{FONTS_DIR} 下缺 {'、'.join(absent)}"
    return True, str(FONTS_DIR)


def _check_opencode_key() -> tuple[bool, str]:
    """按三级顺序解析 OpenCode 密钥，返回（是否找到、找到的来源或三处的清单）。"""
    resolved = opencode.resolve_api_key()
    if resolved is None:
        return False, (
            f"三处都没有：环境变量 {opencode.API_KEY_ENV}、{config.credentials_path()}、"
            f"{opencode.OPENCODE_AUTH_PATH}（opencode 里 /connect 登录 Go 订阅后产生）"
        )
    source_labels = {
        opencode.KEY_SOURCE_ENV: f"环境变量 {opencode.API_KEY_ENV}",
        opencode.KEY_SOURCE_STORED: str(config.credentials_path()),
        opencode.KEY_SOURCE_OPENCODE_LOGIN: f"本机 opencode 登录态（{opencode.OPENCODE_AUTH_PATH.expanduser()}）",
    }
    return True, source_labels[resolved.source]


@app.command()
def preview(
    paper: PaperArg,
    workdir: WorkdirOpt = None,
    serve: Annotated[
        bool,
        typer.Option("--serve", help="起一个本地 http.server 打开（http 下页面走相对路径读 zh.pdf，大文件加载更快）"),
    ] = False,
) -> None:
    """打开 inspection page（out/report.html，完全静态自包含，双击也能开）。"""
    raise _stub_exit("preview", paper=paper, workdir=workdir, serve=serve)


# ------------------------------------------------------- tex 工具面（不面向人）


@tex_app.command("read")
def tex_read(
    preamble: Annotated[bool, typer.Option("--preamble", help="读 preamble（\\begin{document} 之前）")] = False,
    chunk: Annotated[str | None, typer.Option("--chunk", metavar="ID", help="读该 chunk 在 zh.tex 中的区间")] = None,
    lines: Annotated[str | None, typer.Option("--lines", metavar="A-B", help="读行区间，如 120-180")] = None,
) -> None:
    """读 zh.tex 的指定区域。"""
    if sum([preamble, chunk is not None, lines is not None]) != 1:
        raise typer.BadParameter("--preamble / --chunk / --lines 三者必须恰好给一个")
    if chunk is not None and not _CHUNK_ID_RE.fullmatch(chunk):
        raise typer.BadParameter(f"chunk id 形如 c012，不合法：{chunk}")
    if lines is not None and not re.fullmatch(r"[0-9]+-[0-9]+", lines):
        raise typer.BadParameter(f"--lines 形如 A-B（如 120-180），不合法：{lines}")
    raise _stub_exit("tex read", preamble=preamble, chunk=chunk, lines=lines)


@tex_app.command("patch")
def tex_patch(
    old: Annotated[str, typer.Option("--old", help="被替换的原文文本")],
    new: Annotated[str, typer.Option("--new", help="替换后的文本")],
    chunk: Annotated[
        str | None,
        typer.Option(
            "--chunk",
            metavar="ID",
            help="正文 patch 必须标注所属 chunk（该 chunk 状态记 edited）；不给则为 preamble patch",
        ),
    ] = None,
) -> None:
    """patch zh.tex：preamble 自由，正文须标 --chunk（架构 §3 compile 节分区权限）。"""
    if chunk is not None and not _CHUNK_ID_RE.fullmatch(chunk):
        raise typer.BadParameter(f"chunk id 形如 c012，不合法：{chunk}")
    raise _stub_exit("tex patch", old=old, new=new, chunk=chunk)


@tex_app.command("compile")
def tex_compile() -> None:
    """编译一次，返回错误列表与日志摘要。"""
    raise _stub_exit("tex compile")


@tex_app.command("render")
def tex_render(
    page: Annotated[int, typer.Option("--page", min=1, help="页码（1-based）")],
) -> None:
    """渲染某页为图，供 agent 检查排版。"""
    raise _stub_exit("tex render", page=page)


@tex_app.command("fallback")
def tex_fallback(
    chunk_id: ChunkIdArg,
    paragraph: Annotated[
        int | None,
        typer.Option("--paragraph", min=0, metavar="N", help="只回退该段落（0-based）；不给则整个 chunk 回退"),
    ] = None,
) -> None:
    """该 chunk（或其中一段）回退原文。"""
    if not _CHUNK_ID_RE.fullmatch(chunk_id):
        raise typer.BadParameter(f"chunk id 形如 c012，不合法：{chunk_id}")
    raise _stub_exit("tex fallback", chunk_id=chunk_id, paragraph=paragraph)


@tex_app.command("retranslate")
def tex_retranslate(chunk_id: ChunkIdArg) -> None:
    """重译一次该 chunk（复用翻译介入点⑤）。"""
    if not _CHUNK_ID_RE.fullmatch(chunk_id):
        raise typer.BadParameter(f"chunk id 形如 c012，不合法：{chunk_id}")
    raise _stub_exit("tex retranslate", chunk_id=chunk_id)


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
