"""latexmk 薄封装、编译资产与 agent 修复请求（架构 §3 baseline/compile 两行、§10、§13）。

本模块**不是流水线阶段**（故不在 `tongtu/stages/` 下，同 `validate.py`）：baseline 与
compile 两个阶段驱动器共用它——同一套 `Compiler` 接口、同一套资产链接规则、同一套
`.log` 解析、同一个「拉起修复会话」的请求对象。

## 为什么编译要藏在可注入的接口后面

`tongtu run` 的裁决者是编译（架构 §2 原则 1），可编译回环的**逻辑**（失败分诊、块 → 段落
两级二分、回退控制）与 TeX 本身无关：它只需要一个「给我一份 tex，告诉我成没成」的函数。
把 latexmk 封在 :data:`Compiler` 后面，回环逻辑就能在没有 TeX 的机器上用假编译器全量单测
（架构 §12 层 1 的成本纪律），真 latexmk 实现退化成薄薄一层 `subprocess.run`——
它自己只在装了 latexmk 的环境里被测（`skipif`）。

    Compiler = Callable[[Path 主tex, Path build目录], CompileRunResult]

## 资产（迁 v2 `scripts/compile.sh`）

编译要在一个干净的 build 目录里进行（latexmk 的中间文件不该污染 `src/`，`src/` 按架构
§5 是只读区）。于是把 `src/` 里编译需要的东西链进 build 目录：

* v2 写死了四个图目录名（`figures` / `logo` / `tables` / `images`）——真实论文里叫
  `figs` / `plots` / `img` 的比比皆是，漏一个就是「图全丢」。本实现改为**链接 `src/` 下
  全部子目录 + 全部顶层文件**，不猜名字；
* 链接一律走符号链接（省磁盘、`src/` 不变），文件系统不支持时降级为拷贝；
* **越界防线**：`src/` 里的符号链接可能指向工作目录之外，链过去等于让编译读任意路径。
  凡 `resolve()` 后不在工作目录内的条目一律跳过并记警告（同一条纪律见架构 §5：
  `\\input` 与资产限制在 workdir 内）。

字体是唯一的例外：inject_cjk 注入的是相对路径 `Path={fonts/}`，指的是**仓库**的
`fonts/`（霞鹜文楷），故 compile 把它单独链进 build 目录。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Container, Iterable, Protocol

from .texlex import Lexer, TexLexError
from .workdir import Workdir

__all__ = [
    "AssetError",
    "AssetLinks",
    "CompileRunResult",
    "Compiler",
    "DEFAULT_ENGINE",
    "DEFAULT_TIMEOUT",
    "ENGINE_FLAGS",
    "ERROR",
    "FAILED",
    "FONTS_DIRNAME",
    "FONTS_ENV",
    "FixupRequest",
    "LATEXMK",
    "LATEXMK_FLAGS",
    "LogSummary",
    "MISSING_TOOL",
    "OK",
    "RUN_STATUSES",
    "SessionFn",
    "TIMEOUT",
    "detect_engine",
    "find_fonts",
    "latexmk_compiler",
    "link_assets",
    "link_fonts",
    "parse_log",
]


# --------------------------------------------------------------------- 常量

#: latexmk 可执行文件名（可被 `latexmk_compiler(latexmk=...)` 覆盖）。
LATEXMK = "latexmk"

#: 固定 flag（迁 v2 compile.sh）：非交互、出错继续（`-f`，要的是**尽量出 PDF** 与完整
#: 日志，而不是第一个错就停）、安静输出（错误仍进 `.log`）。
LATEXMK_FLAGS: tuple[str, ...] = ("-interaction=nonstopmode", "-f", "-quiet")

#: 引擎名 → latexmk flag。译文恒走 xelatex（架构 §13），baseline 按原文探测。
ENGINE_FLAGS: dict[str, str] = {
    "xelatex": "-xelatex",
    "pdflatex": "-pdf",
    "lualatex": "-lualatex",
}

#: 探测不出线索时的引擎（v2 同）。
FALLBACK_ENGINE = "pdflatex"

#: 中文译文的引擎（= `inject_cjk.ENGINE`，此处不 import stages 以免反向依赖）。
DEFAULT_ENGINE = "xelatex"

#: 单次 latexmk 超时（秒）。大论文三四遍编译也就一两分钟，超时基本等于卡在交互提示上。
DEFAULT_TIMEOUT = 900.0

#: 引擎探测线索：导言区出现其一即判 xelatex（迁 v2 的 `grep 'xeCJK\\|fontspec'` 并扩展）。
XETEX_HINTS: tuple[str, ...] = (
    "xeCJK",
    "fontspec",
    "unicode-math",
    "xltxtra",
    "polyglossia",
    "ctex",
    "XeTeX",
)

# 单次编译的状态。
OK = "ok"
FAILED = "failed"  # latexmk 跑了，但没出 PDF / 退出码非零
MISSING_TOOL = "missing_tool"  # PATH 里没有 latexmk
TIMEOUT = "timeout"
ERROR = "error"  # 调用本身炸了（OSError 等）

RUN_STATUSES: tuple[str, ...] = (OK, FAILED, MISSING_TOOL, TIMEOUT, ERROR)

#: 仓库字体目录名与它的环境变量覆盖。
FONTS_DIRNAME = "fonts"
FONTS_ENV = "TONGTU_FONTS"

#: 字体目录的判据文件——inject_cjk 的注入块按名字引用它们。
FONT_FILES: tuple[str, ...] = ("LXGWWenKai-Light.ttf", "LXGWWenKai-Medium.ttf")

#: 日志里一条错误的起头（v2 用 `grep '^!'`，此处等价）。
ERROR_RE = re.compile(r"^!.*$", re.MULTILINE)

#: 错误上下文行号 `l.123`。TeX 在报错后紧接着打出出错的行。
LINE_RE = re.compile(r"^l\.(\d+)", re.MULTILINE)

#: 日志摘要里每条错误的截断长度——报告要的是「第一个 `!` 错误」，不是整本日志。
_ERROR_MAX = 400

#: 日志尾巴长度（诊断消息与喂给修复会话的上下文）。
LOG_TAIL = 4000


class AssetError(RuntimeError):
    """编译资产不可用（最典型：wheel 安装态找不到仓库 `fonts/`）。

    结构化到 `kind` + `detail`：驱动器据此决定是记警告继续，还是当环境问题终止。
    """

    def __init__(self, message: str, *, kind: str = "asset", detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail

    def to_json(self) -> dict:
        return {"kind": self.kind, "message": str(self), "detail": self.detail}


# ----------------------------------------------------------------- 日志解析


@dataclass(frozen=True)
class LogSummary:
    """`.log` 摘要（迁 v2 compile.sh 末尾的 `grep -c '^!'` / `grep '^!' | uniq -c`）。"""

    errors: tuple[str, ...] = ()
    error_count: int = 0
    error_line: int | None = None
    """第一个错误之后出现的 `l.<N>` 行号——compile 用它判断错误是否落在前导区。"""

    @property
    def first_error(self) -> str | None:
        return self.errors[0] if self.errors else None

    @property
    def top_errors(self) -> tuple[tuple[str, int], ...]:
        """按出现次数排序的错误种类（v2 的 `sort | uniq -c | sort -rn | head`）。"""
        counts: dict[str, int] = {}
        for error in self.errors:
            counts[error] = counts.get(error, 0) + 1
        return tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def to_json(self) -> dict:
        data: dict = {"error_count": self.error_count}
        if self.first_error:
            data["first_error"] = self.first_error
        if self.error_line is not None:
            data["error_line"] = self.error_line
        if len(self.errors) > 1:
            data["errors"] = [error for error, _ in self.top_errors[:10]]
        return data


def parse_log(text: str) -> LogSummary:
    """从 latexmk / TeX 日志里抽出错误清单与第一个错误的行号。"""
    if not text:
        return LogSummary()
    errors = [line.strip()[:_ERROR_MAX] for line in ERROR_RE.findall(text)]
    error_line: int | None = None
    match = ERROR_RE.search(text)
    if match is not None:
        line_match = LINE_RE.search(text, match.end())
        if line_match is not None:
            error_line = int(line_match.group(1))
    return LogSummary(
        errors=tuple(errors),
        error_count=len(errors),
        error_line=error_line,
    )


# ------------------------------------------------------------------- 编译接口


@dataclass(frozen=True)
class CompileRunResult:
    """一次编译的结果。**成败只看 `ok`**——agent 的自述、latexmk 的退出码都不是裁决者，
    有没有 PDF 才是（`-f` 之下出了 PDF 的「失败」照样能用，v2 亦然）。
    """

    ok: bool
    pdf: Path | None = None
    log: str = ""
    log_path: Path | None = None
    returncode: int | None = None
    engine: str = ""
    status: str = ""
    message: str = ""
    command: tuple[str, ...] = ()

    @property
    def summary(self) -> LogSummary:
        return parse_log(self.log)

    @property
    def first_error(self) -> str | None:
        return self.summary.first_error

    @property
    def error_count(self) -> int:
        return self.summary.error_count

    @property
    def has_pdf(self) -> bool:
        """出了 PDF 没有——比 `ok` 弱的判据，用于「不比原文更糟」这类相对判定。"""
        return self.pdf is not None

    @property
    def missing_tool(self) -> bool:
        return self.status == MISSING_TOOL

    @property
    def log_tail(self) -> str:
        return self.log[-LOG_TAIL:] if self.log else ""

    def to_json(self) -> dict:
        data: dict = {"ok": self.ok, "engine": self.engine}
        if self.status:
            data["status"] = self.status
        if self.returncode is not None:
            data["returncode"] = self.returncode
        if self.pdf is not None:
            data["pdf"] = self.pdf.name
        data.update(self.summary.to_json())
        if self.message:
            data["message"] = self.message
        return data


class Compiler(Protocol):
    """`(主 tex, build 目录) -> CompileRunResult`。

    实现只需保证：在 `build_dir` 里编译 `tex`，返回结果里的 `ok` 与 `pdf` 说实话。
    测试里的假编译器、真 latexmk、将来的远端编译服务都是同一个形状。
    """

    def __call__(self, tex: Path, build_dir: Path) -> CompileRunResult: ...


#: 关节 ②/⑥ 的回调形状。M3 的适配层把它接到 `agent.session(prompt, workdir, model, budget)`；
#: 返回值**不作裁决依据**（架构 §9），驱动器只认调用之后的重新编译。
SessionFn = Callable[["FixupRequest"], object]


@dataclass(frozen=True)
class FixupRequest:
    """拉起一次有界修复会话所需的全部上下文（关节 ② 构建环境 / ⑥ 适配与修复）。

    `prompt` 是现成的一段话，`workdir` 划定可写范围，其余字段供适配层组装更细的提示词
    或直接落 `logs/`。驱动器在调用之后**一定**会重新编译——这是唯一的裁决。
    """

    joint: str
    prompt: str
    workdir: Workdir
    build_dir: Path
    tex: Path
    engine: str = ""
    log: str = ""
    first_error: str | None = None
    attempt: int = 1

    def to_json(self) -> dict:
        return {
            "joint": self.joint,
            "build_dir": str(self.build_dir),
            "tex": str(self.tex),
            "engine": self.engine,
            "first_error": self.first_error,
            "attempt": self.attempt,
        }


# ------------------------------------------------------------------- 引擎探测


@lru_cache(maxsize=1)
def _verbatim_envs() -> frozenset[str]:
    """mask 分类表里的 verbatim 环境名——体内的 `%` 与命令都只是字符。"""
    try:
        from .stages.mask import load_environment_table

        return load_environment_table().verbatim_envs
    except Exception:  # 分类表损坏不该拖垮编译：退化为不识别 verbatim
        return frozenset()


def _without_comments(src: str) -> str:
    """剥掉注释（保留其余字节）。引擎探测只做子串搜索，不需要更精细的解析。"""
    try:
        lexer = Lexer(src, verbatim_envs=_verbatim_envs())
        out: list[str] = []
        pos = 0
        for tok in lexer:
            out.append(src[pos : tok.start])
            if tok.kind != "comment":
                out.append(src[tok.start : tok.end])
            pos = tok.end
        out.append(src[pos:])
        return "".join(out)
    except TexLexError:
        return src


def detect_engine(src: str) -> str:
    """按源码内容探测引擎：见到 xeCJK / fontspec 等即 xelatex，否则 pdflatex。

    迁自 v2 compile.sh 的 `grep -q 'xeCJK\\|fontspec'`，修掉两处：命中**注释里**
    （`% \\usepackage{fontspec}` 在 arXiv 源码里极常见）与 verbatim 里展示的同名代码。
    """
    text = _without_comments(src)
    return DEFAULT_ENGINE if any(hint in text for hint in XETEX_HINTS) else FALLBACK_ENGINE


# --------------------------------------------------------------------- 资产


@dataclass(frozen=True)
class AssetLinks:
    """一次资产链接的结果（进 report 与日志，便于事后回答「图为什么没进 PDF」）。"""

    linked: tuple[str, ...] = ()
    copied: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.linked) + len(self.copied)

    def to_json(self) -> dict:
        data: dict = {"linked": list(self.linked)}
        if self.copied:
            data["copied"] = list(self.copied)
        if self.skipped:
            data["skipped"] = list(self.skipped)
        if self.warnings:
            data["warnings"] = list(self.warnings)
        return data


def _place(entry: Path, dest: Path) -> str:
    """把 `entry` 放到 `dest`：优先符号链接，文件系统不支持时拷贝。返回 "link"/"copy"。"""
    if dest.is_symlink() or dest.exists():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    try:
        dest.symlink_to(entry, target_is_directory=entry.is_dir())
        return "link"
    except (OSError, NotImplementedError):
        if entry.is_dir():
            shutil.copytree(entry, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, dest)
        return "copy"


def link_assets(
    src_dir: Path,
    build_dir: Path,
    *,
    root: Path | None = None,
    skip: Container[str] = frozenset(),
) -> AssetLinks:
    """把 `src_dir` 下的编译资产链进 `build_dir`（全部子目录 + 全部顶层文件）。

    :param root: 越界防线的根，通常是工作目录。给出时，`resolve()` 后不在 `root` 内的
        条目一律跳过并记警告（`src/` 里的符号链接可能指向任意路径）。
    :param skip: 不链的文件名（驱动器自己要写进 build 目录的那些，如 `zh.tex`）。
    """
    linked: list[str] = []
    copied: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []

    if not src_dir.is_dir():
        return AssetLinks(warnings=(f"源码目录不存在，未链接任何资产：{src_dir}",))

    build_dir.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve() if root is not None else None
    for entry in sorted(src_dir.iterdir(), key=lambda p: p.name):
        name = entry.name
        if name.startswith(".") or name == "__MACOSX" or name in skip:
            skipped.append(name)
            continue
        try:
            target = entry.resolve()
        except OSError as exc:  # 断链等
            warnings.append(f"资产 {name} 无法解析（{exc}），跳过")
            skipped.append(name)
            continue
        if root_resolved is not None and not (
            target == root_resolved or target.is_relative_to(root_resolved)
        ):
            warnings.append(f"资产 {name} 指向工作目录之外（{target}），跳过")
            skipped.append(name)
            continue
        try:
            how = _place(entry, build_dir / name)
        except OSError as exc:
            warnings.append(f"资产 {name} 无法放入 build 目录（{exc}），跳过")
            skipped.append(name)
            continue
        (linked if how == "link" else copied).append(name)

    return AssetLinks(
        linked=tuple(linked),
        copied=tuple(copied),
        skipped=tuple(skipped),
        warnings=tuple(warnings),
    )


def _fonts_ok(path: Path) -> bool:
    return path.is_dir() and any((path / name).is_file() for name in FONT_FILES)


def find_fonts(fonts: str | os.PathLike[str] | None = None) -> Path:
    """定位随仓库分发的 `fonts/`（霞鹜文楷）。

    解析顺序：显式参数 → `$TONGTU_FONTS` → **相对本包文件逐级向上找**（源码树 / editable
    安装态：`tongtu/compiler.py` → 仓库根 `fonts/`）。

    找不到抛 :class:`AssetError`——**已知缺口**：wheel 安装态里仓库根不存在，字体没被打进
    包，此路必然走空。零期不解决打包（见报告的「口径外决定」），但错误必须结构化：调用方
    据此记警告并继续（编译才是裁决者），而不是抛出一个看不懂的 FileNotFoundError。
    """
    if fonts is not None:
        path = Path(fonts).expanduser()
        if _fonts_ok(path):
            return path.absolute()
        raise AssetError(
            f"指定的字体目录不可用：{path}（需含 {FONT_FILES[0]}）",
            kind="missing_fonts",
            detail=str(path),
        )

    env = (os.environ.get(FONTS_ENV) or "").strip()
    if env:
        path = Path(env).expanduser()
        if _fonts_ok(path):
            return path.absolute()
        raise AssetError(
            f"${FONTS_ENV} 指向的字体目录不可用：{path}",
            kind="missing_fonts",
            detail=str(path),
        )

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / FONTS_DIRNAME
        if _fonts_ok(candidate):
            return candidate
    raise AssetError(
        "找不到仓库 fonts/（霞鹜文楷）——inject_cjk 注入的是相对路径 Path={fonts/}，"
        "缺它则中文全是豆腐。源码树 / editable 安装应能自动找到；wheel 安装态请用 "
        f"${FONTS_ENV} 或 compile_zh(fonts=...) 显式指定",
        kind="missing_fonts",
        detail=str(here.parent),
    )


def link_fonts(build_dir: Path, fonts: str | os.PathLike[str] | None = None) -> Path:
    """把仓库 `fonts/` 链进 build 目录（inject_cjk 用相对路径 `Path={fonts/}`）。

    与 `src/` 资产的越界防线不同，这一条是**故意**指向仓库内的路径，故单独一个函数。
    找不到字体时把 :class:`AssetError` 原样抛出，由驱动器决定降级方式。
    """
    source = find_fonts(fonts)
    build_dir.mkdir(parents=True, exist_ok=True)
    _place(source, build_dir / FONTS_DIRNAME)
    return build_dir / FONTS_DIRNAME


# ---------------------------------------------------------------- latexmk 封装


def latexmk_compiler(
    engine: str | None = None,
    *,
    latexmk: str = LATEXMK,
    timeout: float = DEFAULT_TIMEOUT,
    strict: bool = True,
    extra_args: Iterable[str] = (),
) -> Compiler:
    """返回一个真正调 latexmk 的 :data:`Compiler`（薄封装，逻辑全在调用方）。

    :param engine: `xelatex` / `pdflatex` / `lualatex`；`None` 表示按 tex 内容探测
        （:func:`detect_engine`，v2 compile.sh 的 `engine=auto`）。
    :param strict: `ok` 的判据。默认 `有 PDF 且退出码为 0`——`-f` 之下退出码非零等价于
        「日志里有 `!` 错误」，而编译回环的意义正是把这种错误定位到段落。置 False 则只看
        有没有 PDF（v2 语义），给「原文本身就带错误」的论文留后门；驱动器另有相对判据
        （见 compile 阶段的「不比原文更糟」）。
    :param extra_args: 追加 flag（调试用，如 `-pv`）。

    命令固定为 ``latexmk <engine flag> -interaction=nonstopmode -f -quiet <tex>``，
    在 `build_dir` 里执行，日志取同名 `.log`。
    """

    def run(tex: Path, build_dir: Path) -> CompileRunResult:
        tex = Path(tex)
        build_dir = Path(build_dir)
        chosen = engine
        if chosen is None:
            try:
                chosen = detect_engine(tex.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                chosen = DEFAULT_ENGINE
        flag = ENGINE_FLAGS.get(chosen)
        if flag is None:
            raise ValueError(f"未知引擎：{chosen!r}（可选 {sorted(ENGINE_FLAGS)}）")

        pdf = build_dir / f"{tex.stem}.pdf"
        log_path = build_dir / f"{tex.stem}.log"
        # 上一轮的产物不能冒充这一轮的成功——先清掉再编。
        for stale in (pdf, log_path):
            if stale.exists():
                stale.unlink()

        executable = shutil.which(latexmk)
        if executable is None:
            return CompileRunResult(
                ok=False,
                engine=chosen,
                status=MISSING_TOOL,
                message=(
                    f"PATH 中没有 {latexmk}——编译阶段需要它；跑 `tongtu doctor` 看缺什么，"
                    "或用参考镜像（架构 §10）"
                ),
            )

        command = (executable, flag, *LATEXMK_FLAGS, *extra_args, tex.name)
        try:
            proc = subprocess.run(
                command,
                cwd=build_dir,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            returncode: int | None = proc.returncode
            status = OK
            message = ""
        except subprocess.TimeoutExpired:
            returncode, status = None, TIMEOUT
            message = f"latexmk 超时（{timeout:.0f}s）"
        except (OSError, subprocess.SubprocessError) as exc:
            returncode, status = None, ERROR
            message = f"latexmk 调用失败（{type(exc).__name__}）：{exc}"

        log = ""
        if log_path.is_file():
            log = log_path.read_text(encoding="utf-8", errors="replace")
        ok = pdf.is_file() and (returncode == 0 or not strict)
        if status == OK and not ok:
            status = FAILED
        return CompileRunResult(
            ok=ok,
            pdf=pdf if pdf.is_file() else None,
            log=log,
            log_path=log_path if log_path.is_file() else None,
            returncode=returncode,
            engine=chosen,
            status=status,
            message=message,
            command=command,
        )

    return run
