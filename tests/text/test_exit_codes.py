"""各阶段状态到退出码的映射，对照 docs/CLI.md 的退出码表。

判定直接调 `tongtu.cli` 的映射函数，不走端到端执行：要覆盖每个状态就得制造对应的失败
条件（下载失败、解包失败、编译超时），成本高且结果不稳定，而映射本身是纯函数。

每个状态枚举都做穷举比对——新增状态若没在这里给出期望退出码，用例失败。退出码是 wenshu
容器调度侧不解析输出就要据以分流的契约，`pdf_only` 沿链退 3 尤其容易在改动错误处理时被
无声破坏。
"""

from __future__ import annotations

import pytest

from tongtu.artifacts.fetch import FetchStatus
from tongtu.artifacts.flatten import FlattenManifest, FlattenStatus
from tongtu.artifacts.mask import MaskManifest, MaskStatus
from tongtu.artifacts.precompile import PrecompileManifest, PrecompileStatus
from tongtu.cli import (
    EXIT_FAILURE,
    EXIT_PDF_ONLY,
    _fetch_exit_code,
    _flatten_exit_code,
    _mask_exit_code,
    _precompile_exit_code,
    _upstream_exit_code,
)

#: fetch 自身判定出源是 PDF，退业务分支段的 3；其余失败态退 1。
FETCH_EXPECTED: dict[FetchStatus, int] = {
    FetchStatus.OK: 0,
    FetchStatus.PDF_ONLY: EXIT_PDF_ONLY,
    FetchStatus.EMPTY: EXIT_FAILURE,
    FetchStatus.DOWNLOAD_FAILED: EXIT_FAILURE,
    FetchStatus.UNPACK_FAILED: EXIT_FAILURE,
    FetchStatus.SOURCE_MISSING: EXIT_FAILURE,
}

#: 下游三个阶段：只有「上游失败且失败源头是 PDF-only」才沿链退 3。
FLATTEN_EXPECTED: dict[FlattenStatus, int] = {
    FlattenStatus.OK: 0,
    FlattenStatus.FETCH_MISSING: EXIT_FAILURE,
    FlattenStatus.FETCH_NOT_OK: EXIT_FAILURE,  # fetch_status 非 pdf_only 时
    FlattenStatus.MAIN_NOT_FOUND: EXIT_FAILURE,
    FlattenStatus.MAIN_AMBIGUOUS: EXIT_FAILURE,
    FlattenStatus.EXPAND_FAILED: EXIT_FAILURE,
}

PRECOMPILE_EXPECTED: dict[PrecompileStatus, int] = {
    PrecompileStatus.OK: 0,
    PrecompileStatus.FLATTEN_MISSING: EXIT_FAILURE,
    PrecompileStatus.FLATTEN_NOT_OK: EXIT_FAILURE,  # fetch_status 非 pdf_only 时
    PrecompileStatus.COMPILE_FAILED: EXIT_FAILURE,
}

MASK_EXPECTED: dict[MaskStatus, int] = {
    MaskStatus.OK: 0,
    MaskStatus.PRECOMPILE_MISSING: EXIT_FAILURE,
    MaskStatus.PRECOMPILE_NOT_OK: EXIT_FAILURE,  # fetch_status 非 pdf_only 时
    MaskStatus.MASK_FAILED: EXIT_FAILURE,
}


def test_expectations_cover_every_status() -> None:
    """四个状态枚举的每个成员都在期望表里；新增状态时此处先失败。"""
    assert set(FETCH_EXPECTED) == set(FetchStatus)
    assert set(FLATTEN_EXPECTED) == set(FlattenStatus)
    assert set(PRECOMPILE_EXPECTED) == set(PrecompileStatus)
    assert set(MASK_EXPECTED) == set(MaskStatus)


@pytest.mark.parametrize(("status", "expected"), list(FETCH_EXPECTED.items()), ids=lambda value: str(value))
def test_fetch_exit_code(status: FetchStatus, expected: int) -> None:
    assert _fetch_exit_code(status) == expected


