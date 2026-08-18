r"""编译层真实论文组：从 arXiv 拉取源码跑 fetch → flatten → precompile → mask。

覆盖的是自造论文覆盖不到的部分——真实 e-print 的下载形态与源码杂质：主文件名不叫
main.tex、多级 `\input`、正文直接使用 @-命令、生效与注释掉的 `\bibliography` 并存。清单与
各篇的定位见 examples/README.md，源码不入库。

本组需要网络，失败有一部分与代码无关（arXiv 可用性、论文出新版本），因此不设为合并必过，
在 CI 里走定时触发。下载失败按外部不可用跳过，其余失败照常判失败。

八篇中 `1701.06538` 与 `2412.19437` 首编失败、须经修复会话修到通过，属 LLM 层。`2412.19437`
在编译之前那部分判据与模型无关，因此本组另跑它到 flatten 为止（见文件末尾一节）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tongtu.artifacts.fetch import FetchStatus
from tongtu.artifacts.flatten import FlattenStatus
from tongtu.artifacts.mask import MaskStatus
from tongtu.artifacts.precompile import PrecompileStatus
from tongtu.cli import EXIT_PDF_ONLY

from .conftest import (
    MASKED_RATIO_RANGE,
    PipelineRun,
    StageResult,
    run_pipeline,
    run_stage,
    skip_if_download_failed,
    stage_status,
    strip_comments,
    workdir_root,
)

pytestmark = [pytest.mark.compile, pytest.mark.network]

#: 首编即通过的六篇：主文件判定、源码杂质与编译路径各覆盖一处，逐篇的专项判据见值。
#: `main_file` 取自 examples/README.md 的定位说明，其余键对应各阶段设计稿的验收条目。
PAPERS: dict[str, dict] = {
    "2002.05202": {"main_file": "main.tex", "bbl_inlined": True},
    "2106.04426": {"main_file": "neurips_2021.tex", "zero_block_environments": ("scope", "pgfonlayer")},
    "2409.19606": {"main_file": "iclr2025_conference.tex"},
    "2512.02556": {"runs_bibtex": True},
    "2512.24880": {"runs_bibtex": True, "flatten_warning": "makeatletter"},
    "2604.15804": {"runs_bibtex": True},
}

#: PAPERS 里允许出现的专项判据键。多数专项用例在判据缺失时直接返回（该篇不覆盖那一形态），
#: 键名拼错的后果因此是整条用例静默失效，登记表把它变成一条显式失败。
EXPECTATION_KEYS: frozenset[str] = frozenset(
    {"main_file", "bbl_inlined", "runs_bibtex", "flatten_warning", "zero_block_environments"}
)

#: PDF-only 套壳：源是 PDF 而非 LaTeX 源码，fetch 判定后沿链退 3，不进入后续阶段。
PDF_ONLY_PAPER = "1412.6980"

#: 首编失败因而整篇属 LLM 层的一篇，但它的 flatten 判据与编译无关：`main.tex` 里生效的
#: 与注释掉的 `\bibliography` 并存，是 bbl 内联注释判定的实测用例（examples/README.md）。
#: fetch 与 flatten 都不编译也不经 agent，这一条因此留在本组，只跑到 flatten。
COMMENTED_BIBLIOGRAPHY_PAPER = "2412.19437"


@pytest.fixture(scope="session", params=sorted(PAPERS))
def paper_run(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> PipelineRun:
    r"""一篇真实论文跑完四个阶段；同一篇的各用例复用同一次执行。

    fetch 允许命中缓存中已下载的源码，其后各阶段强制重算：CI 缓存的 key 取本文件的 hash，
    改的是别处的代码时缓存照旧命中，不强制重算的话编译不会真实发生。
    """
    arxiv_id = request.param
    workdir = workdir_root(tmp_path_factory) / arxiv_id
    run = run_pipeline(arxiv_id, arxiv_id, workdir, force_after_fetch=True)
    skip_if_download_failed(run)
    return run


def expectations(run: PipelineRun) -> dict:
    return PAPERS[run.paper]


def test_expectation_keys_are_registered() -> None:
    """判据键与登记表两端相符。

    一端：PAPERS 里出现未登记的键，说明键名拼错了——读它的那条用例在每篇论文上都取不到值，
    整条静默失效。另一端：登记表里的键没有任何论文声明，说明该专项用例现在一篇都不覆盖。
    """
    for arxiv_id, declared in PAPERS.items():
        unknown = sorted(set(declared) - EXPECTATION_KEYS)
        assert not unknown, f"{arxiv_id} 声明了未登记的判据键：{unknown}"
    unused = sorted(EXPECTATION_KEYS - {key for declared in PAPERS.values() for key in declared})
    assert not unused, f"登记表里的判据键没有任何论文声明，对应用例不覆盖任何一篇：{unused}"


def test_pipeline_completes(paper_run: PipelineRun) -> None:
    """四个阶段全部退 0。"""
    for stage, result in paper_run.results.items():
        assert result.returncode == 0, f"{paper_run.paper}：{stage} 退 {result.returncode}\n{result.stderr}"
    assert len(paper_run.results) == 4, f"{paper_run.paper}：有阶段未执行"


def test_fetch(paper_run: PipelineRun) -> None:
    """e-print 解包成源码树，逐文件 sha256 记入 manifest。"""
    manifest = paper_run.manifest("fetch")
    assert manifest.status is FetchStatus.OK
    assert manifest.files, "files 清单为空"
    assert manifest.tex_files, "没有识别出 .tex 文件"
    assert not manifest.rejected, f"解包时有成员被安全策略拒绝：{manifest.rejected}"


def test_fetch_rerun_skips(paper_run: PipelineRun) -> None:
    """已有 ok 结论时重跑 fetch 直接跳过，不再访问网络。

    docs/stages/fetch.md 的重跑语义：arXiv 对带版本号的 e-print 内容不可变，重新下载只会
    得到同样的字节。这条同时是 CI 缓存生效的前提——跳过失灵则每次定时执行都重新下载全部。
    """
    result = run_stage("fetch", paper_run.paper, paper_run.workdir)
    assert result.returncode == 0, result.stderr
    assert "跳过" in result.stdout, f"已有 ok 结论却重新执行了：{result.stdout}"


def test_flatten(paper_run: PipelineRun) -> None:
    r"""主文件判定与展开：产物含文档环境，无注释外的 `\input` 残留。"""
    manifest = paper_run.manifest("flatten")
    assert manifest.status is FlattenStatus.OK
    expected_main = expectations(paper_run).get("main_file")
    if expected_main:
        assert manifest.main_file == expected_main, "主文件判定与 examples/README.md 记录的不符"
    flat = paper_run.build_file("flat.tex").read_text(encoding="utf-8")
    assert r"\begin{document}" in flat
    assert r"\end{document}" in flat
    body = strip_comments(flat)
    assert r"\input" not in body, "展开后仍有注释外的 \\input"


def test_flatten_bibliography(paper_run: PipelineRun) -> None:
    r"""bbl 内联只对注释外的 `\bibliography` 生效，且至多内联一次。"""
    manifest = paper_run.manifest("flatten")
    flat = strip_comments(paper_run.build_file("flat.tex").read_text(encoding="utf-8"))
    if expectations(paper_run).get("bbl_inlined"):
        assert manifest.bbl_file, "该篇带预编译 bbl，应内联"
        assert flat.count(r"\begin{thebibliography}") == 1, "内联后 thebibliography 应恰出现一次"
    if expectations(paper_run).get("runs_bibtex"):
        assert not manifest.bbl_file, "该篇无 .bbl，不应内联"


def test_flatten_warnings_recorded(paper_run: PipelineRun) -> None:
    """latexpand 的警告如实记入 manifest 且不拦产出。"""
    expected = expectations(paper_run).get("flatten_warning")
    if not expected:
        return
    manifest = paper_run.manifest("flatten")
    assert manifest.status is FlattenStatus.OK, "警告不应改变状态"
    joined = " ".join(manifest.warnings).lower()
    assert expected in joined, f"没有记下预期的警告（{expected}）：{manifest.warnings}"


def test_precompile(paper_run: PipelineRun) -> None:
    """原文编译通过、页数大于 0；本组六篇均首编即过，不拉修复会话。"""
    manifest = paper_run.manifest("precompile")
    assert manifest.status is PrecompileStatus.OK
    assert manifest.pages > 0, "页数为 0，编译没有真正产出"
    assert manifest.pdf_bytes > 0
    assert manifest.fix_session is False, "本组六篇应首编即过；需要修复会话的两篇属 LLM 层"
    flat = paper_run.build_file("flat.tex").read_bytes()
    precompile = paper_run.build_file("precompile.tex").read_bytes()
    assert flat == precompile, "未经修复会话时 precompile.tex 应与 flat.tex 逐字节相同"


def test_precompile_runs_bibtex(paper_run: PipelineRun) -> None:
    """有 `.bib` 无 `.bbl` 的论文：latexmk 自动跑 bibtex，未定义引用计数为零。"""
    if not expectations(paper_run).get("runs_bibtex"):
        return
    manifest = paper_run.manifest("precompile")
    assert manifest.undefined_citations == 0, "bibtex 没跑通，引用未解析"
    assert manifest.undefined_references == 0, "存在未定义的 reference"


def test_mask(paper_run: PipelineRun) -> None:
    """掩码完成，两份产物齐全，计数一致。

    状态 ok 蕴含往返自检恒等：驱动器写出产物之前调 `verify_roundtrip`，不恒等即转 mask_failed。
    """
    manifest = paper_run.manifest("mask")
    assert manifest.status is MaskStatus.OK
    assert manifest.blocks_total > 0
    low, high = MASKED_RATIO_RANGE
    assert low <= manifest.masked_chars_ratio <= high, (
        f"{paper_run.paper}：掩码字符占比 {manifest.masked_chars_ratio:.3f} 落在合理区间 "
        f"[{low}, {high}] 之外，掩码范围可能整体走样"
    )
    blocks = paper_run.blocks()
    assert len(blocks.blocks) == manifest.blocks_total
    assert len(blocks.captions) == manifest.captions_total


def test_mask_nested_environments_not_blocked(paper_run: PipelineRun) -> None:
    """嵌在已掩 block 内部的环境枚举得到但不成块，成块数为 0。"""
    expected = expectations(paper_run).get("zero_block_environments")
    if not expected:
        return
    environments = paper_run.manifest("mask").environments
    for name in expected:
        assert name in environments, f"{name} 未出现在环境分类结论里"
        assert environments[name].occurrences > 0, f"{name} 应被枚举到"
        assert environments[name].blocks == 0, f"{name} 嵌在已掩 block 内部，不应成块"


# ------------------------------------------------------------------ PDF-only 分流


@pytest.fixture(scope="session")
def pdf_only_fetch(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, StageResult]:
    """PDF-only 套壳论文跑一次 fetch，两个用例共享这一次执行的结果。

    fetch 放在 fixture 里而不是第一个用例里：否则第二个用例依赖第一个的副作用，单独跑或
    重排顺序时它会静默跳过——无覆盖而不报警。
    """
    workdir = workdir_root(tmp_path_factory) / PDF_ONLY_PAPER
    return workdir, run_stage("fetch", PDF_ONLY_PAPER, workdir, force=True)


def require_pdf_only(workdir: Path) -> None:
    """确认 fetch 确实判成了 pdf_only，否则区分「外部不可用」与「判定错了」。

    判据取 manifest 的 `status` 而不是「manifest 文件在不在」：下载失败时驱动器同样落盘
    manifest（`tongtu/stages/fetch.py` 的 download_failed 分支），按文件存在与否判会让
    arXiv 不可用变成硬失败。反过来，状态既不是 pdf_only 也不是 download_failed，说明这篇
    本该判成 PDF 套壳却没有，那是缺陷，不跳过。
    """
    status = stage_status(workdir, "fetch")
    if status is None:
        pytest.skip(f"{PDF_ONLY_PAPER}：fetch 未产出可读 manifest，按外部不可用处理")
    if status == "download_failed":
        pytest.skip(f"{PDF_ONLY_PAPER}：e-print 下载失败（arXiv 不可用），本组不设为合并必过")
    assert status == "pdf_only", f"{PDF_ONLY_PAPER} 是 PDF 套壳，fetch 却判成 {status}"


def test_pdf_only_fetch_exits_three(pdf_only_fetch: tuple[Path, StageResult]) -> None:
    """源是 PDF 套壳时 fetch 退 3：这是分支不是错误，调度方据此改道 degraded path。"""
    workdir, result = pdf_only_fetch
    require_pdf_only(workdir)
    assert result.returncode == EXIT_PDF_ONLY, f"PDF-only 应退 3，实际退 {result.returncode}"


def test_pdf_only_propagates_to_flatten(pdf_only_fetch: tuple[Path, StageResult]) -> None:
    """flatten 沿链退 3：跨子命令同码同义，调用方不必解析输出即可分流。"""
    workdir, _ = pdf_only_fetch
    require_pdf_only(workdir)
    result = run_stage("flatten", PDF_ONLY_PAPER, workdir, force=True)
    assert result.returncode == EXIT_PDF_ONLY, f"flatten 应沿链退 3，实际退 {result.returncode}"


# ------------------------------------------------------------------ 注释掉的 \bibliography


@pytest.fixture(scope="session")
def commented_bibliography_run(tmp_path_factory: pytest.TempPathFactory) -> PipelineRun:
    r"""`2412.19437` 只跑到 flatten。

    这一篇首编失败、须经修复会话修到通过，整篇属 LLM 层；但它承担的 flatten 判据（注释掉
    的 `\bibliography` 不参与内联）在编译之前就能判定，随整篇推到 LLM 层会白丢一处覆盖。
    """
    paper = COMMENTED_BIBLIOGRAPHY_PAPER
    workdir = workdir_root(tmp_path_factory) / paper
    run = run_pipeline(paper, paper, workdir, force_after_fetch=True, stages=("fetch", "flatten"))
    skip_if_download_failed(run)
    return run


def test_commented_bibliography_is_not_inlined(commented_bibliography_run: PipelineRun) -> None:
    r"""生效的 `\bibliography` 内联一次，注释掉的那行不触发第二次内联。"""
    run = commented_bibliography_run
    for stage, result in run.results.items():
        assert result.returncode == 0, f"{run.paper}：{stage} 退 {result.returncode}\n{result.stderr}"
    manifest = run.manifest("flatten")
    assert manifest.status is FlattenStatus.OK
    assert manifest.bbl_file, "该篇带预编译 bbl，应内联"

    raw = run.build_file("flat.tex").read_text(encoding="utf-8")
    body = strip_comments(raw)
    assert body.count(r"\begin{thebibliography}") == 1, "内联后 thebibliography 应恰出现一次"
    assert r"\bibliography{" not in body, r"生效的 \bibliography 应已被 bbl 内容替换"

    commented_out = [line for line in raw.splitlines() if r"\bibliography{" in line]
    assert commented_out, (
        r"这一篇的定位是注释掉的 \bibliography 与生效的并存；源码里已找不到注释掉的那行，"
        "用例不再覆盖它所声称的形态，需回到 examples/README.md 重新选篇"
    )
