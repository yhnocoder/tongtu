"""export 阶段：产物包组装 + anchors 合成 + 检验页 + 契约自校验（架构 §3 export 行、§7）。

流水线的最后一段，也是唯一一个**出口判据就是产物契约本身**的阶段：

    build/ 里的中间产物 ──► out/ 契约文件 ──► anchors ──► report.json ──► report.html
                                              └── 每份 JSON 过 docs/schemas/ ──► 全绿才算成功

「agent 负责判断、脚本负责验证」在这里退化成纯粹的机械动作：本阶段没有任何 agent 关节，
它的正确性完全由 schema 校验裁决。任何一份 JSON 不过 schema 都判 `failed`——那说明**通途
自己**违了约（不是论文的问题），必须当场炸出来，不能悄悄交付一个不合契约的包。

## out/ 的形状（架构 §7 的产物表）

    out/
    ├── zh.tex zh.pdf zh.synctex.gz          # 契约文件在顶层
    ├── blocks.json chunks.json brief.json glossary.json anchors.json report.json
    ├── report.html report-data.js vendor/   # 静态检验页（§11）
    ├── figures/*.png + figures.json         # 预渲染图与元数据
    └── zh-pack/                             # 自包含编译包：zh.tex + cls/sty/bbl + 图 + fonts/

**`zh.tex` 为什么有两份**：契约要求 `zh.tex` 在包顶层（消费方按名字取），而「自包含」
要求它和一堆资产待在同一个目录里（解包即 `latexmk -xelatex zh.tex`）。两者不可兼得于
一个路径，故顶层放契约文件、`zh-pack/` 放可编译包，两处内容逐字节相同（测试断言这一点）。
`zh-pack/` 的资产直接**从编译目录 `build/zh/` 取并解引用符号链接**——那正是编译器实际看
到的那套文件，不需要再猜「哪些资产是必需的」（v2 `package.py` 猜了四个图目录名，真实论文
里叫 `figs` / `plots` 的比比皆是，漏一个就是图全丢）。

## 翻译记忆写回

`out/chunks.json` 是**权威翻译记忆**（架构 §7、决策 3）：`build/` 整体删除后，下一次
`tongtu run` 从它全量命中。故 export 把 `build/zh-chunks/chunks.json` 原样搬进 `out/`，
这也是 `tongtu.memory.load` 的第一顺位来源。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .. import CONTRACT_VERSION, __version__
from .. import anchors as anchors_module
from .. import report_page
from ..memory import CHUNKS_NAME, ZH_CHUNKS_DIRNAME
from ..schema_check import SchemaError
from ..schema_check import check as schema_check
from ..workdir import Workdir
from . import compile as compile_stage
from . import figures as figures_stage
from . import survey as survey_stage

__all__ = [
    "ANCHORS_NAME",
    "BLOCKS_NAME",
    "CONTRACT_SCHEMAS",
    "FAILED",
    "OK",
    "PACK_DIRNAME",
    "PACK_README",
    "PACK_SKIP_SUFFIXES",
    "REPORT_NAME",
    "SYNCTEX_NAME",
    "ZH_PDF",
    "ZH_TEX",
    "Artifact",
    "ExportResult",
    "caption_translations",
    "export",
    "pack",
]


# --------------------------------------------------------------------- 常量

ZH_TEX = "zh.tex"
ZH_PDF = "zh.pdf"
SYNCTEX_NAME = "zh.synctex.gz"
BLOCKS_NAME = "blocks.json"
ANCHORS_NAME = "anchors.json"
REPORT_NAME = "report.json"

#: 自包含编译包的目录名与说明文件。
PACK_DIRNAME = "zh-pack"
PACK_README = "README.md"

#: 打包时跳过的编译中间文件后缀（`.bbl` **不在其列**——预编译参考文献要随包走，
#: 否则解包后还得跑一遍 bibtex）。
PACK_SKIP_SUFFIXES: frozenset[str] = frozenset(
    {
        ".aux", ".log", ".out", ".toc", ".lof", ".lot", ".fls", ".fdb_latexmk",
        ".xdv", ".bcf", ".blg", ".idx", ".ilg", ".ind",
        ".nav", ".snm", ".vrb", ".dvi",
    }
)

#: out/ 里的 JSON 契约文件 → schema 名（`report.json` 单列，见 :func:`_validate`）。
CONTRACT_SCHEMAS: dict[str, str] = {
    BLOCKS_NAME: "blocks",
    CHUNKS_NAME: "chunks",
    survey_stage.BRIEF_NAME: "brief",
    survey_stage.GLOSSARY_NAME: "glossary",
    ANCHORS_NAME: "anchors",
    f"{figures_stage.FIGURES_DIRNAME}/{figures_stage.FIGURES_JSON}": "figures",
}

OK = "ok"
FAILED = "failed"

#: 掩码流里的 CAP 行（回填 caption 译文用）。判定规则与 `unmask` 完全一致：
#: 流中文本与 `stream_text` 相同或为空 ⇒ 没人翻译过，不算译文。
_CAPTION_LINE_RE = re.compile(r"⟦CAP-(\d+)⟧([^\n]*)")


# --------------------------------------------------------------------- 结果


@dataclass(frozen=True)
class Artifact:
    """产物包里的一个文件及其 schema 自校验结果（进 `report.json` 的 `artifacts`）。"""

    path: str
    bytes: int = 0
    schema_valid: bool | None = None
    """非 JSON 产物、或 schema 取不到时为 None。"""

    errors: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"path": self.path, "bytes": self.bytes, "schema_valid": self.schema_valid}


@dataclass(frozen=True)
class ExportResult:
    """export 阶段的结构化结果。"""

    status: str
    out_dir: Path
    artifacts: tuple[Artifact, ...] = ()
    anchors: anchors_module.AnchorsResult | None = None
    report_path: Path | None = None
    page: report_page.PageResult | None = None
    pack_dir: Path | None = None
    pack_files: int = 0
    pack_bytes: int = 0
    """自包含包的体积。字体占大头（约 50 MB），云上批量存包时看这个数决定要不要
    `bundle_fonts=False`。"""

    figures_count: int = 0
    warnings: tuple[str, ...] = ()
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def invalid(self) -> tuple[Artifact, ...]:
        return tuple(a for a in self.artifacts if a.schema_valid is False)

    def outputs(self) -> tuple[Path, ...]:
        """写进 manifest 的输出清单（`manifest_fresh` 逐个查存在，缺一个就重算）。"""
        paths = [self.out_dir / a.path for a in self.artifacts]
        if self.report_path is not None:
            paths.append(self.report_path)  # artifacts 里没有它自己，这里补上
        if self.pack_dir is not None:
            paths.append(self.pack_dir)
        return tuple(paths)

    def to_json(self) -> dict:
        data: dict = {
            "status": self.status,
            "out_dir": str(self.out_dir),
            "artifacts": [a.to_json() for a in self.artifacts],
            "pack_files": self.pack_files,
            "pack_bytes": self.pack_bytes,
            "figures": self.figures_count,
        }
        if self.anchors is not None:
            data["anchors"] = self.anchors.to_json()
        if self.page is not None:
            data["report_page"] = self.page.to_json()
        if self.warnings:
            data["warnings"] = list(self.warnings)
        if self.message:
            data["message"] = self.message
        return data


# ------------------------------------------------------------------- 小工具


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: Mapping) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _copy(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def caption_translations(stream: str, blocks: Mapping | None) -> dict[str, str]:
    """从译文掩码流里取各 caption 的译文（`{caption 原文: 译文}`）。

    键用**原文**而不是 caption id：`figures.json` 记的是 `caption.source`（figures 阶段
    只依赖 `src/`，不认识 caption 槽位 id），按原文回填才对得上。判定「这算不算译文」用的
    是 unmask 的同一条规则——流里的行文本与 `stream_text` 相同或为空即视为没翻译过。
    """
    if not stream or not isinstance(blocks, Mapping):
        return {}
    by_number: dict[str, Mapping] = {}
    for caption in blocks.get("captions", ()) or ():
        if isinstance(caption, Mapping) and isinstance(caption.get("id"), str):
            by_number[caption["id"]] = caption
    found: dict[str, str] = {}
    for match in _CAPTION_LINE_RE.finditer(stream):
        caption = by_number.get(f"CAP-{match.group(1)}")
        if caption is None:
            continue
        text = match.group(2).strip()
        source = str(caption.get("text") or "").strip()
        if not text or text == str(caption.get("stream_text") or "").strip():
            continue  # 未改动 ⇒ 原文（与 unmask 同一判据）
        if source:
            found[source] = text
    return found


# ------------------------------------------------------------------ 自包含包


def pack(
    build_zh: Path,
    out_pack: Path,
    *,
    tex_text: str,
    engine: str = "xelatex",
    fonts: bool = True,
) -> int:
    """把编译目录组装成自包含包（返回拷进去的文件数）。

    资产来源就是 `build/zh/`——**编译器实际看到的那套文件**，符号链接一律解引用拷贝。
    这样「包里有没有这张图」的答案与「编译时有没有这张图」永远一致，不需要另一套猜名字
    的规则（v2 `package.py` 的四个写死图目录名正是这么漏图的）。

    :param fonts: 是否把仓库字体（霞鹜文楷，约 50 MB）也拷进包里。默认拷——`zh.tex` 里
        写的是包内相对路径 `Path={fonts/}`，不带字体的包在别人机器上编出来全是豆腐，那就
        不叫自包含了。体积敏感的场景（云上批量存包）可置 False，代价是解包方须自备中文
        字体。
    """
    if out_pack.exists():
        shutil.rmtree(out_pack)
    out_pack.mkdir(parents=True, exist_ok=True)
    count = 0
    if build_zh.is_dir():
        for entry in sorted(build_zh.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or entry.name == "__MACOSX":
                continue
            if entry.name == compile_stage.ZH_TEX:
                continue  # 单独写（下面），避免依赖编译目录里那份的时间戳
            if entry.is_file():  # 符号链接指向文件时 is_file() 亦为真（自动解引用）
                if _is_intermediate(entry.name):
                    continue
                try:
                    _copy(entry.resolve(), out_pack / entry.name)
                except OSError:
                    continue
                count += 1
                continue
            if entry.is_dir():
                if entry.name == "fonts" and not fonts:
                    continue
                try:
                    shutil.copytree(
                        entry,
                        out_pack / entry.name,
                        symlinks=False,
                        ignore=shutil.ignore_patterns(".*", "__MACOSX"),
                        dirs_exist_ok=True,
                    )
                except OSError:
                    continue
                count += sum(1 for p in (out_pack / entry.name).rglob("*") if p.is_file())

    (out_pack / ZH_TEX).write_text(tex_text, encoding="utf-8")
    count += 1
    flag = {"xelatex": "-xelatex", "lualatex": "-lualatex"}.get(engine, "-pdf")
    has_bbl = any(p.suffix == ".bbl" for p in out_pack.iterdir() if p.is_file())
    has_fonts = (out_pack / "fonts").is_dir()
    (out_pack / PACK_README).write_text(
        "# 自包含编译包（通途产出）\n\n"
        "解包后一条命令即可编译：\n\n"
        f"```\nlatexmk {flag} {ZH_TEX}\n```\n\n"
        + (
            "- 中文字体（霞鹜文楷）随包在 `fonts/`，`zh.tex` 里用的是包内相对路径，无需安装；\n"
            if has_fonts
            else "- **本包不带字体**：`zh.tex` 引用的是相对路径 `fonts/`，"
            "请自备霞鹜文楷（或改 `\\setCJKmainfont` 为本地中文字体），否则中文全是豆腐；\n"
        )
        + ("- `*.bbl` 是预编译参考文献，不必再跑 bibtex；\n" if has_bbl else "")
        + "- 其余 `.cls` / `.sty` / 图目录都是原论文源码包里的资产，与编译时所见完全一致。\n",
        encoding="utf-8",
    )
    return count + 1


def _is_intermediate(name: str) -> bool:
    """编译中间文件？

    `zh.synctex.gz` 与 `zh.pdf` 也算——它们在包顶层另有一份契约副本，塞进 `zh-pack/`
    只是把体积翻倍。**图片 PDF 不在其列**：只有 `zh.` 前缀的那份是编译产物。
    """
    lower = name.lower()
    if lower.endswith(".synctex.gz") or lower.endswith(".run.xml"):
        return True
    if lower == compile_stage.ZH_TEX.lower():
        return True
    if lower.startswith("zh.") and lower.endswith(".pdf"):
        return True
    return Path(lower).suffix in PACK_SKIP_SUFFIXES


# --------------------------------------------------------------------- 校验


def _validate(out: Path, warnings: list[str]) -> list[Artifact]:
    """逐个 out/ 契约文件过 schema，组装 `artifacts` 清单。

    非 JSON 产物（`zh.tex` / `zh.pdf` / `report.html` …）记 `schema_valid=null`——它们的
    正确性由别的判据裁决（编译、浏览器），不是这一层的事。
    """
    artifacts: list[Artifact] = []
    for name in sorted(_shipped(out)):
        path = out / name
        schema = CONTRACT_SCHEMAS.get(name)
        valid: bool | None = None
        errors: tuple[str, ...] = ()
        if schema is not None:
            document = _read_json(path)
            if document is None:
                valid, errors = False, (f"{name} 读不出来或不是 JSON 对象",)
            else:
                try:
                    found = schema_check(document, schema)
                except SchemaError as exc:
                    warnings.append(f"跳过 {name} 的 schema 校验：{exc}")
                else:
                    valid = not found
                    errors = tuple(found[:5])
        artifacts.append(
            Artifact(path=name, bytes=_size(path), schema_valid=valid, errors=errors)
        )
    return artifacts


def _shipped(out: Path) -> list[str]:
    """产物包里要记账的文件：顶层契约文件 + `figures/` 逐个（相对 out/ 的路径）。

    `zh-pack/` 与 `vendor/` 是**目录**级产物，逐文件记账只会把清单撑成噪音（vendor 里
    是两个 PDF.js 大文件，pack 里是整套源码资产），故不进 `artifacts`——它们的存在与体积
    另有 `pack_files` / `pack_bytes` 两个数交代。`report.json` 也不在其中：它就是这份清单
    本身，把自己列进去等于自我评判，没有效力。
    """
    names: list[str] = []
    for path in out.iterdir():
        if path.is_file() and path.name != REPORT_NAME:
            names.append(path.name)
    figures_dir = out / figures_stage.FIGURES_DIRNAME
    if figures_dir.is_dir():
        for path in sorted(figures_dir.iterdir()):
            if path.is_file():
                names.append(f"{figures_stage.FIGURES_DIRNAME}/{path.name}")
    return names


# ------------------------------------------------------------------- 阶段入口


def export(
    workdir: Workdir,
    *,
    report: Mapping | None = None,
    blocks: Mapping | None = None,
    zh_tex: str | os.PathLike[str] | None = None,
    pdf: str | os.PathLike[str] | None = None,
    synctex: str | os.PathLike[str] | None = None,
    figures_dir: str | os.PathLike[str] | None = None,
    spans: str | os.PathLike[str] | Mapping | None = None,
    out_dir: str | os.PathLike[str] | None = None,
    title: str = "",
    bundle_fonts: bool = True,
) -> ExportResult:
    """组装产物包。出口判据：全部契约 JSON 过 schema（不过即 `failed`）。

    :param report: `report.json` 的主体（编排器组装，见 `tongtu.pipeline`）。缺省时只记
        一个最小骨架——本阶段不猜运行过程，那是编排器才知道的事。
    :param blocks: blocks.json 内容；缺省从 `build/blocks.json` 读。
    :param zh_tex / pdf / synctex: 编译产物；缺省取 `build/zh/` 下的同名文件。
    :param figures_dir: figures 阶段的产物目录；缺省 `build/figures/`。
    :param spans: 块在 `zh.tex` 里的字符区间（compile 落的 `build/zh-spans.json`，缺省即
        取它）。anchors 拿它精确定位块；文件缺席（旧产物、或关节⑥改过 `zh.tex` 使区间失效）
        时自动退回文本查找。
    """
    build = workdir.build
    out = Path(out_dir) if out_dir is not None else workdir.out
    out.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    zh_dir = build / compile_stage.ZH_DIRNAME
    tex_path = Path(zh_tex) if zh_tex is not None else zh_dir / compile_stage.ZH_TEX
    pdf_path = Path(pdf) if pdf is not None else zh_dir / ZH_PDF
    synctex_path = Path(synctex) if synctex is not None else zh_dir / SYNCTEX_NAME
    figures_src = Path(figures_dir) if figures_dir is not None else build / figures_stage.FIGURES_DIRNAME
    spans_src: str | os.PathLike[str] | Mapping | None = spans
    if spans_src is None:
        candidate = build / compile_stage.SPANS_NAME
        spans_src = candidate if candidate.is_file() else None

    def fail(message: str) -> ExportResult:
        return ExportResult(
            status=FAILED, out_dir=out, warnings=tuple(warnings), message=message
        )

    if not tex_path.is_file():
        return fail(f"没有 {tex_path}（先跑 compile）")
    if not pdf_path.is_file():
        # zh.pdf 是产物契约的核心（架构 §7）：没有它，包就不是包。
        return fail(f"没有 {pdf_path}——compile 没出 PDF，产物包不完整（先跑 compile）")

    tex_text = tex_path.read_text(encoding="utf-8", errors="replace")

    # --- 契约文件搬运 --------------------------------------------------
    _copy(tex_path, out / ZH_TEX)
    _copy(pdf_path, out / ZH_PDF)
    if synctex_path.is_file():
        _copy(synctex_path, out / SYNCTEX_NAME)
    else:
        (out / SYNCTEX_NAME).unlink(missing_ok=True)
        warnings.append(
            f"没有 {synctex_path.name}（编译时没开 -synctex=1 或被清掉了）——"
            "anchors 退化为页级锚点"
        )

    for source, name in (
        (build / BLOCKS_NAME, BLOCKS_NAME),
        (build / ZH_CHUNKS_DIRNAME / CHUNKS_NAME, CHUNKS_NAME),
        (build / survey_stage.BRIEF_NAME, survey_stage.BRIEF_NAME),
        (build / survey_stage.GLOSSARY_NAME, survey_stage.GLOSSARY_NAME),
    ):
        if source.is_file():
            _copy(source, out / name)
        else:
            warnings.append(f"缺 {source}（上游阶段没跑完？），产物包少一份契约文件")

    blocks_json = blocks if blocks is not None else _read_json(out / BLOCKS_NAME)
    chunks_json = _read_json(out / CHUNKS_NAME)

    # --- figures：PNG + 元数据（caption 译文从译块回填）-----------------
    figures_count = _export_figures(figures_src, out, blocks_json, chunks_json, warnings)

    # --- anchors 三来源合成 --------------------------------------------
    result = anchors_module.build(
        zh_tex=tex_text,
        blocks=blocks_json or {},
        pdf=out / ZH_PDF,
        synctex=(out / SYNCTEX_NAME) if (out / SYNCTEX_NAME).is_file() else None,
        chunks=chunks_json,
        spans=spans_src,
        tex_name=compile_stage.ZH_TEX,
        pdf_path=ZH_PDF,
    )
    warnings.extend(result.warnings)
    _write_json(out / ANCHORS_NAME, result.to_anchors_json())

    # --- 自包含包 ------------------------------------------------------
    pack_dir = out / PACK_DIRNAME
    engine = str((report or {}).get("compile", {}).get("engine") or "xelatex")
    pack_files = pack(
        zh_dir, pack_dir, tex_text=tex_text, engine=engine, fonts=bundle_fonts
    )

    # --- 契约自校验 ----------------------------------------------------
    body = dict(report or {})
    body.setdefault("paper", {"arxiv_id": workdir.arxiv_id or out.parent.name})
    body.setdefault("status", OK)
    body.setdefault("validation", {"chunks_total": 0})
    body.setdefault("compile", {"passed": True})
    body["contract_version"] = CONTRACT_VERSION
    body.setdefault("tongtu_version", __version__)

    # 检验页要先于 report.json 生成（它也是一份产物，要进 artifacts 记账），
    # 但它消费的 report 里还没有 artifacts —— 侧栏那份清单因此少列它自己，无妨。
    page = report_page.render(
        out,
        report={**body, "artifacts": []},
        anchors=result.to_anchors_json(),
        blocks=blocks_json,
        figures=_read_json(out / figures_stage.FIGURES_DIRNAME / figures_stage.FIGURES_JSON),
        pdf=out / ZH_PDF,
        pdf_name=ZH_PDF,
        title=title or (workdir.arxiv_id or ""),
    )
    warnings.extend(page.warnings)

    artifacts = _validate(out, warnings)
    invalid = [a for a in artifacts if a.schema_valid is False]
    status = FAILED if invalid else OK
    body["status"] = "failed" if invalid else body.get("status", OK)
    body["artifacts"] = [a.to_json() for a in artifacts]
    _write_json(out / REPORT_NAME, body)

    # report.json 自己也要过 schema——它是唯一一份「校验器写的报告」，
    # 校验它的只能是同一份 schema 实现（自我声明不算数，故这一步的结论直接决定阶段成败）。
    message = ""
    try:
        errors = schema_check(body, "report")
    except SchemaError as exc:
        warnings.append(f"跳过 report.json 的 schema 校验：{exc}")
        errors = []
    if errors:
        status = FAILED
        message = "report.json 不通过 schema 校验：" + errors[0]
    elif invalid:
        message = "产物不通过 schema 校验：" + "；".join(
            f"{a.path}（{a.errors[0] if a.errors else '未知'}）" for a in invalid
        )

    return ExportResult(
        status=status,
        out_dir=out,
        artifacts=tuple(artifacts),
        anchors=result,
        report_path=out / REPORT_NAME,
        page=page,
        pack_dir=pack_dir,
        pack_files=pack_files,
        pack_bytes=sum(_size(p) for p in pack_dir.rglob("*") if p.is_file()),
        figures_count=figures_count,
        warnings=tuple(warnings),
        message=message,
    )


def _export_figures(
    src: Path,
    out: Path,
    blocks: Mapping | None,
    chunks: Mapping | None,
    warnings: list[str],
) -> int:
    """把 `build/figures/` 搬进 `out/figures/`，并把 caption 译文回填进元数据。

    figures 阶段只依赖 `src/`（架构 §3、决策 9），它看不到译文；caption 的译文住在译块流
    里，故回填这一步天然属于 export——它是唯一同时握有两侧的阶段。
    """
    target = out / figures_stage.FIGURES_DIRNAME
    if not src.is_dir():
        warnings.append(f"没有 {src}（figures 阶段没跑？），产物包里不带图")
        return 0
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    for path in sorted(src.iterdir()):
        if path.is_file() and path.suffix.lower() == ".png":
            _copy(path, target / path.name)

    document = _read_json(src / figures_stage.FIGURES_JSON)
    if document is None:
        warnings.append(f"没有 {src / figures_stage.FIGURES_JSON}，产物包里的图缺元数据")
        return 0

    stream = "".join(
        str(entry.get("translation") or "")
        for entry in (chunks or {}).get("chunks", ()) or ()
        if isinstance(entry, Mapping)
    )
    translations = caption_translations(stream, blocks)
    filled = 0
    for figure in document.get("figures", ()) or ():
        if not isinstance(figure, dict):
            continue
        caption = figure.get("caption")
        if not isinstance(caption, dict):
            continue
        source = str(caption.get("source") or "").strip()
        translated = translations.get(source)
        if translated:
            caption["translation"] = translated
            filled += 1
    if translations and not filled:
        warnings.append("caption 译文一条都没对上（译文流里的 CAP 行与块清单不匹配？）")
    _write_json(target / figures_stage.FIGURES_JSON, document)
    return len(document.get("figures", ()) or ())
