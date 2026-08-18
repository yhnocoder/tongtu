"""自造论文 `MANIFEST.json` 与源码的一致性。

examples/README.md 记着这两端校验此前由测试探针保证、随重构移除、重建测试时恢复：声明的
覆盖点必须真在源码里，三篇的并集必须等于覆盖矩阵的全部词表。少了它，改动论文源码时
MANIFEST 会悄悄与源码脱节，而后续各阶段的验收条目正是按 MANIFEST 记的形态写的。

覆盖点分三类判定：多数能由源码里的固定标志判出；`asset_pdf` 这类描述的是附属文件的构成，
由 MANIFEST 自身的字段判出；`nested_env` 这类是语义性的，没有低成本的机械判据，列入
`SEMANTIC_COVERAGE` 只校验它出现在词表内，仍靠人工维护。
"""

from __future__ import annotations

import json
import re

import pytest

from ..conftest import FIXTURE_PAPERS, PAPERS_DIR, REPO_ROOT, paper_dir

#: examples/README.md 覆盖矩阵那一节的标题，词表从它之后开始解析——README 里另有一张
#: MANIFEST 字段说明表，行形状相同，不从它取词。
COVERAGE_SECTION_HEADING = "## 覆盖矩阵"

#: 覆盖矩阵表的行首单元格，即一个覆盖点。
COVERAGE_TABLE_RE = re.compile(r"^\| `([a-z_]+)` \|", re.MULTILINE)

#: 覆盖点 → 源码里必须出现的标志之一。
SOURCE_PROBES: dict[str, tuple[str, ...]] = {
    "abstract": (r"\begin{abstract}", r"\abstract"),
    "align_env": (r"\begin{align}",),
    "appendix": (r"\appendix", r"\appendices", r"\begin{appendices}"),
    "caption_label_inline": (r"\caption{\label",),
    "caption_optional_arg": (r"\caption[",),
    "cite": (r"\cite",),
    "custom_env_declared": (r"\newenvironment",),
    "custom_macro": (r"\newcommand", "\\def\\", r"\DeclareMathOperator"),
    "enumerate": (r"\begin{enumerate}",),
    "equation_env": (r"\begin{equation}",),
    "escaped_ampersand": (r"\&",),
    "escaped_hash": (r"\#",),
    "escaped_percent": (r"\%",),
    # 星号变体继承基础环境的分类，用了 figure* 就算用了 figure 环境，故按前缀匹配两者。
    "figure_env": (r"\begin{figure",),
    "figure_starred": (r"\begin{figure*}",),
    "footnote": (r"\footnote",),
    "includegraphics": (r"\includegraphics",),
    "inline_math": ("$", r"\("),
    "itemize": (r"\begin{itemize}",),
    "label": (r"\label",),
    "lstlisting_env": (r"\begin{lstlisting}",),
    "newtheorem": (r"\newtheorem",),
    "ref": (r"\ref", r"\eqref", r"\autoref"),
    "section": (r"\section",),
    "subsection": (r"\subsection",),
    "subsubsection": (r"\subsubsection",),
    "table_env": (r"\begin{table}",),
    "tabular_env": (r"\begin{tabular}",),
    "thebibliography_env": (r"\begin{thebibliography}",),
    "title": (r"\title",),
    "verbatim_env": (r"\begin{verbatim}",),
}

#: 覆盖点 → 由 MANIFEST 自身字段判定（描述的是附属文件构成或版式身份，不是源码里的标志）。
MANIFEST_PROBES: dict[str, str] = {
    "asset_pdf": "generated_assets 里有 .pdf",
    "asset_png": "generated_assets 里有 .png",
    "bibtex_database": "aux_files 里有 .bib",
    "local_sty_package": "aux_files 里有 .sty",
    "precompiled_bbl": "aux_files 里有 .bbl",
    "multi_file_input": "inputs 非空",
    "two_column": "columns 为 2",
    "title_in_preamble": r"\title 出现在 \begin{document} 之前",
}

#: 语义性覆盖点：没有低成本的机械判据，由各篇 notes 与人工维护，此处只校验它在词表内。
SEMANTIC_COVERAGE: frozenset[str] = frozenset({"comment_run", "custom_env_unknown", "nested_env", "theorem_env_usage"})


def manifest_of(paper: str) -> dict:
    return json.loads((paper_dir(paper) / "MANIFEST.json").read_text(encoding="utf-8"))


def source_text(paper: str) -> str:
    """一篇论文源码树里全部 `.tex` 与 `.sty` 的内容拼接，供标志探测。"""
    directory = paper_dir(paper)
    parts = [path.read_text(encoding="utf-8") for path in sorted(directory.rglob("*.tex"))]
    parts += [path.read_text(encoding="utf-8") for path in sorted(directory.rglob("*.sty"))]
    return "\n".join(parts)


def coverage_vocabulary() -> set[str]:
    """examples/README.md 覆盖矩阵声明的全部词表。"""
    readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    heading_at = readme.find(COVERAGE_SECTION_HEADING)
    assert heading_at >= 0, f"examples/README.md 里找不到「{COVERAGE_SECTION_HEADING}」一节"
    return set(COVERAGE_TABLE_RE.findall(readme[heading_at:]))


