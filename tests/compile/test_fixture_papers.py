r"""编译层自造论文组：`examples/papers/` 三篇跑 fetch → flatten → precompile → mask。

断言取自各阶段设计稿的「验收与试跑对象」一节，不另行设计用例：
docs/stages/fetch.md、flatten.md、precompile.md、mask.md。真实论文那部分在
test_real_papers.py，两者依赖不同（本组无网络依赖），因此分属不同 CI 作业。
"""

from __future__ import annotations

import re

import pytest

from tongtu.artifacts.fetch import FetchStatus
from tongtu.artifacts.flatten import FlattenStatus
from tongtu.artifacts.mask import MaskStatus
from tongtu.artifacts.precompile import PrecompileStatus
from tongtu.masking import DecidedBy, EnvironmentClass

from ..conftest import FIXTURE_PAPERS, PAPERS_DIR
from .conftest import MASKED_RATIO_RANGE, PipelineRun, run_pipeline, run_stage, strip_comments, workdir_root

pytestmark = pytest.mark.compile

#: 注释外的 `\caption`：行首到第一个未转义 `%` 之前的部分才算数。
CAPTION_RE = re.compile(r"\\caption\b")

#: 掩码文本里的 block placeholder。
BLOCK_TOKEN_RE = re.compile(r"⟦BLK-[0-9]+⟧")

#: 页数与 MANIFEST 目测值的容差。目测值只用于「规模没跑偏」的粗判（examples/README.md），
#: 不是精确期望：实测三篇分别差 +1 / -1 / -1 页。
PAGES_TOLERANCE = 2

#: 分类表外的环境，按论文登记。集中成表是为了让下面的元校验能确认每条都真的被断言过——
#: 键名写错时对应断言会静默失效，那正是这类「按参数分流」的用例最容易出的问题。
UNKNOWN_ENVIRONMENT_BY_PAPER: dict[str, str] = {"revtex": "widetext", "conference": "sidenote"}

#: 正文里 metadata 命令成块的论文：revtex 把 \title / \author 写在 \begin{document} 之后。
METADATA_BLOCK_PAPERS: tuple[str, ...] = ("revtex",)

#: 由源码声明（\newtheorem / \newenvironment）裁定分类的论文。
DECLARATION_DRIVEN_PAPERS: tuple[str, ...] = ("article",)