@pytest.mark.parametrize(("status", "expected"), list(FLATTEN_EXPECTED.items()), ids=lambda value: str(value))
def test_flatten_exit_code(status: FlattenStatus, expected: int) -> None:
    manifest = FlattenManifest(status=status, fetch_status=FetchStatus.OK.value)
    assert _flatten_exit_code(manifest) == expected


@pytest.mark.parametrize(("status", "expected"), list(PRECOMPILE_EXPECTED.items()), ids=lambda value: str(value))
def test_precompile_exit_code(status: PrecompileStatus, expected: int) -> None:
    manifest = PrecompileManifest(status=status, fetch_status=FetchStatus.OK.value)
    assert _precompile_exit_code(manifest) == expected


@pytest.mark.parametrize(("status", "expected"), list(MASK_EXPECTED.items()), ids=lambda value: str(value))
def test_mask_exit_code(status: MaskStatus, expected: int) -> None:
    manifest = MaskManifest(status=status, fetch_status=FetchStatus.OK.value)
    assert _mask_exit_code(manifest) == expected


# ------------------------------------------------- PDF-only 沿链退 3（跨子命令同码同义）


def test_flatten_propagates_pdf_only() -> None:
    """flatten 因上游判定为 PDF 而失败时退 3，调用方据此改道 degraded path。"""
    manifest = FlattenManifest(status=FlattenStatus.FETCH_NOT_OK, fetch_status=FetchStatus.PDF_ONLY.value)
    assert _flatten_exit_code(manifest) == EXIT_PDF_ONLY


def test_precompile_propagates_pdf_only() -> None:
    manifest = PrecompileManifest(status=PrecompileStatus.FLATTEN_NOT_OK, fetch_status=FetchStatus.PDF_ONLY.value)
    assert _precompile_exit_code(manifest) == EXIT_PDF_ONLY


def test_mask_propagates_pdf_only() -> None:
    manifest = MaskManifest(status=MaskStatus.PRECOMPILE_NOT_OK, fetch_status=FetchStatus.PDF_ONLY.value)
    assert _mask_exit_code(manifest) == EXIT_PDF_ONLY


@pytest.mark.parametrize(
    ("status", "manifest_cls"),
    [
        (FlattenStatus.EXPAND_FAILED, FlattenManifest),
        (PrecompileStatus.COMPILE_FAILED, PrecompileManifest),
        (MaskStatus.MASK_FAILED, MaskManifest),
    ],
    ids=["flatten", "precompile", "mask"],
)
def test_own_failure_does_not_propagate_pdf_only(status, manifest_cls) -> None:
    """本阶段自身失败时不退 3，即使 fetch 记录的是 pdf_only。

    退 3 的含义是「源是 PDF 而非 LaTeX 源码，请改道」，本阶段自己编不过或掩不动是另一回事，
    混用会让调度方把真实失败当成分支处理。
    """
    manifest = manifest_cls(status=status, fetch_status=FetchStatus.PDF_ONLY.value)
    exit_code = {
        FlattenManifest: _flatten_exit_code,
        PrecompileManifest: _precompile_exit_code,
        MaskManifest: _mask_exit_code,
    }[manifest_cls](manifest)
    assert exit_code == EXIT_FAILURE


# ------------------------------------------------- 共用映射函数


@pytest.mark.parametrize(
    ("ok", "pdf_only_chain", "expected"),
    [
        (True, False, 0),
        (True, True, 0),  # 成功优先：ok 时不看 pdf_only_chain
        (False, True, EXIT_PDF_ONLY),
        (False, False, EXIT_FAILURE),
    ],
)
def test_upstream_exit_code(ok: bool, pdf_only_chain: bool, expected: int) -> None:
    assert _upstream_exit_code(ok=ok, pdf_only_chain=pdf_only_chain) == expected