def check_manifest_probe(point: str, manifest: dict, paper: str) -> bool:
    """按覆盖点判定 MANIFEST 字段，判不出对应关系时让用例失败。"""
    aux = manifest["aux_files"]
    assets = manifest["generated_assets"]
    match point:
        case "asset_pdf":
            return any(name.endswith(".pdf") for name in assets)
        case "asset_png":
            return any(name.endswith(".png") for name in assets)
        case "bibtex_database":
            return any(name.endswith(".bib") for name in aux)
        case "local_sty_package":
            return any(name.endswith(".sty") for name in aux)
        case "precompiled_bbl":
            return any(name.endswith(".bbl") for name in aux)
        case "multi_file_input":
            return bool(manifest["inputs"])
        case "two_column":
            return manifest["columns"] == 2
        case "title_in_preamble":
            main = (paper_dir(paper) / manifest["main"]).read_text(encoding="utf-8")
            title_at = main.find(r"\title")
            document_at = main.find(r"\begin{document}")
            return 0 <= title_at < document_at
        case _:
            pytest.fail(f"覆盖点 {point} 归入 MANIFEST_PROBES 但没有判定分支")


def test_vocabulary_is_partitioned() -> None:
    """三类判定合起来恰好覆盖词表，不重不漏。"""
    vocabulary = coverage_vocabulary()
    assert vocabulary, "没有从 examples/README.md 解析出覆盖矩阵词表"
    classified = set(SOURCE_PROBES) | set(MANIFEST_PROBES) | SEMANTIC_COVERAGE
    assert classified == vocabulary, (
        f"判定方式与词表不符：词表多出 {sorted(vocabulary - classified)}，判定多出 {sorted(classified - vocabulary)}"
    )


def test_union_equals_vocabulary() -> None:
    """三篇声明的覆盖点并集等于全部词表：每个词都至少被一篇覆盖。"""
    union: set[str] = set()
    for paper in FIXTURE_PAPERS:
        union |= set(manifest_of(paper)["coverage"])
    assert union == coverage_vocabulary(), (
        f"并集与词表不符：词表未被覆盖 {sorted(coverage_vocabulary() - union)}，"
        f"声明了词表外的点 {sorted(union - coverage_vocabulary())}"
    )


@pytest.mark.parametrize("paper", FIXTURE_PAPERS)
def test_coverage_is_sorted_and_deduplicated(paper: str) -> None:
    """`coverage` 清单排序、去重，diff 才稳定。"""
    coverage = manifest_of(paper)["coverage"]
    assert coverage == sorted(coverage), "coverage 未排序"
    assert len(coverage) == len(set(coverage)), "coverage 有重复项"


@pytest.mark.parametrize("paper", FIXTURE_PAPERS)
def test_declared_coverage_is_present_in_source(paper: str) -> None:
    """声明的每个覆盖点都能在源码或 MANIFEST 字段里找到依据。"""
    manifest = manifest_of(paper)
    source = source_text(paper)
    missing: list[str] = []
    for point in manifest["coverage"]:
        if point in SEMANTIC_COVERAGE:
            continue
        if point in SOURCE_PROBES:
            if not any(marker in source for marker in SOURCE_PROBES[point]):
                missing.append(point)
        elif point in MANIFEST_PROBES:
            if not check_manifest_probe(point, manifest, paper):
                missing.append(point)
        else:
            missing.append(f"{point}（无判定方式）")
    assert not missing, f"{paper} 声明了但源码里找不到依据的覆盖点：{missing}"


@pytest.mark.parametrize("paper", FIXTURE_PAPERS)
def test_declared_files_exist(paper: str) -> None:
    """MANIFEST 记的主文件、`\\input` 清单、附属文件与生成的图都真实存在。"""
    manifest = manifest_of(paper)
    directory = paper_dir(paper)
    assert (directory / manifest["main"]).is_file(), f"主文件 {manifest['main']} 不存在"
    for group in ("inputs", "aux_files", "generated_assets"):
        for relative in manifest[group]:
            assert (directory / relative).is_file(), f"{group} 记的 {relative} 不存在"


@pytest.mark.parametrize("paper", FIXTURE_PAPERS)
def test_documentclass_matches_source(paper: str) -> None:
    r"""MANIFEST 的 `documentclass` 与 `class_options` 与主文件的 `\documentclass` 逐项一致。"""
    manifest = manifest_of(paper)
    main = (paper_dir(paper) / manifest["main"]).read_text(encoding="utf-8")
    match = re.search(r"\\documentclass\s*(?:\[([^\]]*)\])?\s*\{([^}]+)\}", main, re.DOTALL)
    assert match, f"{paper}：主文件里找不到 \\documentclass"
    options = [item.strip() for item in (match.group(1) or "").replace("\n", "").split(",") if item.strip()]
    assert match.group(2).strip() == manifest["documentclass"], "documentclass 与源码不符"
    assert options == manifest["class_options"], f"class_options 与源码不符：源码 {options}"


@pytest.mark.parametrize("paper", FIXTURE_PAPERS)
def test_identifier_follows_convention(paper: str) -> None:
    """`id` 形如 `fixture-<目录名>`，工作目录名取自它。"""
    assert manifest_of(paper)["id"] == f"fixture-{paper}"


def test_every_paper_has_a_manifest() -> None:
    """`examples/papers/` 下的每个目录都有 MANIFEST，没有漏登记的论文。"""
    directories = {path.name for path in PAPERS_DIR.iterdir() if path.is_dir()}
    assert directories == set(FIXTURE_PAPERS), f"目录与已登记的三篇不符：{sorted(directories)}"
    for paper in FIXTURE_PAPERS:
        assert (paper_dir(paper) / "MANIFEST.json").is_file()
