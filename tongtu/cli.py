"""`tongtu` 命令行入口（架构 §6）。

    tongtu run <arxiv-id | dir>  [--glossary FILE]...  [--workdir DIR]  [--force]  [--json]
    tongtu retranslate <id>  (--chunks c012,c045 | --term WORD | --all)
    tongtu stage <name> <id>          # 单阶段入口，调试用
    tongtu doctor                     # 检查 xelatex/latexmk/latexpand/字体，缺啥说啥
    tongtu preview <id>               # 打开检验页

零期状态：`doctor` 已实现；其余子命令建好签名骨架，执行时退出 2 并指向 docs/PHASE0.md。
退出码约定：0 = 成功（`doctor` 全部命中 / `run` 产物包完整产出）；
1 = 检查未通过或运行失败；2 = 用法错误或功能尚未实现。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from typing import Sequence

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
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=20, check=False
            )
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
            "fc-list 不可用，无法探测；请自行确认 "
            f"{' / '.join(FONT_CHAIN)} 之一可用",
        )
    haystack = "\n".join(families).lower()
    found = [name for name in FONT_CHAIN if name.lower() in haystack]
    if found:
        return Check("中文字体链", _OK, "、".join(found))
    return Check(
        "中文字体链",
        _MISSING,
        f"探测链全部落空（{' → '.join(FONT_CHAIN)}）；"
        "装一款中文字体，或用仓库随附的霞鹜文楷",
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
        "\n未通过："
        + "、".join(f"{c.name}（{'无法探测' if c.status == _UNKNOWN else '缺失'}）" for c in failed),
        file=stream,
    )
    print("TeX 环境安装指引见 docs/ARCHITECTURE.md §10（或直接用参考镜像）。", file=stream)
    return 1


# ------------------------------------------------------------------------ argparse


def _not_implemented(command: str) -> int:
    print(f"tongtu {command}：{_NOT_IMPLEMENTED}", file=sys.stderr)
    return 2


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

    parser = argparse.ArgumentParser(
        prog="tongtu",
        parents=[global_opts],
        description="基于 LaTeX 源码的 arXiv 论文英译中引擎",
    )
    parser.add_argument("--version", action="version", version=f"tongtu {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_run = sub.add_parser(
        "run",
        parents=[global_opts, workdir_opts],
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
        parents=[global_opts, workdir_opts],
        help="块级失效重算（增量重翻）",
    )
    p_re.add_argument("id", metavar="<id>", help="arXiv id")
    scope = p_re.add_mutually_exclusive_group(required=True)
    scope.add_argument("--chunks", metavar="c012,c045", help="指定块 id，逗号分隔")
    scope.add_argument("--term", metavar="WORD", help="重翻命中该术语的块")
    scope.add_argument("--all", action="store_true", help="全量重翻（改文风/换模型时的显式操作）")

    p_stage = sub.add_parser(
        "stage",
        parents=[global_opts, workdir_opts],
        help="单阶段入口，调试用",
    )
    p_stage.add_argument("name", metavar="<name>", choices=list(STAGES), help="阶段名：" + " / ".join(STAGES))
    p_stage.add_argument("id", metavar="<id>", help="arXiv id")

    sub.add_parser(
        "doctor",
        parents=[global_opts],
        help="检查 xelatex / latexmk / latexpand / 中文字体，缺啥说啥",
    )

    p_preview = sub.add_parser(
        "preview",
        parents=[global_opts, workdir_opts],
        help="打开静态检验页 report.html",
    )
    p_preview.add_argument("id", metavar="<id>", help="arXiv id")

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
    return _not_implemented(args.command)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