@pytest.fixture(scope="session", params=FIXTURE_PAPERS)
def fixture_run(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> PipelineRun:
    """一篇自造论文跑完四个阶段；同一篇的各用例复用同一次执行。"""
    paper = request.param
    workdir = workdir_root(tmp_path_factory) / f"fixture-{paper}"
    return run_pipeline(paper, str(PAPERS_DIR / paper), workdir)


def test_pipeline_completes(fixture_run: PipelineRun) -> None:
    """四个阶段全部退 0。"""
    for stage, result in fixture_run.results.items():
        assert result.returncode == 0, f"{fixture_run.paper}：{stage} 退 {result.returncode}\n{result.stderr}"
    assert len(fixture_run.results) == 4, f"{fixture_run.paper}：有阶段未执行"


def test_fetch(fixture_run: PipelineRun) -> None:
    """本地目录入口：源码树落 src/、逐文件 sha256 记入 manifest。"""
    manifest = fixture_run.manifest("fetch")
    assert manifest.status is FetchStatus.OK
    assert manifest.kind == "local"
    assert manifest.files, "files 清单为空"
    assert manifest.tex_files, "没有识别出 .tex 文件"
    assert manifest.tex_chars > 0
    src = fixture_run.workdir / "src"
    for relative in manifest.files:
        assert (src / relative).exists(), f"manifest 记了 {relative} 但 src/ 里没有"


def test_flatten(fixture_run: PipelineRun) -> None:
    r"""展开成单文件，产物含文档环境且无注释外的 `\input` 残留。"""
    manifest = fixture_run.manifest("flatten")
    assert manifest.status is FlattenStatus.OK
    assert manifest.main_file, "没有判定出主文件"
    flat = fixture_run.build_file("flat.tex").read_text(encoding="utf-8")
    assert r"\begin{document}" in flat
    assert r"\end{document}" in flat
    body = strip_comments(flat)
    assert r"\input" not in body, "展开后仍有注释外的 \\input"
    assert r"\include{" not in body, "展开后仍有注释外的 \\include"


def test_flatten_bibliography_handling(fixture_run: PipelineRun) -> None:
    r"""三篇的参考文献路径各不相同，flatten 对三条路径的处理各有判据。

    conference 篇带预编译的 `main.bbl`，内联后 `thebibliography` 恰出现一次；revtex 篇手写
    `thebibliography`，不内联且原样通过；article 篇有 `.bib` 无 `.bbl`，不内联，`\bibliography`
    命令留给 precompile 的 latexmk 跑 bibtex。
    """
    manifest = fixture_run.manifest("flatten")
    flat = strip_comments(fixture_run.build_file("flat.tex").read_text(encoding="utf-8"))
    if fixture_run.paper == "conference":
        assert manifest.bbl_file, "conference 篇应内联预编译的 main.bbl"
        assert flat.count(r"\begin{thebibliography}") == 1
    elif fixture_run.paper == "revtex":
        assert not manifest.bbl_file, "revtex 篇手写 thebibliography，不应内联"
        assert flat.count(r"\begin{thebibliography}") == 1
    else:
        assert not manifest.bbl_file, "article 篇有 .bib 无 .bbl，不应内联"
        assert r"\bibliography{" in flat, "article 篇的 \\bibliography 命令应留给 latexmk 跑 bibtex"


def test_precompile(fixture_run: PipelineRun) -> None:
    """原文编译通过、页数大于 0；三篇均首编即过，产出与 flat.tex 逐字节相同。"""
    manifest = fixture_run.manifest("precompile")
    assert manifest.status is PrecompileStatus.OK
    assert manifest.pages > 0, "页数为 0，编译没有真正产出"
    assert manifest.pdf_bytes > 0
    assert manifest.fix_session is False, "自造论文不应触发修复会话"
    assert manifest.missing_characters == 0, "原文编译不该有 missing character"
    flat = fixture_run.build_file("flat.tex").read_bytes()
    precompile = fixture_run.build_file("precompile.tex").read_bytes()
    assert flat == precompile, "未经修复会话时 precompile.tex 应与 flat.tex 逐字节相同"


def test_mask(fixture_run: PipelineRun) -> None:
    """掩码完成，两份产物齐全，计数与 blocks.json 一致。

    状态 ok 蕴含往返自检恒等：驱动器在写出产物之前调 `verify_roundtrip`，不恒等即转
    mask_failed。
    """
    manifest = fixture_run.manifest("mask")
    assert manifest.status is MaskStatus.OK
    assert manifest.blocks_total > 0
    low, high = MASKED_RATIO_RANGE
    assert low <= manifest.masked_chars_ratio <= high, (
        f"{fixture_run.paper}：掩码字符占比 {manifest.masked_chars_ratio:.3f} 落在合理区间 "
        f"[{low}, {high}] 之外，掩码范围可能整体走样"
    )
    blocks = fixture_run.blocks()
    assert len(blocks.blocks) == manifest.blocks_total
    assert len(blocks.captions) == manifest.captions_total
    masked = fixture_run.build_file("masked.tex").read_text(encoding="utf-8")
    assert len(BLOCK_TOKEN_RE.findall(masked)) == manifest.blocks_total


def test_mask_caption_slots_match_source(fixture_run: PipelineRun) -> None:
    r"""caption 槽位数与源码里注释外的 `\caption` 数吻合（abstract 槽位另计）。"""
    manifest = fixture_run.manifest("mask")
    blocks = fixture_run.blocks()
    caption_slots = [record for record in blocks.captions if record.kind.value == "caption"]
    source_captions = CAPTION_RE.findall(strip_comments(fixture_run.build_file("flat.tex").read_text(encoding="utf-8")))
    assert len(caption_slots) == len(source_captions), (
        f"{fixture_run.paper}：caption 槽位 {len(caption_slots)} 个，源码里 {len(source_captions)} 个"
    )
    assert manifest.captions_total == len(blocks.captions)


def test_mask_unknown_environments(fixture_run: PipelineRun) -> None:
    """分类表外的环境记 unknown 且由默认规则裁定，走保守整块掩码。"""
    environments = fixture_run.manifest("mask").environments
    expected = UNKNOWN_ENVIRONMENT_BY_PAPER.get(fixture_run.paper)
    if expected is None:
        return
    assert expected in environments, f"{expected} 未出现在环境分类结论里"
    record = environments[expected]
    assert record.classification is EnvironmentClass.NON_TRANSLATABLE
    assert record.category == "unknown"
    assert record.decided_by is DecidedBy.DEFAULT
    assert record.blocks > 0, f"{expected} 应当实际成块"


def test_mask_declaration_driven_environments(fixture_run: PipelineRun) -> None:
    r"""article 篇的定理环境与自定义环境由源码声明裁定，留在掩码文本里不成块。"""
    if fixture_run.paper not in DECLARATION_DRIVEN_PAPERS:
        return
    environments = fixture_run.manifest("mask").environments
    declared = {
        name: record
        for name, record in environments.items()
        if record.decided_by in (DecidedBy.NEWTHEOREM, DecidedBy.NEWENVIRONMENT)
    }
    assert declared, "没有由 \\newtheorem / \\newenvironment 裁定的环境"
    assert any(record.decided_by is DecidedBy.NEWTHEOREM for record in declared.values())
    assert any(record.decided_by is DecidedBy.NEWENVIRONMENT for record in declared.values())
    for name, record in declared.items():
        assert record.classification is EnvironmentClass.TEXT, f"{name} 应留在掩码文本里"
        assert record.blocks == 0, f"{name} 不应成块"


def test_mask_metadata_block_removes_title(fixture_run: PipelineRun, paper_manifests: dict[str, dict]) -> None:
    r"""revtex 篇正文里的 `\title` 成 metadata block，掩码文本中不再出现原题文本。"""
    if fixture_run.paper not in METADATA_BLOCK_PAPERS:
        return
    masked = fixture_run.build_file("masked.tex").read_text(encoding="utf-8")
    flat = fixture_run.build_file("flat.tex").read_text(encoding="utf-8")
    title = paper_manifests[fixture_run.paper]["title"]
    assert title in flat, "前提不成立：原题不在展开后的源码里"
    assert title not in masked, "掩码文本里仍能看到原题，metadata block 没有摘出去"
    for command in (r"\title", r"\author"):
        assert command in strip_comments(flat), f"前提不成立：源码里没有 {command}"
        assert command not in strip_comments(masked), f"{command} 没有随 metadata block 摘出去"


def test_rerun_skips_and_force_recomputes(fixture_run: PipelineRun) -> None:
    """重跑命中跳过、`--force` 重算。

    fetch 不在其中：本地目录入口每次都重新拷贝源码树，本就不做跳过判定
    （`tongtu/stages/fetch.py`），它的重拷语义在文本层由
    `tests/text/test_cli_contract.py::test_local_entry_recopies_source` 覆盖——只做文件
    拷贝，不需要 TeX。
    """
    for stage in ("flatten", "precompile", "mask"):
        manifest_path = fixture_run.workdir / "build" / "manifests" / f"{stage}.json"
        before = manifest_path.stat().st_mtime_ns

        rerun = run_stage(stage, fixture_run.source, fixture_run.workdir)
        assert rerun.returncode == 0, rerun.stderr
        assert "跳过" in rerun.stdout, f"{stage} 重跑没有命中跳过：{rerun.stdout}"
        assert manifest_path.stat().st_mtime_ns == before, f"{stage} 命中跳过时不应重写 manifest"

        forced = run_stage(stage, fixture_run.source, fixture_run.workdir, force=True)
        assert forced.returncode == 0, forced.stderr
        assert "跳过" not in forced.stdout, f"{stage} 带 --force 时不应跳过"
        assert manifest_path.stat().st_mtime_ns != before, f"{stage} 带 --force 时应重写 manifest"


def test_precompile_pages_match_estimate(fixture_run: PipelineRun, paper_manifests: dict[str, dict]) -> None:
    """页数与 MANIFEST 目测的规模相符，容差两页。

    docs/stages/precompile.md 的验收写的是「页数大于 0 且与真实 PDF 规模相符」；只断言大于 0
    等于不设限，排版跑飞成十几页也照样通过。目测值是粗判基准，不追求精确。
    """
    manifest = fixture_run.manifest("precompile")
    estimate = paper_manifests[fixture_run.paper]["pages_estimate"]
    assert abs(manifest.pages - estimate) <= PAGES_TOLERANCE, (
        f"{fixture_run.paper}：编译得 {manifest.pages} 页，MANIFEST 目测 {estimate} 页，超出容差 {PAGES_TOLERANCE} 页"
    )


def test_specialised_expectations_are_registered() -> None:
    """三张按论文分流的登记表里，论文名都真实存在。

    这些表驱动的用例在不匹配的论文上直接返回，键名写错就再也不执行断言而测试照绿。这里
    把「表里的名字必须是真论文」钉住，让写错当场暴露。
    """
    for table_name, papers in (
        ("UNKNOWN_ENVIRONMENT_BY_PAPER", tuple(UNKNOWN_ENVIRONMENT_BY_PAPER)),
        ("METADATA_BLOCK_PAPERS", METADATA_BLOCK_PAPERS),
        ("DECLARATION_DRIVEN_PAPERS", DECLARATION_DRIVEN_PAPERS),
    ):
        unknown = set(papers) - set(FIXTURE_PAPERS)
        assert not unknown, f"{table_name} 里有不存在的论文：{sorted(unknown)}"
        assert papers, f"{table_name} 是空表，对应的专项断言从不执行"
