"""baseline 阶段：原样编译原文，隔离环境问题（架构 §3 baseline 行、关节②）。

**这是流水线最早、也是唯一为省钱而设的门控**：mask 之后的 survey 与 translate 是全流程
里仅有的 LLM 支出，而「编译不过」的论文在这两步之前就该被拦下——架构 §3 原话是
「编译不过的论文不花一分钱」。拦下的判据必须是原文自己的编译，因为此时还没有任何译文，
失败必然是**环境问题**（缺 `.sty`、图找不到、TeX 发行版差异），不是翻译问题。

流程（迁 v2 `scripts/compile.sh` 的资产链接与 latexmk 用法）：

1. `build/baseline/` 里放 `flat.tex`（flatten 的产物，拷贝而非链接——latexmk 会在旁边落
   一堆 `.aux`/`.log`，`build/` 才是该被弄脏的地方，`src/` 按架构 §5 只读）；
2. 链接 `src/` 下的编译资产（全部子目录 + 全部顶层文件，见 `tongtu.compiler.link_assets`；
   v2 写死 `figures`/`logo`/`tables`/`images` 四个目录名，改叫 `figs` 的论文就整批丢图）；
3. 引擎自动探测（含 xeCJK / fontspec → xelatex，否则 pdflatex，迁 v2 `engine=auto`）；
4. latexmk 回环一次；失败且给了 `session`（关节②，M3 接线）→ 拉起一次修复会话再编一次；
5. 仍失败 → `status="env_failed"`，**终止语义**：编排器据此停在这里，不进 survey。

## 出口判据为什么是「出了 PDF」而不是「零错误」

真实 arXiv 论文里带几个 `!` 错误却照样出 PDF 的比例不低（作者本地能编出来就投了）。
baseline 的职责是隔离**环境**问题，不是给原文的排版质量打分——把这类论文挡在门外
只会误伤。故：出了 PDF 即放行，日志里的错误数记进结果与警告，供 compile 阶段做
「译文不比原文更糟」的相对判据（见 `stages/compile.py`）。

字体不在这里链：原文引用不到仓库的 `fonts/`（那是 inject_cjk 注入之后才出现的相对路径），
baseline 只验证原文自己的环境。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..compiler import (
    DEFAULT_TIMEOUT,
    AssetLinks,
    CompileRunResult,
    Compiler,
    FixupRequest,
    LOG_TAIL,
    SessionFn,
    detect_engine,
    latexmk_compiler,
    link_assets,
)
from ..workdir import Workdir
from .flatten import FLAT_NAME

__all__ = [
    "BASELINE_DIRNAME",
    "BASELINE_TEX",
    "ENV_FAILED",
    "JOINT",
    "MISSING_SOURCE",
    "OK",
    "STATUSES",
    "BaselineResult",
    "baseline",
]

#: build 区里 baseline 的编译目录与主文件名。
BASELINE_DIRNAME = "baseline"
BASELINE_TEX = "flat.tex"

#: 编译日志的归档位置（相对 `logs/`，架构 §5）。
LOG_NAME = "baseline.log"

# 状态常量。
OK = "ok"
ENV_FAILED = "env_failed"  # 终止语义：环境问题，不进 survey / translate
MISSING_SOURCE = "missing_source"  # 没有 build/flat.tex（flatten 没跑或失败）

STATUSES: tuple[str, ...] = (OK, ENV_FAILED, MISSING_SOURCE)

#: 本阶段的 agent 关节（`tongtu.agent.JOINTS` 的 ②）。
JOINT = "build_env"


@dataclass(frozen=True)
class BaselineResult:
    """baseline 的结构化结果。失败一律走状态，不抛栈（同 flatten 的纪律）。"""

    status: str
    pdf: Path | None = None
    tex: Path | None = None
    build_dir: Path | None = None
    engine: str = ""
    passes: int = 0
    """latexmk 被调用的次数（修复会话后的重试计入）。"""

    error_count: int = 0
    first_error: str | None = None
    log_path: Path | None = None
    session_used: int = 0
    """关节②被拉起的次数（0 或 1）——进 report 的干预统计。"""

    assets: AssetLinks = field(default_factory=AssetLinks)
    warnings: tuple[str, ...] = ()
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK

    def to_json(self) -> dict:
        data: dict = {
            "status": self.status,
            "engine": self.engine,
            "passes": self.passes,
            "passed": self.status == OK,
            "error_count": self.error_count,
            "session_used": self.session_used,
        }
        if self.pdf is not None:
            data["pdf"] = self.pdf.name
        if self.first_error:
            data["first_error"] = self.first_error
        if self.log_path is not None:
            data["log_path"] = self.log_path.name
        if self.assets.linked or self.assets.copied:
            data["assets"] = self.assets.to_json()
        if self.warnings:
            data["warnings"] = list(self.warnings)
        if self.message:
            data["message"] = self.message
        return data


#: 日志里连一条 `!` 都没有时的说明（多半是 latexmk 本身没跑起来）。
_NO_ERROR = "（日志里没有 ! 错误，可能是 latexmk 本身没跑起来）"


def _prompt(tex: Path, build_dir: Path, engine: str, result: CompileRunResult) -> str:
    """关节②的提示词。控制流不移交：会话结束后由脚本重新编译裁决。"""
    return (
        "原文（未翻译）在隔离的 build 目录里编译失败，需要你修构建环境。\n\n"
        f"- 主文件：{tex}\n"
        f"- 编译目录：{build_dir}（`src/` 的资产已链接在此）\n"
        f"- 引擎：latexmk -{engine}\n"
        f"- 第一个错误：{result.first_error or _NO_ERROR}\n"
        f"- latexmk 退出码：{result.returncode}\n\n"
        "这一步还没有任何译文，失败必然是环境问题（缺 .cls/.sty/.bst、图找不到、"
        "TeX 发行版差异、字体缺失等）。请在工作目录内把它修到能出 PDF：\n"
        "- 只改工作目录内的文件（`build/` 可随便改，`src/` 尽量只读）；\n"
        "- 不要改动论文正文内容，也不要翻译任何东西；\n"
        "- 缺宏包优先考虑装包或在导言区补最小可行替代；\n"
        "- 改完不用自己下结论——脚本会重新编译一次，编译是唯一的裁决者。\n\n"
        f"日志尾部：\n{result.log_tail}\n"
    )


def baseline(
    workdir: Workdir,
    *,
    compiler: Compiler | None = None,
    session: SessionFn | None = None,
    flat: str | os.PathLike[str] | None = None,
    engine: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> BaselineResult:
    """原样编译 `build/flat.tex`，隔离环境问题。

    :param compiler: 注入的编译器（默认真 latexmk）。测试与「本机没有 TeX」的开发环境
        用假编译器跑通全部分诊逻辑——真 latexmk 只是 `tongtu.compiler` 里的一层薄封装。
    :param session: 关节② 回调。`None`（默认，M3 才接线）时失败即 `env_failed`；给了则
        失败后拉起一次修复会话，**再编一次**——裁决权在这第二次编译，不在会话自述。
    :param flat: 覆盖主文件路径（默认 `build/flat.tex`）。
    :param engine: 覆盖引擎（默认按源码探测）。
    """
    source = Path(flat) if flat is not None else workdir.build / FLAT_NAME
    if not source.is_file():
        return BaselineResult(
            status=MISSING_SOURCE,
            message=f"没有可编译的原文：{source}（先跑 flatten）",
        )

    text = source.read_text(encoding="utf-8", errors="replace")
    chosen = engine or detect_engine(text)

    build_dir = workdir.build / BASELINE_DIRNAME
    build_dir.mkdir(parents=True, exist_ok=True)
    assets = link_assets(
        workdir.src,
        build_dir,
        root=workdir.path,
        skip=frozenset({BASELINE_TEX}),
    )
    tex = build_dir / BASELINE_TEX
    # 拷贝而非链接：latexmk 在 build 目录里落一堆中间文件，主文件也该住在这里。
    shutil.copyfile(source, tex)

    run: Compiler = compiler if compiler is not None else latexmk_compiler(chosen, timeout=timeout)
    warnings = list(assets.warnings)

    result = run(tex, build_dir)
    passes = 1
    session_used = 0

    # 判据是「出了 PDF」而非「零错误」（见模块文档）。
    if not (result.ok or result.has_pdf) and session is not None and not result.missing_tool:
        session(
            FixupRequest(
                joint=JOINT,
                prompt=_prompt(tex, build_dir, chosen, result),
                workdir=workdir,
                build_dir=build_dir,
                tex=tex,
                engine=chosen,
                log=result.log[-LOG_TAIL:],
                first_error=result.first_error,
                attempt=1,
            )
        )
        session_used = 1
        result = run(tex, build_dir)
        passes += 1

    log_path = _archive_log(workdir, result)
    passed = result.ok or result.has_pdf
    if passed and result.error_count:
        warnings.append(
            f"原文编译出了 PDF 但日志里有 {result.error_count} 个 ! 错误"
            f"（第一个：{result.first_error}）——放行，compile 阶段按「不比原文更糟」判译文"
        )

    if not passed:
        message = result.message or (
            f"原文编译失败：{result.first_error or '日志里没有 ! 错误'}"
            "（环境问题，不是翻译问题——流水线到此终止，不产生任何 LLM 支出）"
        )
        return BaselineResult(
            status=ENV_FAILED,
            tex=tex,
            build_dir=build_dir,
            engine=result.engine or chosen,
            passes=passes,
            error_count=result.error_count,
            first_error=result.first_error,
            log_path=log_path,
            session_used=session_used,
            assets=assets,
            warnings=tuple(warnings),
            message=message,
        )

    return BaselineResult(
        status=OK,
        pdf=result.pdf,
        tex=tex,
        build_dir=build_dir,
        engine=result.engine or chosen,
        passes=passes,
        error_count=result.error_count,
        first_error=result.first_error,
        log_path=log_path,
        session_used=session_used,
        assets=assets,
        warnings=tuple(warnings),
    )


def _archive_log(workdir: Workdir, result: CompileRunResult) -> Path | None:
    """把编译日志抄进 `logs/`（架构 §5：日志与会话转录都住那儿）。"""
    if not result.log:
        return None
    try:
        workdir.logs.mkdir(parents=True, exist_ok=True)
        path = workdir.logs / LOG_NAME
        path.write_text(result.log, encoding="utf-8")
        return path
    except OSError:
        return result.log_path
