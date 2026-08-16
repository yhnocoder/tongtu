r"""mask 阶段驱动器：把不该翻译的部分换成 placeholder，只留需要翻译的文本。

mask 只读 `build/`、只写 `build/`：纯文本变换，不访问网络、不编译，也不拉起 agent。上游
结论与输入 hash 从 precompile manifest 装载。词法状态机与 mask / unmask 的实现在
`tongtu/masking.py`，本模块管前置条件、跳过判定、自检编排与落盘。

前置条件：precompile manifest 缺失或不可解析，或它的状态是 ok 但 `build/precompile.tex`
不在 → 状态 `precompile_missing`；precompile 的状态不是 ok → 状态 `precompile_not_ok`，本次
读到的 precompile 状态与它记录的 fetch 状态转录进 manifest。前置条件不满足同样写 mask
manifest：驱动器不向调用方抛栈，每次执行的结论都落盘。

编码与哨兵：读 bytes 按 UTF-8 严格解码，写出时编码回 UTF-8，全程不做换行规范化，偏移与
比对都以字符计。解码失败或源码本身含 `⟦` `⟧` → `mask_failed`。依据：xelatex 对非法 UTF-8
输入直接报错，precompile 状态 ok 意味着这份文件已被 xelatex 接受过。

出口判据三条同时成立才是 ok：词法遍历无错（含解码与哨兵检查）；往返自检逐字符恒等；
`masked.tex` 与 `blocks.json` 落盘。自检是对未翻译的掩码文本跑 unmask、与 `precompile.tex`
全文逐字符比对，不等即 `mask_failed`，message 报首处差异的字符偏移与两侧上下文摘录。

重跑语义：输入 hash 是 `precompile_sha256`（从 precompile manifest 转录）与
`environments_table_sha256`（分类表文件内容的 sha256）两个值。表也参与的理由：重建期分类表
会频繁增补，不参与跳过判定的话，改表之后旧的掩码结果会静默留存。已有 mask manifest 可解析、
状态 ok、两个输入 hash 与当前值一致、`build/masked.tex` 与 `build/blocks.json` 都存在 →
跳过；失败状态不跳过；`force` 无视已有结论。每次非跳过的执行开始先删除已有的两件产物，失败
时不留上次的产物误导下游。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .. import manifests, masking, workdir
from ..artifacts.fetch import FetchStatus
from ..artifacts.mask import (
    BlockRecord,
    BlocksFile,
    CaptionRecord,
    EnvironmentDecisionRecord,
    MaskManifest,
    MaskStatus,
)
from ..artifacts.precompile import PrecompileManifest, PrecompileStatus
from .precompile import PRECOMPILE_FILENAME
from .precompile import STAGE_NAME as PRECOMPILE_STAGE_NAME

#: 阶段名，也是 stage manifest 的文件名主干。
STAGE_NAME = "mask"

#: 掩码文本在 build/ 下的文件名，survey 起下游全部阶段的输入。
MASKED_FILENAME = "masked.tex"

#: 被摘出去的 block 与 caption 槽位在 build/ 下的文件名，artifact contract 的一员。
BLOCKS_FILENAME = "blocks.json"

#: 产物文本的编码；解码从严，写出时编码回同一个。
ENCODING = "utf-8"


@dataclass(frozen=True)
class MaskResult:
    """驱动器的返回值：manifest、工作目录与是否命中跳过。"""

    manifest: MaskManifest
    workdir: workdir.Workdir
    skipped: bool


# ------------------------------------------------------------------ 阶段驱动器


def mask(
    workdir_name: str | None = None,
    workdir_path: Path | None = None,
    *,
    force: bool = False,
) -> MaskResult:
    """装载 precompile 结论、执行掩码与往返自检，写出 masked.tex、blocks.json 与 manifest。

    `workdir_name` 是工作目录名（arXiv 编号，或本地源码目录的 basename），`workdir_path`
    直接给出论文工作目录本身并覆盖前者。mask 不访问网络，也不读源目录，两个参数只用来定位
    工作目录。`force` 无视已有结论重新执行。
    """
    paper_workdir = workdir.Workdir(workdir.resolve(workdir_name, workdir_path))
    paper_workdir.create()  # 前置条件不满足时也要写 manifest，先确保四区存在

    # 上游 precompile manifest 读不到或不可解析都转 precompile_missing，两种情形对本阶段含义相同。
    precompile_manifest = manifests.load_manifest(
        paper_workdir.manifest_path(PRECOMPILE_STAGE_NAME), PrecompileManifest
    )
    if precompile_manifest is None:
        # 输入 hash 之一从 precompile manifest 转录，读不到就无从做跳过判定，直接给结论。
        _reset_outputs(paper_workdir)
        return _write_result(
            paper_workdir,
            MaskManifest(
                status=MaskStatus.PRECOMPILE_MISSING,
                message="读不到 build/manifests/precompile.json 或它不可解析，先跑 `tongtu stage precompile`。",
            ),
        )

    table_content, table_sha256, table_error = _read_environments_table()
    if not force:
        existing = _load_skippable_manifest(paper_workdir, precompile_manifest, table_sha256)
        if existing is not None:
            return MaskResult(manifest=existing, workdir=paper_workdir, skipped=True)

    _reset_outputs(paper_workdir)
    if precompile_manifest.status is not PrecompileStatus.OK:
        return _write_result(
            paper_workdir,
            _manifest_from_precompile(
                MaskStatus.PRECOMPILE_NOT_OK,
                precompile_manifest,
                table_sha256,
                message=(
                    f"precompile 的状态是 {precompile_manifest.status}，上游 fetch 判定源是 PDF 而非 LaTeX 源码，"
                    "没有可掩码的原文，走 degraded path。"
                    if precompile_manifest.fetch_status == FetchStatus.PDF_ONLY
                    else f"precompile 的状态是 {precompile_manifest.status}，不是 ok，先重跑 `tongtu stage precompile`。"
                ),
            ),
        )
    source_path = precompile_path(paper_workdir)
    if not source_path.is_file():
        return _write_result(
            paper_workdir,
            _manifest_from_precompile(
                MaskStatus.PRECOMPILE_MISSING,
                precompile_manifest,
                table_sha256,
                message=(
                    f"precompile 的状态是 ok，但 build/{PRECOMPILE_FILENAME} 不是文件，"
                    "没有可掩码的原文，先跑 `tongtu stage precompile`。"
                ),
            ),
        )
    if table_content is None:
        return _write_result(
            paper_workdir,
            _manifest_from_precompile(MaskStatus.MASK_FAILED, precompile_manifest, table_sha256, message=table_error),
        )

    try:
        manifest = _mask(paper_workdir, precompile_manifest, source_path, table_content, table_sha256)
    except Exception as error:  # 解码、词法遍历、自检与落盘的异常类型多样，统一转状态
        manifest = _manifest_from_precompile(
            MaskStatus.MASK_FAILED, precompile_manifest, table_sha256, message=manifests.describe_error(error)
        )
    return _write_result(paper_workdir, manifest)


def _mask(
    paper_workdir: workdir.Workdir,
    precompile_manifest: PrecompileManifest,
    source_path: Path,
    table_content: str,
    table_sha256: str,
) -> MaskManifest:
    """前置条件满足之后的主流程：解码、掩码、往返自检、写出两件产物。"""
    table = masking.parse_environment_table(table_content)
    source = source_path.read_bytes().decode(ENCODING)
    outcome = masking.mask_document(source, table)
    masking.verify_roundtrip(source, outcome)

    masked_bytes = outcome.masked.encode(ENCODING)
    blocks_bytes = _blocks_file(outcome).model_dump_json(indent=2).encode(ENCODING) + b"\n"
    masked_path(paper_workdir).write_bytes(masked_bytes)
    blocks_path(paper_workdir).write_bytes(blocks_bytes)
    return _manifest_from_precompile(
        MaskStatus.OK,
        precompile_manifest,
        table_sha256,
        masked_sha256=hashlib.sha256(masked_bytes).hexdigest(),
        masked_bytes=len(masked_bytes),
        precompile_chars=len(source),
        masked_chars=len(outcome.masked),
        blocks_sha256=hashlib.sha256(blocks_bytes).hexdigest(),
        environments=_environment_records(outcome),
        blocks_total=len(outcome.blocks),
        captions_total=len(outcome.captions),
        masked_chars_ratio=round(len(outcome.masked) / len(source), 4) if source else 0.0,
        warnings=list(outcome.warnings),
    )


# ------------------------------------------------------------------ 产物组装


def _blocks_file(outcome: masking.MaskOutcome) -> BlocksFile:
    """把词法层的两类记录转成 blocks.json 的 artifact model。"""
    return BlocksFile(
        blocks=[
            BlockRecord(
                id=block.id,
                category=block.category,
                environment=block.environment,
                decided_by=str(block.decided_by) if block.decided_by is not None else "",
                labels=list(block.labels),
                tex=block.tex,
                start=block.start,
                end=block.end,
                line=block.line,
            )
            for block in outcome.blocks
        ],
        captions=[
            CaptionRecord(
                id=caption.id,
                block_id=caption.block_id,
                kind=caption.kind,
                tex=caption.tex,
                masked_text=caption.masked_text,
            )
            for caption in outcome.captions
        ],
    )


def _environment_records(outcome: masking.MaskOutcome) -> dict[str, EnvironmentDecisionRecord]:
    """把环境分类结论一览转成 manifest 字段，按环境名排序。"""
    return {
        name: EnvironmentDecisionRecord(
            classification=decision.classification,
            category=str(decision.category) if decision.category is not None else "",
            decided_by=decision.decided_by,
            occurrences=decision.occurrences,
            blocks=decision.blocks,
        )
        for name, decision in sorted(outcome.environments.items())
    }


def _manifest_from_precompile(
    status: MaskStatus, precompile_manifest: PrecompileManifest, table_sha256: str, **fields: object
) -> MaskManifest:
    """组装 manifest：两个输入 hash 与上游两个状态一律转录，其余字段由调用处给出。"""
    return MaskManifest(
        status=status,
        precompile_sha256=precompile_manifest.precompile_sha256,
        environments_table_sha256=table_sha256,
        precompile_status=str(precompile_manifest.status),
        fetch_status=precompile_manifest.fetch_status,
        **fields,
    )


# ------------------------------------------------------------------ 分类表、跳过判定与落盘


def _read_environments_table() -> tuple[str | None, str, str]:
    """读环境分类表，返回（内容、内容的 sha256、读不到时的失败说明）。

    读不到时内容为 None、hash 为空串：这份表随包分发，缺了是安装或打包的问题，由调用方转
    `mask_failed`。内容解析（取值是否在词表里）由 masking 在掩码时做。
    """
    try:
        content = masking.ENVIRONMENTS_TABLE_PATH.read_bytes()
    except OSError as error:
        return (
            None,
            "",
            f"读不到环境分类表 {masking.ENVIRONMENTS_TABLE_PATH}（{manifests.describe_error(error)}），"
            "它随通途分发，确认安装完整。",
        )
    try:
        text = content.decode(ENCODING)
    except UnicodeDecodeError as error:
        return None, hashlib.sha256(content).hexdigest(), f"环境分类表不是合法 UTF-8：{manifests.describe_error(error)}"
    return text, hashlib.sha256(content).hexdigest(), ""


def _load_skippable_manifest(
    paper_workdir: workdir.Workdir, precompile_manifest: PrecompileManifest, table_sha256: str
) -> MaskManifest | None:
    """读已有 mask manifest；可解析、状态 ok、两个输入 hash 一致且两件产物都在，返回它，否则返回 None。"""
    manifest = manifests.load_manifest(paper_workdir.manifest_path(STAGE_NAME), MaskManifest)
    if manifest is None:
        return None
    if manifest.status is not MaskStatus.OK:
        return None
    if manifest.precompile_sha256 != precompile_manifest.precompile_sha256:
        return None
    if manifest.environments_table_sha256 != table_sha256:
        return None
    if not masked_path(paper_workdir).is_file():
        return None
    if not blocks_path(paper_workdir).is_file():
        return None
    return manifest


def precompile_path(paper_workdir: workdir.Workdir) -> Path:
    """上游 precompile 的输出路径，本阶段的输入。"""
    return paper_workdir.build / PRECOMPILE_FILENAME


def masked_path(paper_workdir: workdir.Workdir) -> Path:
    """掩码文本的路径；下游 survey、chunk、translate 取同一个文件。"""
    return paper_workdir.build / MASKED_FILENAME


def blocks_path(paper_workdir: workdir.Workdir) -> Path:
    """blocks.json 的路径；下游 compile 的 backfill 与 figures 取同一个文件。"""
    return paper_workdir.build / BLOCKS_FILENAME


def _reset_outputs(paper_workdir: workdir.Workdir) -> None:
    """删除两件产物：失败时不留上次的结果误导下游。"""
    masked_path(paper_workdir).unlink(missing_ok=True)
    blocks_path(paper_workdir).unlink(missing_ok=True)


def _write_result(paper_workdir: workdir.Workdir, manifest: MaskManifest) -> MaskResult:
    """写出 manifest 并组装返回值；除跳过外的每次执行（含失败）都经此处落盘。"""
    manifests.write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return MaskResult(manifest=manifest, workdir=paper_workdir, skipped=False)
