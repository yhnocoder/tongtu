"""`tongtu` 命令行入口（架构 §6）。

    tongtu run <arxiv-id | dir>  [--glossary FILE]...  [--workdir DIR]  [--force]  [--json]
    tongtu retranslate <id>  (--chunks c012,c045 | --term WORD | --all)
    tongtu stage <name> <id>          # 单阶段入口，调试用
    tongtu doctor                     # 检查 xelatex/latexmk/latexpand/字体，缺啥说啥
    tongtu preview <id>               # 打开检验页

零期状态：`doctor`（M0）、`run` 与 `stage`（M2）、`retranslate`（M3）、`preview`（M4）
全部已实现。
退出码约定：0 = 成功（`doctor` 全部命中 / `run` 产物包完整产出，含有回退块的情形；
`preview` 打不开浏览器但打印了路径也算成功）；1 = 检查未通过或运行失败；2 = 用法错误。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from . import __version__
from .stages import STAGES

#: doctor 探测的可执行文件：名字 → 用途说明。
REQUIRED_TOOLS: tuple[tuple[str, str], ...] = (
    ("xelatex", "编译引擎（中文排版必需）"),
    ("latexmk", "编译回环驱动"),
    ("latexpand", "flatten 阶段展开多文件源码"),
)

#: 中文字体探测链（架构 §10）：Hiragino → Noto Sans CJK → 霞鹜文楷。
FONT_CHAIN: tuple[str, ...] = (
    "Hiragino Sans GB",
    "Noto Sans CJK SC",
    "LXGW WenKai",
)

_NOT_IMPLEMENTED = "零期施工中，见 docs/PHASE0.md 里程碑"

_OK, _MISSING, _UNKNOWN = "ok", "missing", "unknown"

_MARK = {_OK: "[ok]", _MISSING: "[缺失]", _UNKNOWN: "[未知]"}


@dataclass
class Check:
    """一条 doctor 检查结果。"""

    name: str
    status: str  # _OK / _MISSING / _UNKNOWN
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == _OK


# --------------------------------------------------------------------------- doctor


def _probe_tools() -> list[Check]:
    checks = []
    for tool, purpose in REQUIRED_TOOLS:
        path = shutil.which(tool)
        if path:
            checks.append(Check(tool, _OK, path))
        else:
            checks.append(Check(tool, _MISSING, f"未在 PATH 中找到——{purpose}"))
    return checks


def _list_font_families() -> list[str] | None:
    """用 fc-list 列出系统字体族名；fc-list 不可用或调用失败返回 None。"""
    fc_list = shutil.which("fc-list")
    if not fc_list:
        return None
    for argv in ([fc_list, "--format", "%{family}\n"], [fc_list]):
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return proc.stdout.splitlines()
    return None


def _probe_fonts() -> Check:
    families = _list_font_families()
    if families is None:
        return Check(
            "中文字体链",
            _UNKNOWN,
            f"fc-list 不可用，无法探测；请自行确认 {' / '.join(FONT_CHAIN)} 之一可用",
        )
    haystack = "\n".join(families).lower()
    found = [name for name in FONT_CHAIN if name.lower() in haystack]
    if found:
        return Check("中文字体链", _OK, "、".join(found))
    return Check(
        "中文字体链",
        _MISSING,
        f"探测链全部落空（{' → '.join(FONT_CHAIN)}）；装一款中文字体，或用仓库随附的霞鹜文楷",
    )


def _display_width(text: str) -> int:
    """终端显示宽度：CJK 全角字符算两列（对齐 doctor 的输出列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def run_doctor(out=None) -> int:
    """探测本机运行环境。全部命中返回 0，否则返回 1。"""
    stream = sys.stdout if out is None else out
    checks = [*_probe_tools(), _probe_fonts()]

    print(f"tongtu doctor (v{__version__})", file=stream)
    mark_width = max(_display_width(m) for m in _MARK.values())
    name_width = max(_display_width(c.name) for c in checks)
    for check in checks:
        print(
            f"{_pad(_MARK[check.status], mark_width)} {_pad(check.name, name_width)}  {check.detail}",
            file=stream,
        )

    failed = [c for c in checks if not c.passed]
    if not failed:
        print("\n环境就绪。", file=stream)
        return 0
    print(
        "\n未通过：" + "、".join(f"{c.name}（{'无法探测' if c.status == _UNKNOWN else '缺失'}）" for c in failed),
        file=stream,
    )
    print("TeX 环境安装指引见 docs/ARCHITECTURE.md §10（或直接用参考镜像）。", file=stream)
    return 1


