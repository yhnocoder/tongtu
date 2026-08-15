"""flatten 阶段：主文件识别 + latexpand 展开（架构 §3 flatten 行、关节①）。

出口判据是机械的：单文件 `build/flat.tex`。本模块做两件事：

1. **主文件识别**（`find_main_tex`）：候选 = `src/` 下含 `\\documentclass` 的 `.tex`。
   恰一个直接用；多个则启发式打分；分数并列时是**真歧义**，走关节①（`arbiter` 回调，
   M3 接 agent）。没有 arbiter 就取分最高者并把 `ambiguous=True` 记进结果供 report——
   猜错的代价是 baseline 编译当场失败（架构 §3：编译门控在 flatten 之后），不会闷声出错。
2. **展开**（`flatten`）：调 latexpand 把 `\\input`/`\\include` 与自定义命令文件展平。

## 为什么不能用 `"\\documentclass" in text`

v2 就是这么找主文件的（`scripts/fetch.py` 第 48 行），于是 `% \\documentclass{article}`
这种被注释掉的行、以及 lstlisting 里演示模板的那一行，都会把一个非主文件选成主文件。
这里改用 `tongtu.texlex` 的词法扫描：注释、`\\verb`、verbatim 环境体内的
`\\documentclass` 一律不算数（同一条纪律见架构 §3.1 第 1 条）。

## 打分启发式

| 特征 | 分 | 依据 |
|---|---|---|
| 含 `\\begin{document}` | +100 | 主文件的定义性特征；只有 `\\documentclass` 的多半是被 `\\input` 的片段或模板残骸 |
| 文件名 main / ms / paper / … | +45…+20 | arXiv 投稿的社区习惯，且逐名递减可打破并列 |
| 直接位于 `src/` 顶层 | +10 | 主文件极少埋在子目录里 |
| 被别的 `.tex` `\\input`/`\\include` | −80 | 被别人包含的文件不是主文件（v2 完全没看这条） |

## latexpand 的两个参数

* `--keep-comments`：注释必须留着。M1 的 mask 把注释当**块**无损处理（`unmask(mask(x))
  == x` 逐字节恒等），删注释反而让原文与产物对不齐；v2 时代二者不兼容的顾虑已消解。
* `--expand-bbl`：arXiv 源码常只给预编译 `.bbl` 而无 `.bib`（v2 经验），不内联进
  `flat.tex` 的话编译时 bibtex 会失败、参考文献整段丢失。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..texlex import Lexer, TexLexError, find_balanced
from ..workdir import Workdir

__all__ = [
    "STATUSES",
    "MainCandidate",
    "MainQuery",
    "MainArbiter",
    "MainTexResult",
    "FlattenResult",
    "find_main_tex",
    "flatten",
]

#: 展平产物（相对 `build/`）。
FLAT_NAME = "flat.tex"

#: latexpand 可执行文件名（可被 `flatten(..., latexpand=...)` 覆盖）。
LATEXPAND = "latexpand"

#: latexpand 调用超时（秒）。大型论文展开也就几秒，超时基本等于卡死。
DEFAULT_TIMEOUT = 300.0

# 状态常量。
OK = "ok"
NO_MAIN = "no_main"  # find_main_tex：没有候选
MISSING_MAIN = "missing_main"  # flatten：给的主文件不存在
MISSING_TOOL = "missing_tool"  # PATH 里没有 latexpand
FAILED = "failed"  # latexpand 非零退出 / 调用异常
EMPTY = "empty"  # latexpand 成功但输出为空

STATUSES: tuple[str, ...] = (OK, NO_MAIN, MISSING_MAIN, MISSING_TOOL, FAILED, EMPTY)

#: 打分权重（见模块文档表）。
SCORE_DOCUMENT_ENV = 100
SCORE_TOPLEVEL = 10
PENALTY_INCLUDED = -80

#: 文件名习惯分，逐名递减——顺带让「main.tex vs paper.tex」不构成并列。
NAME_SCORES: dict[str, int] = {
    "main": 45,
    "ms": 40,
    "paper": 35,
    "root": 30,
    "manuscript": 25,
    "article": 20,
}

#: 包含命令：`\input{x}` / `\include{x}` / `\subfile{x}`。
_INCLUDE_COMMANDS = frozenset({"input", "include", "subfile", "subfileinclude"})

_STDERR_TAIL = 800


@lru_cache(maxsize=1)
def _verbatim_envs() -> frozenset[str]:
    """分类表里 verbatim 类环境名——它们的体内不参与词法（`\\documentclass` 只是字符）。"""
    try:
        from .mask import load_environment_table

        return load_environment_table().verbatim_envs
    except Exception:  # 分类表损坏不该拖垮 flatten：退化为不识别 verbatim
        return frozenset()


# --------------------------------------------------------------------- 主文件识别


@dataclass(frozen=True)
class MainCandidate:
    """一个候选主文件及其打分明细（明细进 report，便于事后判断启发式好不好使）。"""

    path: Path
    relpath: str
    score: int
    has_document: bool = False
    included_by: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "path": self.relpath,
            "score": self.score,
            "has_document": self.has_document,
            "included_by": list(self.included_by),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class MainQuery:
    """交给关节①（`arbiter` 回调）的一次提问：并列的候选摆在这里，请判一个主文件。"""

    root: Path
    candidates: tuple[MainCandidate, ...]
    tied: tuple[MainCandidate, ...]  # 并列在最高分上的那几个


#: 关节①：主文件歧义的外部裁决回调。返回选中的路径，或 None 表示「不知道」。
MainArbiter = Callable[[MainQuery], "str | os.PathLike[str] | None"]


@dataclass(frozen=True)
class MainTexResult:
    """主文件识别结果。`ambiguous` 为真时 `main` 仍然给出——猜一个再让编译裁决。"""

    status: str
    main: Path | None = None
    candidates: tuple[MainCandidate, ...] = ()
    ambiguous: bool = False
    arbitrated: bool = False
    commented_out: tuple[str, ...] = ()  # 只在注释 / verbatim 里出现 \documentclass 的文件
    warnings: tuple[str, ...] = ()
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK

    def to_json(self) -> dict:
        data: dict = {
            "status": self.status,
            "main": None if self.main is None else self.main.name,
            "ambiguous": self.ambiguous,
            "arbitrated": self.arbitrated,
            "candidates": [c.to_json() for c in self.candidates],
        }
        if self.commented_out:
            data["commented_out"] = list(self.commented_out)
        if self.warnings:
            data["warnings"] = list(self.warnings)
        if self.message:
            data["message"] = self.message
        return data


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _tex_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".tex"),
        key=lambda p: p.relative_to(root).as_posix(),
    )


def _scan(text: str) -> tuple[bool, bool, list[str]]:
    """词法扫一遍，返回 (有 `\\documentclass`, 有 `\\begin{document}`, 被包含的文件名)。

    注释、`\\verb`、verbatim 体内的东西不算数——`Lexer` 已经把它们整段吞掉了。
    """
    has_class = False
    has_document = False
    includes: list[str] = []
    lexer = Lexer(text, verbatim_envs=_verbatim_envs())
    for token in lexer:
        if token.kind == "begin" and token.name == "document":
            has_document = True
            continue
        if token.kind != "control":
            continue
        name = text[token.start : token.end][1:]
        if name == "documentclass":
            has_class = True
            continue
        if name in _INCLUDE_COMMANDS:
            target = _include_target(text, token.end)
            if target:
                includes.append(target)
    return has_class, has_document, includes


def _include_target(text: str, pos: int) -> str | None:
    """读 `\\input` 之后的参数：`{file}` 或 TeX 的裸写法 `\\input file.tex`。"""
    i = pos
    while i < len(text) and text[i] in " \t":
        i += 1
    if i < len(text) and text[i] == "{":
        try:
            close = find_balanced(text, i)
        except TexLexError:
            return None
        return text[i + 1 : close].strip() or None
    end = i
    while end < len(text) and not text[end].isspace() and text[end] not in "{}%\\":
        end += 1
    return text[i:end].strip() or None


def _resolve_include(root: Path, referrer: Path, target: str) -> Path | None:
    """`\\input` 的参数 → 实际文件（相对引用文件所在目录，或相对 `src/`；可省 `.tex`）。"""
    raw = target.replace("\\", "/").strip('"')
    names = [raw] if raw.lower().endswith(".tex") else [raw + ".tex", raw]
    for base in (referrer.parent, root):
        for name in names:
            candidate = base / name
            if candidate.is_file():
                return candidate
    return None


def _score(root: Path, path: Path, has_document: bool, included_by: list[str]) -> MainCandidate:
    relpath = path.relative_to(root).as_posix()
    score = 0
    reasons: list[str] = []
    if has_document:
        score += SCORE_DOCUMENT_ENV
        reasons.append(f"含 \\begin{{document}} (+{SCORE_DOCUMENT_ENV})")
    name_bonus = NAME_SCORES.get(path.stem.lower())
    if name_bonus:
        score += name_bonus
        reasons.append(f"文件名 {path.stem} (+{name_bonus})")
    if path.parent == root:
        score += SCORE_TOPLEVEL
        reasons.append(f"位于 src/ 顶层 (+{SCORE_TOPLEVEL})")
    if included_by:
        score += PENALTY_INCLUDED
        reasons.append(f"被 {', '.join(sorted(included_by)[:3])} 包含 ({PENALTY_INCLUDED})")
    return MainCandidate(
        path=path,
        relpath=relpath,
        score=score,
        has_document=has_document,
        included_by=tuple(sorted(included_by)),
        reasons=tuple(reasons),
    )


def find_main_tex(
    source: Workdir | str | os.PathLike[str],
    *,
    arbiter: MainArbiter | None = None,
) -> MainTexResult:
    """在源码树里判定主文件。`source` 可以是 `Workdir`（取其 `src/`）或目录本身。

    唯一候选直接采用；多候选按启发式打分（见模块文档）；**最高分并列即真歧义**，
    有 `arbiter`（关节①）就问它，没有则取排序后的第一个并置 `ambiguous=True`。
    """
    root = Path(source.src if isinstance(source, Workdir) else source)
    if not root.is_dir():
        return MainTexResult(status=NO_MAIN, message=f"源码目录不存在：{root}")

    files = _tex_files(root)
    texts: dict[Path, str] = {path: _read(path) for path in files}
    scans: dict[Path, tuple[bool, bool, list[str]]] = {path: _scan(text) for path, text in texts.items()}

    # 谁被谁 \input：全树扫一遍（片段文件也可能 \input 别的片段）
    included: dict[Path, list[str]] = {}
    for path, (_, _, includes) in scans.items():
        for target in includes:
            resolved = _resolve_include(root, path, target)
            if resolved is None:
                continue
            try:
                key = resolved.resolve().relative_to(root.resolve())
            except ValueError:
                continue  # \input 指到源码树外面：不参与打分
            included.setdefault(root / key, []).append(path.relative_to(root).as_posix())

    candidates: list[MainCandidate] = []
    commented: list[str] = []
    for path in files:
        has_class, has_document, _ = scans[path]
        if has_class:
            candidates.append(_score(root, path, has_document, included.get(path, [])))
        elif "\\documentclass" in texts[path]:
            # 源码里出现过这几个字，但词法上只在注释 / verbatim 里——v2 会选中它
            commented.append(path.relative_to(root).as_posix())

    if not candidates:
        detail = (
            f"（{len(commented)} 个文件里的 \\documentclass 全在注释或 verbatim 中：{commented[:3]}）"
            if commented
            else ""
        )
        return MainTexResult(
            status=NO_MAIN,
            commented_out=tuple(commented),
            message=f"未找到含 \\documentclass 的主文件{detail}",
        )

    ranked = tuple(sorted(candidates, key=lambda c: (-c.score, c.relpath)))
    if len(ranked) == 1:
        return MainTexResult(
            status=OK,
            main=ranked[0].path,
            candidates=ranked,
            commented_out=tuple(commented),
        )

    top = ranked[0].score
    tied = tuple(c for c in ranked if c.score == top)
    if len(tied) == 1:
        return MainTexResult(
            status=OK,
            main=ranked[0].path,
            candidates=ranked,
            commented_out=tuple(commented),
        )

    # 关节①：真歧义
    warnings: list[str] = []
    if arbiter is not None:
        answer = arbiter(MainQuery(root=root, candidates=ranked, tied=tied))
        chosen = _match_answer(root, ranked, answer)
        if chosen is not None:
            return MainTexResult(
                status=OK,
                main=chosen.path,
                candidates=ranked,
                arbitrated=True,
                commented_out=tuple(commented),
                message=f"主文件歧义由关节①判定为 {chosen.relpath}",
            )
        if answer is not None:
            warnings.append(f"关节①给的答案不在候选里：{answer!r}，按分数取第一个")

    return MainTexResult(
        status=OK,
        main=ranked[0].path,
        candidates=ranked,
        ambiguous=True,
        commented_out=tuple(commented),
        warnings=tuple(warnings),
        message=(
            f"{len(tied)} 个候选同分（{top}）：{[c.relpath for c in tied]}；"
            f"暂取 {ranked[0].relpath}，由 baseline 编译裁决"
        ),
    )


def _match_answer(root: Path, candidates: tuple[MainCandidate, ...], answer: object) -> MainCandidate | None:
    if answer is None:
        return None
    if isinstance(answer, MainCandidate):
        answer = answer.path
    if not isinstance(answer, (str, os.PathLike)):
        return None
    text = str(answer).replace("\\", "/")
    for candidate in candidates:
        if text in (candidate.relpath, candidate.path.name, str(candidate.path)):
            return candidate
    try:
        resolved = Path(text)
        if not resolved.is_absolute():
            resolved = root / resolved
        resolved = resolved.resolve()
    except OSError:
        return None
    for candidate in candidates:
        if candidate.path.resolve() == resolved:
            return candidate
    return None


# ------------------------------------------------------------------------- 展开


@dataclass(frozen=True)
class FlattenResult:
    """flatten 的结构化结果。失败一律走状态，不抛栈。"""

    status: str
    flat: Path | None = None
    main: Path | None = None
    command: tuple[str, ...] = ()
    bbl_expanded: bool = False
    chars: int = 0
    stderr: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK

    def to_json(self) -> dict:
        data: dict = {
            "status": self.status,
            "main": None if self.main is None else self.main.name,
            "command": list(self.command),
            "bbl_expanded": self.bbl_expanded,
            "chars": self.chars,
        }
        if self.stderr:
            data["stderr"] = self.stderr
        if self.message:
            data["message"] = self.message
        return data


def flatten(
    workdir: Workdir,
    main_tex: str | os.PathLike[str],
    *,
    latexpand: str = LATEXPAND,
    timeout: float = DEFAULT_TIMEOUT,
) -> FlattenResult:
    """调 latexpand 把主文件展平成 `build/flat.tex`。

    `main_tex` 可以是绝对路径，或相对 `workdir.src` 的路径。有同名 `.bbl` 时加
    `--expand-bbl`（见模块文档）。latexpand 不在 PATH → `status="missing_tool"`，
    消息里指向 `tongtu doctor`。
    """
    main = Path(main_tex)
    if not main.is_absolute():
        main = workdir.src / main
    if not main.is_file():
        return FlattenResult(status=MISSING_MAIN, main=main, message=f"主文件不存在：{main}")

    executable = shutil.which(latexpand)
    if executable is None:
        return FlattenResult(
            status=MISSING_TOOL,
            main=main,
            message=(
                f"PATH 中没有 {latexpand}——flatten 阶段展开多文件源码需要它；"
                "跑 `tongtu doctor` 看缺什么，或用参考镜像（架构 §10）"
            ),
        )

    command = [executable, "--keep-comments"]
    bbl = main.with_suffix(".bbl")
    bbl_expanded = bbl.is_file()
    if bbl_expanded:
        command += ["--expand-bbl", bbl.name]
    command.append(main.name)

    try:
        proc = subprocess.run(
            command,
            cwd=main.parent,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return FlattenResult(
            status=FAILED,
            main=main,
            command=tuple(command),
            bbl_expanded=bbl_expanded,
            message=f"latexpand 调用失败（{type(exc).__name__}）：{exc}",
        )

    stderr = proc.stderr.decode("utf-8", errors="replace").strip()[-_STDERR_TAIL:]
    if proc.returncode != 0:
        return FlattenResult(
            status=FAILED,
            main=main,
            command=tuple(command),
            bbl_expanded=bbl_expanded,
            stderr=stderr,
            message=f"latexpand 退出码 {proc.returncode}",
        )
    if not proc.stdout.strip():
        return FlattenResult(
            status=EMPTY,
            main=main,
            command=tuple(command),
            bbl_expanded=bbl_expanded,
            stderr=stderr,
            message="latexpand 输出为空",
        )

    workdir.build.mkdir(parents=True, exist_ok=True)
    flat = workdir.build / FLAT_NAME
    # 写字节而非解码后的文本：源码可能是 latin-1 等编码，展平不该改动一个字节
    flat.write_bytes(proc.stdout)
    return FlattenResult(
        status=OK,
        flat=flat,
        main=main,
        command=tuple(command),
        bbl_expanded=bbl_expanded,
        chars=len(proc.stdout.decode("utf-8", errors="ignore")),
        stderr=stderr,
    )
