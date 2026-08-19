r"""chunk 阶段驱动器：把掩码文本按章节结构切成大小受控的 chunk 序列。

chunk 只读 `build/`、只写 `build/`：纯文本变换，不访问网络、不编译，也不拉起 agent。上游
结论与输入 hash 从 mask manifest 装载。扫描、定级、切点与聚合的实现在 `tongtu/chunking.py`，
本模块管前置条件、跳过判定、自检编排与落盘。chunk 不消费 survey 的产物，`stage chunk` 不要求
survey 已跑。

前置条件：mask manifest 缺失或不可解析，或它的状态是 ok 但 `build/masked.tex` 不在 → 状态
`mask_missing`；mask 的状态不是 ok → 状态 `mask_not_ok`，本次读到的 mask 状态与它记录的
fetch 状态转录进 manifest。前置条件不满足同样写 chunk manifest：驱动器不向调用方抛栈，每次
执行的结论都落盘。

出口判据同时成立才是 ok：扫描无错、定级兜底硬判据通过（两者由 chunking 抛 `ChunkError`）；
全部 chunk 按序拼接逐字符等于 `masked.tex`；每个 chunk 段落数至少 1；chunk 文件与 manifest
落盘并通过 artifact model 校验。自检在落盘之前跑完，失败不留半份产物。超过 `SPLIT_ABOVE` 的
chunk 不阻断，逐个记进 manifest 的 `warnings`：它们已下分到不可再分的单元，是分块算法接受的
终态，但 translate 会在这些 chunk 上撞到长生成。

重跑语义：输入 hash 是 `masked_sha256`（从 mask manifest 转录），另记 `split_above` /
`merge_below` / `chars_per_token` 三个配置值，跳过判定要求它们与当前模块常量一致——校准期
这几个数会改，不参与判定的话改完常量旧分块会静默留存。已有 chunk manifest 可解析、状态 ok、
输入 hash 与三个配置值一致、清单内全部 chunk 文件存在 → 跳过；失败状态不跳过；
`force` 无视已有结论。每次非跳过的执行开始先整目录删除 `build/chunks/`，失败时不留上次的产物
误导下游。
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import chunking, manifests, workdir
from ..artifacts.chunk import ChunkManifest, ChunkRecord, ChunkStatus
from ..artifacts.fetch import FetchStatus
from ..artifacts.mask import MaskManifest, MaskStatus
from .mask import MASKED_FILENAME, masked_path
from .mask import STAGE_NAME as MASK_STAGE_NAME

#: 阶段名，也是 stage manifest 的文件名主干。
STAGE_NAME = "chunk"

#: chunk 文件所在目录，位于 build/ 之下；只存本阶段产物，故整目录删除是安全的。
CHUNKS_DIRNAME = "chunks"

#: chunk 文件的扩展名。
CHUNK_SUFFIX = ".tex"

#: 产物文本的编码；读写都用它。
ENCODING = "utf-8"


@dataclass(frozen=True)
class ChunkResult:
    """驱动器的返回值：manifest、工作目录与是否命中跳过。"""

    manifest: ChunkManifest
    workdir: workdir.Workdir
    skipped: bool


# ------------------------------------------------------------------ 阶段驱动器


def chunk(
    workdir_name: str | None = None,
    workdir_path: Path | None = None,
    *,
    force: bool = False,
) -> ChunkResult:
    """装载 mask 结论、分块并自检，写出 build/chunks/ 下的 chunk 文件与 manifest。

    `workdir_name` 是工作目录名（arXiv 编号，或本地源码目录的 basename），`workdir_path`
    直接给出论文工作目录本身并覆盖前者。chunk 不访问网络，也不读源目录，两个参数只用来定位
    工作目录。`force` 无视已有结论重新执行。
    """
    paper_workdir = workdir.Workdir(workdir.resolve(workdir_name, workdir_path))
    paper_workdir.create()  # 前置条件不满足时也要写 manifest，先确保四区存在

    # 上游 mask manifest 读不到或不可解析都转 mask_missing，两种情形对本阶段含义相同。
    mask_manifest = manifests.load_manifest(paper_workdir.manifest_path(MASK_STAGE_NAME), MaskManifest)
    if mask_manifest is None:
        # 输入 hash 从 mask manifest 转录，读不到就无从做跳过判定，直接给结论。
        _reset_outputs(paper_workdir)
        return _write_result(
            paper_workdir,
            ChunkManifest(
                status=ChunkStatus.MASK_MISSING,
                message="读不到 build/manifests/mask.json 或它不可解析，先跑 `tongtu stage mask`。",
            ),
        )

    if not force:
        existing = _load_skippable_manifest(paper_workdir, mask_manifest)
        if existing is not None:
            return ChunkResult(manifest=existing, workdir=paper_workdir, skipped=True)

    _reset_outputs(paper_workdir)
    if mask_manifest.status is not MaskStatus.OK:
        return _write_result(
            paper_workdir,
            _manifest_from_mask(
                ChunkStatus.MASK_NOT_OK,
                mask_manifest,
                message=(
                    f"mask 的状态是 {mask_manifest.status}，上游 fetch 判定源是 PDF 而非 LaTeX 源码，"
                    "没有可分块的掩码文本，走 degraded path。"
                    if mask_manifest.fetch_status == FetchStatus.PDF_ONLY
                    else f"mask 的状态是 {mask_manifest.status}，不是 ok，先重跑 `tongtu stage mask`。"
                ),
            ),
        )
    source_path = masked_path(paper_workdir)
    if not source_path.is_file():
        return _write_result(
            paper_workdir,
            _manifest_from_mask(
                ChunkStatus.MASK_MISSING,
                mask_manifest,
                message=(
                    f"mask 的状态是 ok，但 build/{MASKED_FILENAME} 不是文件，"
                    "没有可分块的掩码文本，先跑 `tongtu stage mask`。"
                ),
            ),
        )

    try:
        manifest = _chunk(paper_workdir, mask_manifest, source_path)
    except Exception as error:  # 扫描、定级、自检与落盘的异常类型多样，统一转状态
        manifest = _manifest_from_mask(ChunkStatus.CHUNK_FAILED, mask_manifest, message=manifests.describe_error(error))
    return _write_result(paper_workdir, manifest)


def _chunk(paper_workdir: workdir.Workdir, mask_manifest: MaskManifest, source_path: Path) -> ChunkManifest:
    """前置条件满足之后的主流程：分块、自检、写出 chunk 文件。"""
    masked = source_path.read_bytes().decode(ENCODING)
    outcome = chunking.split_document(masked)

    contents = [masked[item.start : item.end] for item in outcome.chunks]
    _verify(masked, outcome.chunks, contents)
    records = [
        _record(f"c{index:03d}", item, body)
        for index, (item, body) in enumerate(zip(outcome.chunks, contents, strict=True))
    ]

    directory = chunks_dir(paper_workdir)
    directory.mkdir(parents=True, exist_ok=True)
    for record, body in zip(records, contents, strict=True):
        chunk_path(paper_workdir, record.id).write_bytes(body.encode(ENCODING))
    return _manifest_from_mask(
        ChunkStatus.OK,
        mask_manifest,
        warnings=_oversized_warnings(records),
        chunks_sha256=_chunks_sha256(records),
        chunks=records,
        chunks_total=len(records),
        heading_level=outcome.heading_level,
        transparent_environments=list(outcome.transparent_environments),
        appendix_source=outcome.appendix_source,
    )


def _verify(masked: str, chunks: tuple[chunking.Chunk, ...], contents: list[str]) -> None:
    """出口自检：至少切出一个 chunk、按序拼接逐字符等于掩码文本、每个 chunk 段落数至少 1。

    拼接恒等由构造成立，自检兜的是实现缺陷。一个 chunk 都没有时状态记 chunk_failed：空的
    掩码文本是上游的问题，报 ok 会让下游把「没有内容」读成「翻完了」。
    """
    if not contents:
        raise chunking.ChunkError(f"masked.tex 有 {len(masked)} 字符，却一个 chunk 也没切出")
    joined = "".join(contents)
    if joined != masked:
        raise chunking.ChunkError(
            f"全部 chunk 拼接共 {len(joined)} 字符，masked.tex 有 {len(masked)} 字符，两者不等，切片实现有误"
        )
    empty = [index for index, body in enumerate(contents) if chunking.count_paragraphs(body) < 1]
    if empty:
        starts = "、".join(str(chunks[index].start) for index in empty)
        raise chunking.ChunkError(f"有 {len(empty)} 个 chunk 一个段落都没有（起始偏移 {starts}），切分实现有误")


# ------------------------------------------------------------------ 产物组装


def _record(chunk_id: str, item: chunking.Chunk, body: str) -> ChunkRecord:
    """把分块层的一个 chunk 转成 manifest 的记录。"""
    return ChunkRecord(
        id=chunk_id,
        start=item.start,
        end=item.end,
        sha256=hashlib.sha256(body.encode(ENCODING)).hexdigest(),
        token_estimate=chunking.estimate_tokens(body),
        paragraphs=chunking.count_paragraphs(body),
        part=item.part,
        headings=list(item.headings),
        internal_cuts=list(item.internal_cuts),
        translatable_chars=chunking.translatable_chars(body),
    )


def _oversized_warnings(records: list[ChunkRecord]) -> list[str]:
    """超过下分线的 chunk 各记一条：它们已下分到不可再分的单元，translate 会在这里撞上长生成。"""
    return [
        f"{record.id} 估算 {record.token_estimate} token，超过下分线 {chunking.SPLIT_ABOVE}："
        "已下分到不可再分的单元（不透明环境体或单个段落），不再切开"
        for record in records
        if record.token_estimate > chunking.SPLIT_ABOVE
    ]


def _chunks_sha256(records: list[ChunkRecord]) -> str:
    """输出 hash：按文档序连接各 chunk 文件的 sha256 十六进制串再取 sha256。"""
    return hashlib.sha256("".join(record.sha256 for record in records).encode("ascii")).hexdigest()


def _manifest_from_mask(status: ChunkStatus, mask_manifest: MaskManifest, **fields: object) -> ChunkManifest:
    """组装 manifest：输入 hash、三个配置值与上游两个状态一律转录，其余字段由调用处给出。"""
    return ChunkManifest(
        status=status,
        masked_sha256=mask_manifest.masked_sha256,
        split_above=chunking.SPLIT_ABOVE,
        merge_below=chunking.MERGE_BELOW,
        chars_per_token=chunking.CHARS_PER_TOKEN,
        mask_status=str(mask_manifest.status),
        fetch_status=mask_manifest.fetch_status,
        **fields,
    )


# ------------------------------------------------------------------ 跳过判定与落盘


def _load_skippable_manifest(paper_workdir: workdir.Workdir, mask_manifest: MaskManifest) -> ChunkManifest | None:
    """读已有 chunk manifest；可解析、状态 ok、输入 hash 与三个配置值一致且全部 chunk 文件在，返回它。"""
    manifest = manifests.load_manifest(paper_workdir.manifest_path(STAGE_NAME), ChunkManifest)
    if manifest is None:
        return None
    if manifest.status is not ChunkStatus.OK:
        return None
    if manifest.masked_sha256 != mask_manifest.masked_sha256:
        return None
    if (manifest.split_above, manifest.merge_below, manifest.chars_per_token) != (
        chunking.SPLIT_ABOVE,
        chunking.MERGE_BELOW,
        chunking.CHARS_PER_TOKEN,
    ):
        return None
    if not all(chunk_path(paper_workdir, record.id).is_file() for record in manifest.chunks):
        return None
    return manifest


def chunks_dir(paper_workdir: workdir.Workdir) -> Path:
    """chunk 文件所在目录；下游 translate 取同一个目录。"""
    return paper_workdir.build / CHUNKS_DIRNAME


def chunk_path(paper_workdir: workdir.Workdir, chunk_id: str) -> Path:
    """一个 chunk 文件的路径。"""
    return chunks_dir(paper_workdir) / f"{chunk_id}{CHUNK_SUFFIX}"


def _reset_outputs(paper_workdir: workdir.Workdir) -> None:
    """整目录删除 build/chunks/：失败时不留上次的产物误导下游。"""
    shutil.rmtree(chunks_dir(paper_workdir), ignore_errors=True)


def _write_result(paper_workdir: workdir.Workdir, manifest: ChunkManifest) -> ChunkResult:
    """写出 manifest 并组装返回值；除跳过外的每次执行（含失败）都经此处落盘。"""
    manifests.write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return ChunkResult(manifest=manifest, workdir=paper_workdir, skipped=False)