# ------------------------------------------------------------------------ argparse


def _not_implemented(command: str) -> int:
    print(f"tongtu {command}：{_NOT_IMPLEMENTED}", file=sys.stderr)
    return 2


# ------------------------------------------------------------------- run / stage


def _agent(args: argparse.Namespace) -> dict:
    """`--agent` / `--model` 透传给 `Pipeline(agent=...)`；没给就让编排器用它的默认（MockAgent）。

    名字解析（显式 → `$TONGTU_AGENT` → mock）在 :func:`tongtu.agent.get_agent` 里，这里
    不重复一套口径。`--model` 同样只是转交：要不要模型、给不给默认，由各运行时自己定
    （codex 要求显式给——模型标识进翻译缓存 key；mock / pseudo 丢弃）。
    """
    from .agent import AGENT_ENV, get_agent

    name = getattr(args, "agent", None) or os.environ.get(AGENT_ENV)
    model = (getattr(args, "model", None) or "").strip()
    if not name and not model:
        return {}
    try:
        return {"agent": get_agent(name, **({"model": model} if model else {}))}
    except RuntimeError as exc:
        # 运行时拒绝被构造（如 codex 没给模型）。对 CLI 而言这与「未知 agent 名」同类：
        # 用法错误，退 2；上层只认 ValueError，故在此换个类型，消息原样带出去。
        raise ValueError(str(exc)) from exc


