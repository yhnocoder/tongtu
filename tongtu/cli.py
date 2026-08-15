"""`tongtu` CLI 命令面（架构 §6）。

命令与参数以架构 §6 为准；`tex` 子命令面（编译修复会话的工具面，不面向人）见架构
§3 compile 节。

当前全部命令为占位实现：只解析并校验参数、说明将要执行的动作，不运行 pipeline，
接线顺序见 docs/BACKLOG.md。run / validate / doctor / `tex compile` 的退出码是机器
判据，占位结果不得被误当成真实结论，故占位命令统一以 ``EXIT_STUB``（3）退出；
`--help` 退 0、用法错误退 2，这两类行为是真实的。`run --json` 输出的事件流经
artifact model 构造，流的形状即接线后的真实形状，内容如实记录「什么都没做」
（阶段 skipped、结果 failed）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .artifacts.events import ResultEvent, StageEndEvent, StageStartEvent
from .stages import STAGES

#: 占位实现的统一退出码。区别于 0（成功）、1（运行失败）与 2（用法错误），
#: 使占位结果在任何脚本化调用里都不可能被读成真实结论。
EXIT_STUB = 3

#: chunk id 形状，与 artifact model（tongtu/artifacts/chunks.py）的 pattern 一致。
_CHUNK_ID_RE = re.compile(r"^c[0-9]{3,}$")

#: doctor 检查项（架构 §6）：名字 → 用途。
DOCTOR_CHECKS: tuple[tuple[str, str], ...] = (
    ("xelatex", "编译引擎（latexmk -xelatex）"),
    ("latexmk", "编译回环驱动"),
    ("latexpand", "flatten 阶段展开多文件源码"),
    ("pdftocairo", "figures 矢量源转位图"),
    ("epstopdf", "EPS 图源接入 xelatex 的转换链"),
    ("中文字体", "font fallback chain（霞鹜文楷随仓库分发）"),
)

#: `tongtu stage` 的阶段名选项，取值即 tongtu.stages.STAGES。
StageName = Enum("StageName", {name: name for name in STAGES}, type=str)

console = Console()

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
    typer.Option("--json", help="向 stdout 输出机器可读事件流（事件类型以 artifact model 定义，架构 §6）"),
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


def _emit_stub_events(paper: str) -> None:
    """按 `--json` 契约向 stdout 输出一条完整事件流（JSON Lines，事件类型见 artifacts/events.py）。"""
    ts = datetime.now(UTC)
    run_id = uuid4().hex[:12]
    arxiv_id = None if ("/" in paper or Path(paper).exists()) else paper
    for name in STAGES:
        start = StageStartEvent(ts=ts, run_id=run_id, arxiv_id=arxiv_id, stage=name)
        end = StageEndEvent(ts=ts, run_id=run_id, arxiv_id=arxiv_id, stage=name, status="skipped", duration_ms=0)
        print(start.model_dump_json(exclude_none=True))
        print(end.model_dump_json(exclude_none=True))
    result = ResultEvent(
        ts=ts,
        run_id=run_id,
        arxiv_id=arxiv_id,
        status="failed",
        exit_code=EXIT_STUB,
        error="占位实现：pipeline 未接入",
    )
    print(result.model_dump_json(exclude_none=True))


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
    if json_output:
        _emit_stub_events(paper)
        raise typer.Exit(EXIT_STUB)
    raise _stub_exit("run", paper=paper, workdir=workdir, glossary=glossary, force=force, agent=agent, model=model)


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
    paper: Annotated[str, typer.Argument(metavar="ID", help="arXiv id（或本地源码目录，供 fetch 用）")],
    workdir: WorkdirOpt = None,
    json_output: JsonOpt = False,
    agent: AgentOpt = None,
    model: ModelOpt = None,
) -> None:
    """单阶段入口，调试用：上游阶段从工作目录装载已有产物，目标阶段无视 manifest 必算。"""
    raise _stub_exit("stage", name=name.value, paper=paper, workdir=workdir, json=json_output, agent=agent, model=model)


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
    """检查 xelatex / latexmk / latexpand / pdftocairo / epstopdf 与中文字体，逐项报告缺失。"""
    console.print(f"tongtu doctor：占位实现，环境检查未执行（退出码 {EXIT_STUB}）")
    for name, purpose in DOCTOR_CHECKS:
        console.print(f"  [未检查] {name} —— {purpose}")
    raise typer.Exit(EXIT_STUB)


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
