"""artifact model 的写入读回往返，以及 `load_manifest` 读不到时的返回约定。

各 manifest 的字段级定义在 `tongtu/artifacts/` 的 model，读写都经 `tongtu.manifests` 的两个
函数。这里判定的是：写出去的字段与取值读回来不变；读不到或不合 schema 时返回 None 而不是
抛异常——驱动器按后者转对应的失败状态，改成抛异常会让各阶段的前置条件处理失效。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from tongtu import manifests
from tongtu.artifacts.fetch import FetchManifest, FetchStatus
from tongtu.artifacts.flatten import FlattenManifest, FlattenStatus
from tongtu.artifacts.mask import (
    BlockRecord,
    BlocksFile,
    CaptionRecord,
    EnvironmentDecisionRecord,
    MaskManifest,
    MaskStatus,
)
from tongtu.artifacts.precompile import PrecompileManifest, PrecompileStatus
from tongtu.masking import BlockCategory, CaptionKind, DecidedBy, EnvironmentClass


def fetch_manifest() -> FetchManifest:
    return FetchManifest(
        status=FetchStatus.OK,
        source="2002.05202",
        kind="tar.gz",
        url="https://arxiv.org/e-print/2002.05202",
        payload_sha256="a" * 64,
        payload_bytes=8192,
        files={"main.tex": "b" * 64, "figures/a.png": "c" * 64},
        tex_files=["main.tex"],
        tex_chars=16424,
        rejected=["../escape.tex"],
        warnings=["一条警告"],
        message="",
    )


def flatten_manifest() -> FlattenManifest:
    return FlattenManifest(
        status=FlattenStatus.OK,
        main_file="main.tex",
        candidates=["main.tex"],
        fetch_files_sha256="d" * 64,
        fetch_status=FetchStatus.OK.value,
        bbl_file="main.bbl",
        flat_sha256="e" * 64,
        flat_bytes=20480,
        command=["latexpand", "--keep-comments", "--fatal", "main.tex"],
        warnings=[],
        message="",
    )


def precompile_manifest() -> PrecompileManifest:
    return PrecompileManifest(
        status=PrecompileStatus.OK,
        flat_sha256="e" * 64,
        fetch_files_sha256="d" * 64,
        precompile_sha256="f" * 64,
        precompile_bytes=20480,
        flatten_status=FlattenStatus.OK.value,
        fetch_status=FetchStatus.OK.value,
        command=["latexmk", "-xelatex", "-interaction=nonstopmode", "flat.tex"],
        pages=9,
        pdf_bytes=271052,
        overfull_hboxes=3,
        undefined_references=0,
        undefined_citations=0,
        missing_characters=0,
        duration_seconds=12.5,
        fix_session=True,
        session_stop_reason="finished",
        session_model="claude-opus-5",
        session_duration_seconds=88.25,
        changed_files=["figures/missing.eps"],
        warnings=[],
        message="",
    )


def mask_manifest() -> MaskManifest:
    return MaskManifest(
        status=MaskStatus.OK,
        precompile_sha256="f" * 64,
        environments_table_sha256="0" * 64,
        masked_sha256="1" * 64,
        masked_bytes=14000,
        precompile_chars=20000,
        masked_chars=13000,
        blocks_sha256="2" * 64,
        environments={
            "widetext": EnvironmentDecisionRecord(
                classification=EnvironmentClass.NON_TRANSLATABLE,
                category=BlockCategory.UNKNOWN.value,
                decided_by=DecidedBy.DEFAULT,
                occurrences=2,
                blocks=2,
            ),
            "itemize": EnvironmentDecisionRecord(
                classification=EnvironmentClass.TEXT,
                decided_by=DecidedBy.TABLE,
                occurrences=5,
                blocks=0,
            ),
        },
        blocks_total=14,
        captions_total=2,
        masked_chars_ratio=0.6288,
        precompile_status=PrecompileStatus.OK.value,
        fetch_status=FetchStatus.OK.value,
        warnings=[],
        message="",
    )


def blocks_file() -> BlocksFile:
    return BlocksFile(
        blocks=[
            BlockRecord(
                id="BLK-0",
                category=BlockCategory.PREAMBLE,
                environment="",
                decided_by="",
                labels=[],
                tex="\\documentclass{article}\n\\begin{document}",
                start=0,
                end=42,
                line=1,
            ),
            BlockRecord(
                id="BLK-1",
                category=BlockCategory.FIGURE,
                environment="figure",
                decided_by=DecidedBy.TABLE.value,
                labels=["fig:a"],
                tex="\\begin{figure}\\caption{⟦CAP-0⟧}\\end{figure}",
                start=100,
                end=160,
                line=8,
            ),
        ],
        captions=[
            CaptionRecord(
                id="CAP-0",
                block_id="BLK-1",
                kind=CaptionKind.CAPTION,
                tex="A figure caption",
                masked_text="A figure caption",
            )
        ],
    )


ROUNDTRIP_CASES = [
    pytest.param(fetch_manifest(), FetchManifest, id="fetch"),
    pytest.param(flatten_manifest(), FlattenManifest, id="flatten"),
    pytest.param(precompile_manifest(), PrecompileManifest, id="precompile"),
    pytest.param(mask_manifest(), MaskManifest, id="mask"),
    pytest.param(blocks_file(), BlocksFile, id="blocks"),
]


@pytest.mark.parametrize(("manifest", "model_cls"), ROUNDTRIP_CASES)
def test_manifest_roundtrip(manifest: BaseModel, model_cls: type[BaseModel], tmp_path: Path) -> None:
    """写出的 manifest 读回来字段与取值不变。"""
    path = tmp_path / "manifest.json"
    manifests.write_manifest(path, manifest)
    loaded = manifests.load_manifest(path, model_cls)
    assert loaded == manifest


@pytest.mark.parametrize(("manifest", "model_cls"), ROUNDTRIP_CASES)
def test_manifest_written_with_trailing_newline(
    manifest: BaseModel, model_cls: type[BaseModel], tmp_path: Path
) -> None:
    """写出的 JSON 缩进两格并以换行结尾，供人直接阅读与 diff。"""
    path = tmp_path / "nested" / "manifest.json"
    manifests.write_manifest(path, manifest)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "\n  " in text


def test_load_manifest_returns_none_when_absent(tmp_path: Path) -> None:
    """文件不存在时返回 None：驱动器据此转「上游没跑」的状态。"""
    assert manifests.load_manifest(tmp_path / "absent.json", FetchManifest) is None


def test_load_manifest_returns_none_on_schema_mismatch(tmp_path: Path) -> None:
    """内容不合 schema 时返回 None 而不是抛异常，与文件缺失同一处置。"""
    path = tmp_path / "manifest.json"
    path.write_text('{"stage": "fetch"}', encoding="utf-8")
    assert manifests.load_manifest(path, FetchManifest) is None


def test_load_manifest_returns_none_on_invalid_json(tmp_path: Path) -> None:
    """内容不是 JSON 时同样返回 None。"""
    path = tmp_path / "manifest.json"
    path.write_text("not json at all", encoding="utf-8")
    assert manifests.load_manifest(path, FetchManifest) is None


def test_status_values_are_lowercase_strings() -> None:
    """状态枚举序列化为小写字符串：manifest 的 `status` 是调用方分流的唯一依据。"""
    for status_enum in (FetchStatus, FlattenStatus, PrecompileStatus, MaskStatus):
        for member in status_enum:
            assert member.value == member.value.lower()
            assert isinstance(member.value, str)


def test_describe_error_format() -> None:
    """异常记进 manifest 的 message 时统一格式化成「类型名：信息」。"""
    assert manifests.describe_error(ValueError("坏输入")) == "ValueError：坏输入"