def run_run(args: argparse.Namespace) -> int:
    """`tongtu run`：跑完整流水线，退出码即 `PipelineResult.exit_code`（架构 §6）。"""
    from .pipeline import run_pipeline
    from .workdir import WorkdirError

    try:
        result = run_pipeline(
            args.target,
            workdir=args.workdir,
            force=args.force,
            json_events=args.json,
            glossary=tuple(args.glossary or ()),
            **_agent(args),
        )
    except WorkdirError as exc:
        print(f"tongtu run：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # 未知 agent 名 = 用法错误
        print(f"tongtu run：{exc}", file=sys.stderr)
        return 2
    return result.exit_code


def run_retranslate(args: argparse.Namespace) -> int:
    """`tongtu retranslate <id>`：块级失效重算（架构 §4 返工触发表）。

    退出码同 `run`（0 = 出包）；块 id 写错、术语没命中任何块这类**用法错误**退 2。
    """
    from .pipeline import retranslate
    from .workdir import WorkdirError

    chunks = tuple(part.strip() for part in (args.chunks or "").split(",") if part.strip())
    if args.chunks is not None and not chunks:
        print("tongtu retranslate：--chunks 要求至少一个块 id（形如 c012,c045）", file=sys.stderr)
        return 2
    try:
        result = retranslate(
            args.id,
            workdir=args.workdir,
            chunks=chunks,
            term=(args.term or "").strip(),
            all_chunks=bool(args.all),
            json_events=args.json,
            glossary=tuple(args.glossary or ()),
            **_agent(args),
        )
    except WorkdirError as exc:
        print(f"tongtu retranslate：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # 未知块 id / 未知 agent 名 = 用法错误
        print(f"tongtu retranslate：{exc}", file=sys.stderr)
        return 2
    return result.exit_code


def run_stage_cmd(args: argparse.Namespace) -> int:
    """`tongtu stage <name> <id>`：单阶段调试入口。

    上游阶段一律**从工作目录装载**（不重算），目标阶段无视 manifest 必算。占位跳过的阶段
    （`SKIPPED_STAGES`，M4 起为空）退 2。
    """
    from .pipeline import SKIPPED_STAGES, run_stage
    from .workdir import WorkdirError

    if args.name in SKIPPED_STAGES:
        print(
            f"tongtu stage {args.name}：{_NOT_IMPLEMENTED}（{SKIPPED_STAGES[args.name]}）",
            file=sys.stderr,
        )
        return 2
    try:
        result = run_stage(
            args.name,
            args.id,
            workdir=args.workdir,
            json_events=args.json,
            **_agent(args),
        )
    except WorkdirError as exc:
        print(f"tongtu stage {args.name}：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # 未知 agent 名 = 用法错误
        print(f"tongtu stage {args.name}：{exc}", file=sys.stderr)
        return 2
    outcome = result.stage(args.name)
    return 0 if outcome is not None and outcome.ok else 1


# ------------------------------------------------------------------------ preview


def run_preview(args: argparse.Namespace, opener=None, server=None) -> int:
    """`tongtu preview <id>`：打开产物包里的静态检验页（架构 §11、PHASE0 §1 第 4 条）。

    退出码语义刻意宽松：**打不开浏览器不算失败**。headless 容器、SSH 会话里
    `webbrowser.open` 必然返回 False，此时打印路径并退 0——用户拿着路径照样能开，
    而把它判成错误只会让脚本化调用平添一个要特判的非零退出码。真正的失败只有一种：
    产物包里没有 `report.html`（还没跑过 `tongtu run`）。

    `--serve` 起一个本地 http.server：`file://` 下 PDF 走内嵌 base64，而 http 下页面会
    走相对路径 fetch 那条快路（省掉 33% 体积的解码），大包用它更跟手。
    """
    import webbrowser

    from .report_page import PAGE_NAME
    from .workdir import WorkdirError, open_workdir

    try:
        paper = open_workdir(arxiv_id=args.id, workdir=args.workdir, create=False)
    except WorkdirError as exc:
        print(f"tongtu preview：{exc}", file=sys.stderr)
        return 2
    page = paper.out / PAGE_NAME
    if not page.is_file():
        print(
            f"tongtu preview：没有 {page}——先跑 `tongtu run {args.id}` 出产物包",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "serve", False):
        return _serve(page, opener=opener, server=server)

    url = page.resolve().as_uri()
    opened = False
    try:
        opened = (opener or webbrowser.open)(url)
    except Exception:  # noqa: BLE001 —— 没有浏览器不是错误
        opened = False
    print(url if opened else f"打不开浏览器，检验页在：{page}")
    return 0


def _serve(page, *, opener=None, server=None) -> int:
    """在产物包目录上起一个本地 http.server，打开检验页（Ctrl-C 退出）。"""
    import functools
    import http.server
    import webbrowser

    directory = str(page.parent)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    factory = server or (lambda: http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler))
    httpd = factory()
    host, port = httpd.server_address[0], httpd.server_address[1]
    url = f"http://{host}:{port}/{page.name}"
    print(f"检验页：{url}（Ctrl-C 退出）")
    try:
        (opener or webbrowser.open)(url)
    except Exception:  # noqa: BLE001
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    # --json 既可放在子命令前也可放在其后。默认值统一为 SUPPRESS：子命令与主 parser 共享
    # 同一个 action 对象，任何一侧设了真实默认值都会把另一侧已解析的 True 覆盖回去。
    # 属性缺省由 parse_args() 补 False。
    global_opts = argparse.ArgumentParser(add_help=False)
    global_opts.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="向 stdout 输出机器可读事件流（schema：docs/schemas/events.schema.json）",
    )

    workdir_opts = argparse.ArgumentParser(add_help=False)
    workdir_opts.add_argument(
        "--workdir",
        metavar="DIR",
        help="论文工作目录（默认 $TONGTU_HOME/<id> 或 ~/.local/share/tongtu/<id>）",
    )

    # agent 运行时（架构 §9/§13）。默认 mock 是有意的：真运行时要花钱、要拉外部进程，
    # 必须显式选择。choices 直接取自适配层的注册表，不在此另抄一份。
    from .agent import AGENT_ENV, DEFAULT_AGENT, agent_names

    agent_opts = argparse.ArgumentParser(add_help=False)
    agent_opts.add_argument(
        "--agent",
        metavar="NAME",
        choices=list(agent_names()),
        default=None,
        help=(f"agent 运行时：{' / '.join(agent_names())}（默认 {DEFAULT_AGENT}；也可用 ${AGENT_ENV}）"),
    )
    # 模型标识进翻译缓存 key（架构 §4），故真运行时要求显式给：换模型必须换缓存，
    # 靠运行时自己的配置文件下发模型会让缓存分不清新旧译文。mock / pseudo 忽略它。
    agent_opts.add_argument(
        "--model",
        metavar="ID",
        default=None,
        help="模型标识，透传给 agent 运行时（--agent codex 必须给；mock / pseudo 忽略）",
    )

    parser = argparse.ArgumentParser(
        prog="tongtu",
        parents=[global_opts],
        description="基于 LaTeX 源码的 arXiv 论文英译中引擎",
    )
    parser.add_argument("--version", action="version", version=f"tongtu {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_run = sub.add_parser(
        "run",
        parents=[global_opts, workdir_opts, agent_opts],
        help="跑完整流水线（幂等：按 manifest 与翻译缓存跳过已完成部分）",
    )
    p_run.add_argument("target", metavar="<arxiv-id | dir>", help="arXiv id 或本地源码目录")
    p_run.add_argument(
        "--glossary",
        metavar="FILE",
        action="append",
        default=[],
        help="输入术语表，可多次；优先级高于论文目录内与全局表",
    )
    p_run.add_argument("--force", action="store_true", help="无视缓存全量重跑")

    p_re = sub.add_parser(
        "retranslate",
        parents=[global_opts, workdir_opts, agent_opts],
        help="块级失效重算（增量重翻）",
        description=(
            "删掉对应的翻译记忆条目，再重算受影响子图：translate 必算（没被失效的块直接"
            "命中缓存），compile 及下游按 manifest 判。上游阶段一律从工作目录装载，不重算。"
        ),
    )
    p_re.add_argument("id", metavar="<id>", help="arXiv id（或本地源码目录名）")
    p_re.add_argument(
        "--glossary",
        metavar="FILE",
        action="append",
        default=[],
        help="输入术语表，可多次；优先级高于论文目录内与全局表",
    )
    scope = p_re.add_mutually_exclusive_group(required=True)
    scope.add_argument("--chunks", metavar="c012,c045", help="指定块 id，逗号分隔")
    scope.add_argument("--term", metavar="WORD", help="重翻命中该术语的块")
    scope.add_argument("--all", action="store_true", help="全量重翻（改文风/换模型时的显式操作）")

    p_stage = sub.add_parser(
        "stage",
        parents=[global_opts, workdir_opts, agent_opts],
        help="单阶段入口，调试用（上游从工作目录装载，目标阶段必算）",
        description=(
            "只跑一个阶段：上游阶段一律从工作目录装载已有产物，不重算；目标阶段无视 "
            "manifest 必算。可单跑的阶段 = 上游产物已在工作目录里的阶段"
            "（flatten / baseline / mask / survey / chunk / translate / compile；"
            "fetch 需要 arXiv id 或本地目录）；figures / export 尚未实现。"
        ),
    )
    p_stage.add_argument("name", metavar="<name>", choices=list(STAGES), help="阶段名：" + " / ".join(STAGES))
    p_stage.add_argument("id", metavar="<id>", help="arXiv id（或本地源码目录，供 fetch 用）")

    sub.add_parser(
        "doctor",
        parents=[global_opts],
        help="检查 xelatex / latexmk / latexpand / 中文字体，缺啥说啥",
    )

    p_preview = sub.add_parser(
        "preview",
        parents=[global_opts, workdir_opts],
        help="打开静态检验页 report.html",
        description=(
            "打开产物包里的 out/report.html（PDF.js 渲染 zh.pdf、anchors 热区可点）。"
            "页面完全静态自包含，双击也能开；headless 环境打不开浏览器时打印路径并退 0。"
        ),
    )
    p_preview.add_argument("id", metavar="<id>", help="arXiv id（或本地源码目录名）")
    p_preview.add_argument(
        "--serve",
        action="store_true",
        help="起一个本地 http.server 打开（http 下页面走相对路径读 zh.pdf，大包更跟手）",
    )

    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行，并把未出现的全局 flag 补成确定值。"""
    args = build_parser().parse_args(argv)
    args.json = getattr(args, "json", False)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    if args.command is None:
        build_parser().print_help()
        return 2
    if args.command == "doctor":
        return run_doctor()
    if args.command == "run":
        return run_run(args)
    if args.command == "retranslate":
        return run_retranslate(args)
    if args.command == "stage":
        return run_stage_cmd(args)
    if args.command == "preview":
        return run_preview(args)
    return _not_implemented(args.command)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
