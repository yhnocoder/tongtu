r"""survey 阶段驱动器：合并三层 input glossary 并按全文命中过滤，照录论文摘要。

survey 只读 `build/` 与术语表输入、只写 `build/`：纯文本变换，不访问网络、不编译，也不拉起
agent。上游结论与两个输入 hash 从 mask manifest 装载。术语表的解析、合并与命中匹配在
`tongtu/glossary.py`（translate 的逐 chunk 命中复用同一实现），本模块管前置条件、跳过判定、
摘要照录与落盘。

前置条件：mask manifest 缺失或不可解析，或它的状态是 ok 但 `build/masked.tex` 与
`build/blocks.json` 有缺（含 blocks.json 不可解析）→ 状态 `mask_missing`；mask 的状态不是 ok
→ 状态 `mask_not_ok`，本次读到的 mask 状态与它记录的 fetch 状态转录进 manifest；任一 input
glossary 读不到或不符合形状 → 状态 `glossary_invalid`，message 指出文件路径与首个错误。前置
条件不满足同样写 survey manifest：驱动器不向调用方抛栈，每次执行的结论都落盘。

章节标题树由 `chunking.document_headings` 扫 `masked.tex` 得出，写进 brief 的 `heading_tree`：
标题结构是分块的输入而不是分块的产物，survey 与 chunk 因此共用同一份扫描实现、各自直接读
掩码文本，两个阶段不互相依赖。掩码文本扫不出标题结构（环境配对不上）或一个标题命令都没有
时，`heading_tree` 为 null 并记一条 warning，不是失败。

摘要照录两条来路，按序尝试：`blocks.json` 中 kind 为 abstract 的 caption 槽位（摘要写在前导区
的文档类，mask 阶段已抽出），取其原始文本；槽位不存在时在 `masked.tex` 里扫描 abstract 环境
（正文形态，多数论文如此），照录环境体。两条都落空则 `abstract` 为 null，不是失败。掩码文本
里没有注释（mask 已把注释整块摘出），故环境扫描按字面匹配即可。

重跑语义：输入 hash 是三个值——`masked_sha256` 与 `blocks_sha256` 从 mask manifest 转录，
`glossary_input_sha256` 是三层输入按层序规范化序列后的 sha256。已有 survey manifest 可解析、
状态 ok、三个输入 hash 与当前值一致、两件产物都存在 → 跳过；失败状态不跳过；`force` 无视已有
结论。每次非跳过的执行开始先删除已有的两件产物，失败时不留上次的产物误导下游。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .. import chunking, config, glossary, manifests, workdir
from ..artifacts.fetch import FetchStatus
from ..artifacts.mask import BlocksFile, MaskManifest, MaskStatus
from ..artifacts.survey import (
    AbstractSource,
    BriefFile,
    BriefHeading,
    DoNotTranslateEntry,
    FilteredTerm,
    GlossaryFile,
    GlossaryInputRecord,
    SurveyManifest,
    SurveyStatus,
    TermEntry,
)
from ..glossary import GlossaryError, GlossaryLayer, GlossarySource
from ..masking import ABSTRACT_ENVIRONMENT, CaptionKind
from .mask import BLOCKS_FILENAME, MASKED_FILENAME, blocks_path, masked_path
from .mask import STAGE_NAME as MASK_STAGE_NAME

#: 阶段名，也是 stage manifest 的文件名主干。
STAGE_NAME = "survey"

#: resolved glossary 在 build/ 下的文件名，与 input glossary 同名（前者由本阶段产出，后者由
#: 用户手写，两者位置不同：build/ 之下与工作目录根、全局配置目录）。
GLOSSARY_FILENAME = glossary.GLOSSARY_FILENAME

#: 全局语境在 build/ 下的文件名，artifact contract 的一员。
BRIEF_FILENAME = "brief.json"

#: 产物文本的编码；读写都用它。
ENCODING = "utf-8"

#: 在掩码文本里定位 abstract 环境的两个匹配式（正文形态的摘要来路）。
BEGIN_ABSTRACT_RE = re.compile(r"\\begin\s*\{" + ABSTRACT_ENVIRONMENT + r"\}")
END_ABSTRACT_RE = re.compile(r"\\end\s*\{" + ABSTRACT_ENVIRONMENT + r"\}")


@dataclass(frozen=True)
class SurveyResult:
    """驱动器的返回值：manifest、工作目录与是否命中跳过。"""

    manifest: SurveyManifest
    workdir: workdir.Workdir
    skipped: bool


# ------------------------------------------------------------------ 阶段驱动器


def survey(
    workdir_name: str | None = None,
    workdir_path: Path | None = None,
    *,
    glossary_paths: Sequence[Path] = (),
    force: bool = False,
) -> SurveyResult:
    """装载 mask 结论与三层术语表，写出 glossary.json、brief.json 与 manifest。

    `workdir_name` 是工作目录名（arXiv 编号，或本地源码目录的 basename），`workdir_path`
    直接给出论文工作目录本身并覆盖前者。`glossary_paths` 是命令行 `--glossary` 给出的文件，
    按给出顺序排列，靠后的优先。`force` 无视已有结论重新执行。
    """
    paper_workdir = workdir.Workdir(workdir.resolve(workdir_name, workdir_path))
    paper_workdir.create()  # 前置条件不满足时也要写 manifest，先确保四区存在

    # 上游 mask manifest 读不到或不可解析都转 mask_missing，两种情形对本阶段含义相同。
    mask_manifest = manifests.load_manifest(paper_workdir.manifest_path(MASK_STAGE_NAME), MaskManifest)
    if mask_manifest is None:
        # 两个输入 hash 从 mask manifest 转录，读不到就无从做跳过判定，直接给结论。
        _reset_outputs(paper_workdir)
        return _write_result(
            paper_workdir,
            SurveyManifest(
                status=SurveyStatus.MASK_MISSING,
                message="读不到 build/manifests/mask.json 或它不可解析，先跑 `tongtu stage mask`。",
            ),
        )

    sources, read_error = _load_sources(paper_workdir, glossary_paths)
    input_sha256 = glossary.input_sha256(sources)
    if not force and not read_error:
        # 某份表读不到时不做跳过判定：它的内容以空占位进 hash，与「该层缺席」无从区分。
        existing = _load_skippable_manifest(paper_workdir, mask_manifest, input_sha256)
        if existing is not None:
            return SurveyResult(manifest=existing, workdir=paper_workdir, skipped=True)

    _reset_outputs(paper_workdir)
    if mask_manifest.status is not MaskStatus.OK:
        return _write_result(
            paper_workdir,
            _manifest_from_mask(
                SurveyStatus.MASK_NOT_OK,
                mask_manifest,
                sources,
                input_sha256,
                message=(
                    f"mask 的状态是 {mask_manifest.status}，上游 fetch 判定源是 PDF 而非 LaTeX 源码，"
                    "没有可合并术语表的掩码文本，走 degraded path。"
                    if mask_manifest.fetch_status == FetchStatus.PDF_ONLY
                    else f"mask 的状态是 {mask_manifest.status}，不是 ok，先重跑 `tongtu stage mask`。"
                ),
            ),
        )
    masked_file = masked_path(paper_workdir)
    blocks_file = blocks_path(paper_workdir)
    if not masked_file.is_file() or not blocks_file.is_file():
        absent = MASKED_FILENAME if not masked_file.is_file() else BLOCKS_FILENAME
        return _write_result(
            paper_workdir,
            _manifest_from_mask(
                SurveyStatus.MASK_MISSING,
                mask_manifest,
                sources,
                input_sha256,
                message=f"mask 的状态是 ok，但 build/{absent} 不是文件，先跑 `tongtu stage mask`。",
            ),
        )
    blocks = _load_blocks(blocks_file)
    if blocks is None:
        return _write_result(
            paper_workdir,
            _manifest_from_mask(
                SurveyStatus.MASK_MISSING,
                mask_manifest,
                sources,
                input_sha256,
                message=f"build/{BLOCKS_FILENAME} 不可解析，先重跑 `tongtu stage mask`。",
            ),
        )
    if read_error:
        return _write_result(
            paper_workdir,
            _manifest_from_mask(
                SurveyStatus.GLOSSARY_INVALID, mask_manifest, sources, input_sha256, message=read_error
            ),
        )

    try:
        merged = _merge_sources(sources)
    except GlossaryError as error:
        return _write_result(
            paper_workdir,
            _manifest_from_mask(
                SurveyStatus.GLOSSARY_INVALID, mask_manifest, sources, input_sha256, message=str(error)
            ),
        )

    masked = masked_file.read_text(encoding=ENCODING)
    resolved = glossary.relevant_terms(merged.entries, masked)
    kept = set(resolved)
    filtered = tuple(entry for entry in merged.entries if entry not in kept)
    if len(resolved) + len(filtered) != len(merged.entries):
        # 出口判据的不变量：命中与被过滤两份清单恰好切分合并结果，不重不漏。
        return _write_result(
            paper_workdir,
            _manifest_from_mask(
                SurveyStatus.GLOSSARY_INVALID,
                mask_manifest,
                sources,
                input_sha256,
                message=(
                    f"合并结果 {len(merged.entries)} 条，命中 {len(resolved)} 条加被过滤 "
                    f"{len(filtered)} 条对不上，命中过滤的实现有误。"
                ),
            ),
        )

    abstract, abstract_source = _extract_abstract(blocks, masked)
    heading_tree, heading_warnings = _extract_heading_tree(masked)
    glossary_bytes = _glossary_file(resolved, merged.style).model_dump_json(indent=2).encode(ENCODING) + b"\n"
    brief_file = BriefFile(abstract=abstract, heading_tree=heading_tree)
    brief_bytes = brief_file.model_dump_json(indent=2).encode(ENCODING) + b"\n"
    glossary_file_path(paper_workdir).write_bytes(glossary_bytes)
    brief_path(paper_workdir).write_bytes(brief_bytes)
    return _write_result(
        paper_workdir,
        _manifest_from_mask(
            SurveyStatus.OK,
            mask_manifest,
            sources,
            input_sha256,
            glossary_sha256=hashlib.sha256(glossary_bytes).hexdigest(),
            brief_sha256=hashlib.sha256(brief_bytes).hexdigest(),
            terms_total=sum(1 for entry in resolved if entry.translation is not None),
            do_not_translate_total=sum(1 for entry in resolved if entry.translation is None),
            filtered=[FilteredTerm(word=entry.word, decided_by=entry.decided_by) for entry in filtered],
            abstract_source=abstract_source,
            abstract_chars=len(abstract) if abstract is not None else 0,
            headings_total=len(heading_tree) if heading_tree is not None else 0,
            warnings=heading_warnings,
        ),
    )


# ------------------------------------------------------------------ 术语表输入


def _load_sources(
    paper_workdir: workdir.Workdir, glossary_paths: Sequence[Path]
) -> tuple[tuple[GlossarySource, ...], str]:
    """读三层 input glossary，返回（按层序排列的合并单元、读不到时的失败说明）。

    全局配置目录与论文工作目录的表默认不存在，缺失即该层缺席；命令行 `--glossary` 是用户显式
    给出的文件，缺失是用户错误，不静默跳过。命令行一份都没给时补一个空占位，保持三层形状。
    """
    sources: list[GlossarySource] = []
    error = ""
    requests: list[tuple[GlossaryLayer, Path, bool]] = [
        (GlossaryLayer.GLOBAL, config.glossary_path(), False),
        (GlossaryLayer.PAPER, input_glossary_path(paper_workdir), False),
    ]
    requests.extend((GlossaryLayer.CLI, path, True) for path in glossary_paths)
    for layer, path, required in requests:
        content, failure = _read_source(path, required=required)
        sources.append(GlossarySource(layer=layer, path=path, content=content))
        if failure and not error:
            error = failure
    if not glossary_paths:
        sources.append(GlossarySource(layer=GlossaryLayer.CLI))
    return tuple(sources), error


def _read_source(path: Path, *, required: bool) -> tuple[str | None, str]:
    """读一份 input glossary，返回（内容、读不到时的失败说明）。

    `required` 为假时文件不存在按该层缺席处理；为真时报错。存在但读不出或不是 UTF-8 一律报错，
    两种情形都不能按缺席处理：那会把用户的配置静默丢掉。
    """
    try:
        return path.read_text(encoding=ENCODING), ""
    except FileNotFoundError:
        if required:
            return None, f"--glossary 给出的 {path} 不存在。"
        return None, ""
    except (OSError, UnicodeDecodeError) as error:
        return None, f"读不到 input glossary {path}（{manifests.describe_error(error)}）。"


def _load_blocks(path: Path) -> BlocksFile | None:
    """读 build/blocks.json 并按 artifact model 解析；读不到或不合 schema 返回 None。

    两种失败对本阶段含义相同（没有可用的 caption 槽位清单），由调用方一并转 mask_missing。
    """
    try:
        return BlocksFile.model_validate_json(path.read_text(encoding=ENCODING))
    except (OSError, ValidationError):
        return None


def _merge_sources(sources: Sequence[GlossarySource]) -> glossary.MergedGlossary:
    """逐份解析并按层序合并；某份不符合形状时抛 `GlossaryError`，由调用方转 glossary_invalid。"""
    units: list[tuple[GlossaryLayer, glossary.InputGlossary]] = []
    for source in sources:
        if source.content is None:
            continue
        units.append((source.layer, glossary.parse(source.content, str(source.path))))
    return glossary.merge(units)


# ------------------------------------------------------------------ 摘要照录


def _extract_abstract(blocks: BlocksFile, masked: str) -> tuple[str | None, AbstractSource]:
    """按两条来路取论文原文摘要，返回（摘要、来路）；两条都落空返回（None、absent）。"""
    for caption in blocks.captions:
        if caption.kind is CaptionKind.ABSTRACT:
            text = caption.tex.strip()
            if text:
                return text, AbstractSource.PREAMBLE_SLOT
            break
    body = _abstract_environment_body(masked)
    if body:
        return body, AbstractSource.BODY_ENVIRONMENT
    return None, AbstractSource.ABSENT


def _extract_heading_tree(masked: str) -> tuple[list[BriefHeading] | None, list[str]]:
    """扫出全文章节标题树，返回（标题树、警告清单）；扫不出结构时返回（None、一条警告）。

    扫描口径与分块共用一份实现（`chunking.document_headings`）：标题结构是分块的输入，不是
    分块的产物，survey 与 chunk 因此都直接扫 `masked.tex`，两个阶段不互相依赖。掩码文本的
    环境配对不上时 `chunking` 抛 `ChunkError`——那是 chunk 阶段要判失败的情形，对 survey 只
    意味着没有标题树可写，记一条警告后照常出产物。
    """
    try:
        headings = chunking.document_headings(masked)
    except chunking.ChunkError as error:
        return None, [f"掩码文本扫不出标题结构（{manifests.describe_error(error)}），brief 的 heading_tree 为 null。"]
    if not headings:
        return None, ["掩码文本里一个标题命令都没有，brief 的 heading_tree 为 null。"]
    return [
        BriefHeading(depth=heading.depth, level=heading.level, argument=heading.argument) for heading in headings
    ], []


def _abstract_environment_body(masked: str) -> str:
    r"""照录掩码文本里首个 abstract 环境的环境体，仅去除首尾空白；没有该环境返回空串。

    环境体照录，其中的 placeholder 原样留着：它记录的是原文摘要在掩码文本里的形态，供
    translate 当全局语境用，不再回填。
    """
    opening = BEGIN_ABSTRACT_RE.search(masked)
    if opening is None:
        return ""
    closing = END_ABSTRACT_RE.search(masked, opening.end())
    if closing is None:
        return ""
    return masked[opening.end() : closing.start()].strip()


# ------------------------------------------------------------------ 产物组装


def _glossary_file(entries: Sequence[glossary.GlossaryEntry], style: str | None) -> GlossaryFile:
    """把命中的词条按两个区段分开，与 style 一起组装成 resolved glossary。

    `style` 是可选输入：三层都没写这一段、或最高层写的是空白，产物里都写 null。
    """
    return GlossaryFile(
        terms=[
            TermEntry(word=entry.word, translation=entry.translation, decided_by=entry.decided_by)
            for entry in entries
            if entry.translation is not None
        ],
        do_not_translate=[
            DoNotTranslateEntry(word=entry.word, decided_by=entry.decided_by)
            for entry in entries
            if entry.translation is None
        ],
        style=style,
    )


def _manifest_from_mask(
    status: SurveyStatus,
    mask_manifest: MaskManifest,
    sources: Sequence[GlossarySource],
    input_sha256: str,
    **fields: object,
) -> SurveyManifest:
    """组装 manifest：三个输入 hash、术语表输入一览与上游两个状态一律转录，其余字段由调用处给出。"""
    return SurveyManifest(
        status=status,
        masked_sha256=mask_manifest.masked_sha256,
        blocks_sha256=mask_manifest.blocks_sha256,
        glossary_input_sha256=input_sha256,
        glossary_inputs=[
            GlossaryInputRecord(
                layer=source.layer,
                path=str(source.path) if source.path is not None else "",
                present=source.content is not None,
            )
            for source in sources
        ],
        mask_status=str(mask_manifest.status),
        fetch_status=mask_manifest.fetch_status,
        **fields,
    )


# ------------------------------------------------------------------ 跳过判定与落盘


def _load_skippable_manifest(
    paper_workdir: workdir.Workdir, mask_manifest: MaskManifest, input_sha256: str
) -> SurveyManifest | None:
    """读已有 survey manifest；可解析、状态 ok、三个输入 hash 一致且两件产物都在，返回它，否则返回 None。"""
    manifest = manifests.load_manifest(paper_workdir.manifest_path(STAGE_NAME), SurveyManifest)
    if manifest is None:
        return None
    if manifest.status is not SurveyStatus.OK:
        return None
    if manifest.masked_sha256 != mask_manifest.masked_sha256:
        return None
    if manifest.blocks_sha256 != mask_manifest.blocks_sha256:
        return None
    if manifest.glossary_input_sha256 != input_sha256:
        return None
    if not glossary_file_path(paper_workdir).is_file():
        return None
    if not brief_path(paper_workdir).is_file():
        return None
    return manifest


def input_glossary_path(paper_workdir: workdir.Workdir) -> Path:
    """论文工作目录内 input glossary 的路径：与 src/、build/ 同级，用户手写，默认不存在。"""
    return paper_workdir.path / GLOSSARY_FILENAME


def glossary_file_path(paper_workdir: workdir.Workdir) -> Path:
    """resolved glossary 的路径；下游 translate 与 export 取同一个文件。"""
    return paper_workdir.build / GLOSSARY_FILENAME


def brief_path(paper_workdir: workdir.Workdir) -> Path:
    """brief.json 的路径；下游 translate 与 export 取同一个文件。"""
    return paper_workdir.build / BRIEF_FILENAME


def _reset_outputs(paper_workdir: workdir.Workdir) -> None:
    """删除两件产物：失败时不留上次的结果误导下游。"""
    glossary_file_path(paper_workdir).unlink(missing_ok=True)
    brief_path(paper_workdir).unlink(missing_ok=True)


def _write_result(paper_workdir: workdir.Workdir, manifest: SurveyManifest) -> SurveyResult:
    """写出 manifest 并组装返回值；除跳过外的每次执行（含失败）都经此处落盘。"""
    manifests.write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return SurveyResult(manifest=manifest, workdir=paper_workdir, skipped=False)
