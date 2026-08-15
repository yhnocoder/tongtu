"""fixture 论文的冒烟测试（架构 §12 层 1，PR 门禁）。

`tests/fixtures/papers/` 下的三篇自造论文是编译层 e2e（层 2，MockAgent 恒等翻译）
的输入。真编译要等参考镜像，本文件先把**不需要 TeX 的那部分**固定下来：

1. **MANIFEST 齐全合法**：字段、类型、id 与目录名对应、声明的文件真实存在。
2. **主文件像论文**：含 `\\documentclass` 与 `\\begin{document}`/`\\end{document}`。
3. **覆盖矩阵不缩水**：MANIFEST 的 `coverage` 是**可机器验证**的断言——每个覆盖点都有
   探针（正则或 mask 结论），claim 了就必须真的在源码里；三篇的并集必须等于全部词表。
   删掉一个覆盖点而忘了改 MANIFEST，本文件会失败。
4. **fixture 先过自家文本层**：对每篇的 flat 视图跑 `unmask(mask(x)) == x` 与
   `ChunkPlan.reassemble() == masked`。fixture 自己都不恒等的话，拿它去做 e2e 毫无意义。
5. **图片资产可复跑再生**：`gen_assets.py` 重新生成的结果与入库文件等价。

flat 视图由本文件按 `\\input` 顺序拼接（生产环境用 latexpand；这里刻意只做近似——
文本层断言不依赖展开的精确性，而 flatten 阶段自有它的测试）。`\\usepackage` 的本地
`.sty` **不**展开，与 latexpand 的默认行为一致。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tongtu.stages.chunk import chunk_masked
from tongtu.stages.mask import MaskResult, mask, roundtrip_diff

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAPERS = FIXTURES / "papers"
GEN_ASSETS = FIXTURES / "gen_assets.py"

sys.path.insert(0, str(FIXTURES))
import gen_assets  # noqa: E402  —— 生成器与被测资产同源，故意直接复用

#: MANIFEST 必填字段 → 期望类型。
REQUIRED_FIELDS: dict[str, type | tuple[type, ...]] = {
    "id": str,
    "title": str,
    "layout": str,
    "documentclass": str,
    "class_options": list,
    "columns": int,
    "main": str,
    "inputs": list,
    "aux_files": list,
    "generated_assets": list,
    "pages_estimate": int,
    "coverage": list,
    "notes": str,
}

_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]*)\}")


# --------------------------------------------------------------------------- #
# flat 视图
# --------------------------------------------------------------------------- #


def flatten(main: Path) -> str:
    """按 `\\input` / `\\include` 顺序递归拼接出近似 flat 视图。

    行内被 `%` 注释掉的 `\\input` 不展开（与 latexpand 一致）；`\\usepackage` 的本地
    `.sty` 同样不展开——conference 篇的 sidenote 环境正是靠这一点成为「分类表外的
    未知环境」，练 mask 的保守默认。
    """
    root = main.parent

    def expand(path: Path, seen: frozenset[Path]) -> str:
        assert path.exists(), f"{path} 不存在（被 \\input 引用）"
        assert path not in seen, f"{path} 循环 \\input"
        text = path.read_text(encoding="utf-8")
        pieces: list[str] = []
        pos = 0
        for m in _INPUT_RE.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            if "%" in text[line_start : m.start()]:
                continue  # 注释掉的 \input
            target = root / m.group(1)
            if not target.suffix:
                target = target.with_suffix(".tex")
            pieces.append(text[pos : m.start()])
            pieces.append(expand(target, seen | {path}))
            pos = m.end()
        pieces.append(text[pos:])
        return "".join(pieces)

    return expand(main, frozenset())


# --------------------------------------------------------------------------- #
# 覆盖点探针
# --------------------------------------------------------------------------- #


def _has_env(flat: str, name: str) -> bool:
    return re.search(r"\\begin\s*\{" + re.escape(name) + r"\*?\}", flat) is not None


def _decided_by(result: MaskResult, how: str) -> bool:
    return any(info.decided_by == how for info in result.environments)


def _asset(paper: Path, flat: str, suffix: str) -> bool:
    """该扩展名的图既入了库，又真被 `\\includegraphics` 引用。"""
    files = [p for p in (paper / "figures").glob(f"*{suffix}")] if (paper / "figures").is_dir() else []
    return bool(files) and any(
        f.name in m for f in files for m in re.findall(r"\\includegraphics[^{]*\{([^}]*)\}", flat)
    )


#: 覆盖点词表：键 → 探针 `(paper_dir, flat, mask_result) -> bool`。
#: MANIFEST 只能声明这里有的键，且声明了就必须探得到。
PROBES: dict[str, callable] = {
    # 结构
    "title": lambda p, f, r: re.search(r"\\title\s*[\[{]", f) is not None,
    "title_in_preamble": lambda p, f, r: any(c.kind == "title" for c in r.captions),
    "abstract": lambda p, f, r: _has_env(f, "abstract"),
    "section": lambda p, f, r: re.search(r"\\section\s*[\[{]", f) is not None,
    "subsection": lambda p, f, r: re.search(r"\\subsection\s*[\[{]", f) is not None,
    "subsubsection": lambda p, f, r: re.search(r"\\subsubsection\s*[\[{]", f) is not None,
    "appendix": lambda p, f, r: re.search(r"\\appendi(x|ces)(?![A-Za-z])", f) is not None or _has_env(f, "appendices"),
    "two_column": lambda p, f, r: (
        "twocolumn" in f or re.search(r"\\documentclass\[[^\]]*conference[^\]]*\]\s*\{IEEEtran\}", f) is not None
    ),
    # 数学
    "inline_math": lambda p, f, r: re.search(r"(?<!\\)\$[^$]+\$", f) is not None,
    "equation_env": lambda p, f, r: _has_env(f, "equation"),
    "align_env": lambda p, f, r: _has_env(f, "align"),
    # 浮动体
    "table_env": lambda p, f, r: _has_env(f, "table"),
    "tabular_env": lambda p, f, r: _has_env(f, "tabular"),
    "figure_env": lambda p, f, r: _has_env(f, "figure"),
    "figure_starred": lambda p, f, r: re.search(r"\\begin\s*\{(figure|table)\*\}", f) is not None,
    "includegraphics": lambda p, f, r: "\\includegraphics" in f,
    "caption_optional_arg": lambda p, f, r: re.search(r"\\caption\s*\[", f) is not None,
    "caption_label_inline": lambda p, f, r: re.search(r"\\caption\s*\{\s*\\label", f) is not None,
    "asset_png": lambda p, f, r: _asset(p, f, ".png"),
    "asset_pdf": lambda p, f, r: _asset(p, f, ".pdf"),
    # 列表与定理
    "itemize": lambda p, f, r: _has_env(f, "itemize"),
    "enumerate": lambda p, f, r: _has_env(f, "enumerate"),
    "newtheorem": lambda p, f, r: "\\newtheorem" in f,
    "theorem_env_usage": lambda p, f, r: _decided_by(r, "newtheorem"),
    # 交叉引用与脚注
    "cite": lambda p, f, r: re.search(r"\\cite\s*\{", f) is not None,
    "ref": lambda p, f, r: re.search(r"\\(eq)?ref\s*\{", f) is not None,
    "label": lambda p, f, r: re.search(r"\\label\s*\{", f) is not None,
    "footnote": lambda p, f, r: re.search(r"\\footnote\s*\{", f) is not None,
    # 逐字与转义
    "verbatim_env": lambda p, f, r: _has_env(f, "verbatim"),
    "lstlisting_env": lambda p, f, r: _has_env(f, "lstlisting"),
    "escaped_percent": lambda p, f, r: re.search(r"(?<!\\)\\%", f) is not None,
    "escaped_ampersand": lambda p, f, r: re.search(r"(?<!\\)\\&", f) is not None,
    "escaped_hash": lambda p, f, r: re.search(r"(?<!\\)\\#", f) is not None,
    "comment_run": lambda p, f, r: any(b.category == "comment" for b in r.blocks),
    # 宏与自定义环境（mask 的三条分类来源各占一条）
    "custom_macro": lambda p, f, r: "\\newcommand" in f,
    "custom_env_declared": lambda p, f, r: _decided_by(r, "newenvironment"),
    "custom_env_unknown": lambda p, f, r: _decided_by(r, "default"),
    "nested_env": lambda p, f, r: any(b.tex.count("\\begin{") >= 2 for b in r.blocks),
    # 源码树与参考文献
    "multi_file_input": lambda p, f, r: _INPUT_RE.search((p / "main.tex").read_text("utf-8")) is not None,
    "local_sty_package": lambda p, f, r: any(
        re.search(r"\\usepackage\s*(\[[^\]]*\])?\s*\{[^}]*" + re.escape(sty.stem) + r"[^}]*\}", f)
        for sty in p.glob("*.sty")
    ),
    "bibtex_database": lambda p, f, r: re.search(r"\\bibliography\s*\{", f) is not None and any(p.glob("*.bib")),
    "precompiled_bbl": lambda p, f, r: any(p.glob("*.bbl")),
    "thebibliography_env": lambda p, f, r: _has_env(f, "thebibliography"),
}


# --------------------------------------------------------------------------- #
# pytest 装配
# --------------------------------------------------------------------------- #


def paper_dirs() -> list[Path]:
    return sorted(p for p in PAPERS.iterdir() if p.is_dir())


PAPER_DIRS = paper_dirs()


@pytest.fixture(scope="module")
def loaded() -> dict[str, tuple[Path, dict, str, MaskResult]]:
    """每篇论文一份 (目录, MANIFEST, flat 视图, mask 结果)——mask 只跑一次。"""
    out = {}
    for paper in PAPER_DIRS:
        manifest = json.loads((paper / "MANIFEST.json").read_text(encoding="utf-8"))
        flat = flatten(paper / manifest["main"])
        out[paper.name] = (paper, manifest, flat, mask(flat))
    return out


def test_three_papers_present():
    """三种版式一篇不少（架构 §12：article / revtex / 双栏会议）。"""
    assert [p.name for p in PAPER_DIRS] == ["article", "conference", "revtex"]


# --------------------------------------------------------------------------- #
# 1. MANIFEST
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_manifest_fields(paper, loaded):
    _, manifest, _, _ = loaded[paper.name]
    for field, expected in REQUIRED_FIELDS.items():
        assert field in manifest, f"{paper.name}/MANIFEST.json 缺字段 {field}"
        assert isinstance(manifest[field], expected), f"{field} 类型应为 {expected}"
    assert manifest["id"] == f"fixture-{paper.name}"
    assert manifest["layout"] == paper.name
    assert manifest["columns"] in (1, 2)
    assert 1 <= manifest["pages_estimate"] <= 8
    assert all(isinstance(x, str) for x in manifest["coverage"])
    assert manifest["coverage"] == sorted(manifest["coverage"]), "coverage 应排序，便于 diff"
    assert len(set(manifest["coverage"])) == len(manifest["coverage"])


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_manifest_files_exist(paper, loaded):
    """MANIFEST 声明的每个文件都在，且反过来没有漏报的 .tex。"""
    _, manifest, _, _ = loaded[paper.name]
    declared = [manifest["main"]] + manifest["inputs"] + manifest["aux_files"] + manifest["generated_assets"]
    for name in declared:
        target = paper / name
        assert target.is_file(), f"{paper.name}/MANIFEST.json 声明了不存在的 {name}"
        assert target.stat().st_size > 0, f"{name} 是空文件"
    on_disk = {str(p.relative_to(paper)) for p in paper.rglob("*.tex")}
    assert on_disk == {manifest["main"], *manifest["inputs"]}, "磁盘上的 .tex 与 MANIFEST 不符"


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_main_looks_like_a_paper(paper, loaded):
    _, manifest, flat, _ = loaded[paper.name]
    main = (paper / manifest["main"]).read_text(encoding="utf-8")
    declaration = re.search(
        r"\\documentclass\s*(?:\[([^\]]*)\])?\s*\{" + re.escape(manifest["documentclass"]) + r"\}",
        main,
    )
    assert declaration, f"{paper.name}/{manifest['main']} 的 \\documentclass 与 MANIFEST 不符"
    options = [o.strip() for o in (declaration.group(1) or "").split(",") if o.strip()]
    assert options == manifest["class_options"]
    assert "\\begin{document}" in main
    assert main.rstrip().endswith("\\end{document}")


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_input_order_matches_manifest(paper, loaded):
    """MANIFEST 的 inputs 按 `\\input` 的实际顺序列（flatten 复现的就是这个顺序）。"""
    _, manifest, _, _ = loaded[paper.name]
    found: list[str] = []

    def walk(path: Path):
        text = path.read_text(encoding="utf-8")
        for m in _INPUT_RE.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            if "%" in text[line_start : m.start()]:
                continue
            target = Path(m.group(1))
            if not target.suffix:
                target = target.with_suffix(".tex")
            found.append(str(target))
            walk(paper / target)

    walk(paper / manifest["main"])
    assert found == manifest["inputs"]


# --------------------------------------------------------------------------- #
# 2. 覆盖矩阵
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_coverage_keys_are_known(paper, loaded):
    _, manifest, _, _ = loaded[paper.name]
    unknown = set(manifest["coverage"]) - set(PROBES)
    assert not unknown, f"{paper.name} 声明了词表外的覆盖点：{sorted(unknown)}"
    assert manifest["coverage"], "coverage 不得为空"


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_claimed_coverage_is_real(paper, loaded):
    """声明了就必须真的在源码里——MANIFEST 不许说谎。"""
    _, manifest, flat, result = loaded[paper.name]
    missing = [key for key in manifest["coverage"] if not PROBES[key](paper, flat, result)]
    assert not missing, f"{paper.name} 声明但探不到的覆盖点：{missing}"


def test_coverage_matrix_is_complete(loaded):
    """三篇合计必须覆盖全部词表——任何一处缩水都会让本用例失败。"""
    union: set[str] = set()
    for _, manifest, _, _ in loaded.values():
        union |= set(manifest["coverage"])
    assert union == set(PROBES), f"未被任何 fixture 覆盖：{sorted(set(PROBES) - union)}"


def test_layouts_are_distinct(loaded):
    """三种版式各不相同，且至少两篇是双栏。"""
    classes = {name: m["documentclass"] for name, (_, m, _, _) in loaded.items()}
    assert len(set(classes.values())) == 3, classes
    assert sum(m["columns"] == 2 for _, m, _, _ in loaded.values()) >= 2


# --------------------------------------------------------------------------- #
# 3. 文本层：fixture 先过自家的恒等判据
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_mask_roundtrip_is_identity(paper, loaded):
    """`unmask(mask(x)) == x`——与生产环境每篇论文都会跑的自检同一条判据（架构 §3.1）。"""
    _, _, flat, result = loaded[paper.name]
    assert roundtrip_diff(flat, result=result) is None


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_mask_has_no_warnings(paper, loaded):
    """fixture 是自造的，源码畸形没有借口：警告清单必须是空的。"""
    _, _, _, result = loaded[paper.name]
    assert result.warnings == ()


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_chunk_reassembles_to_masked(paper, loaded):
    """块拼接恒等于掩码流：无丢段、无重复。"""
    _, _, _, result = loaded[paper.name]
    plan = chunk_masked(result.masked)
    assert plan.chunks, "分块结果为空"
    assert plan.reassemble() == result.masked
    assert [c.id for c in plan] == [f"c{i:03d}" for i in range(len(plan))]


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_chunk_survives_tight_limits(paper, loaded):
    """把软/硬上限压到 fixture 规模，逼出小节下分路径，拼接仍须恒等。"""
    _, _, _, result = loaded[paper.name]
    plan = chunk_masked(result.masked, soft_target=120, hard_limit=240)
    assert plan.reassemble() == result.masked
    assert len(plan) >= 2


@pytest.mark.parametrize("paper", PAPER_DIRS, ids=lambda p: p.name)
def test_blocks_json_is_serializable(paper, loaded):
    """blocks.json 能落盘（e2e 的产物契约第一步）。"""
    _, manifest, _, result = loaded[paper.name]
    data = result.to_blocks_json(source_path=f"{manifest['id']}/flat.tex", roundtrip_ok=True)
    assert json.loads(json.dumps(data, ensure_ascii=False)) == data
    assert data["blocks"][0]["category"] == "preamble"


# --------------------------------------------------------------------------- #
# 4. 图片资产
# --------------------------------------------------------------------------- #


def test_asset_manifest_matches_disk():
    """gen_assets.py 的清单与各 MANIFEST 的 generated_assets 一致。"""
    from_generator = set(gen_assets.ASSETS)
    from_manifests = {
        f"{paper.name}/{name}"
        for paper in PAPER_DIRS
        for name in json.loads((paper / "MANIFEST.json").read_text("utf-8"))["generated_assets"]
    }
    assert from_generator == from_manifests


@pytest.mark.parametrize("relative", sorted(gen_assets.ASSETS), ids=lambda s: s)
def test_asset_is_regenerable(relative):
    """入库的图与「现在重跑生成器」的结果等价（PNG 比结构，PDF 比字节）。"""
    kind, params = gen_assets.ASSETS[relative]
    committed = (PAPERS / relative).read_bytes()
    assert gen_assets.equivalent(kind, gen_assets.render(kind, params), committed)


def test_png_assets_are_well_formed():
    """PNG 的 chunk CRC 与像素流长度自洽（CRC 校验在 png_fingerprint 里）。"""
    import struct

    for relative in sorted(gen_assets.ASSETS):
        if not relative.endswith(".png"):
            continue
        ihdr, raw = gen_assets.png_fingerprint((PAPERS / relative).read_bytes())
        width, height, depth, color_type = struct.unpack(">IIBB", ihdr[:10])
        assert (depth, color_type) == (8, 2), "约定为 8-bit truecolor"
        assert len(raw) == height * (1 + width * 3), "像素流长度与 IHDR 不符"


def test_pdf_assets_are_well_formed():
    """PDF 的 startxref 指到 xref 表，且每条记录的偏移真的落在对应对象上。"""
    for relative in sorted(gen_assets.ASSETS):
        if not relative.endswith(".pdf"):
            continue
        data = (PAPERS / relative).read_bytes()
        assert data.startswith(b"%PDF-1.4")
        assert data.rstrip().endswith(b"%%EOF")
        assert b"/MediaBox" in data
        offset = int(re.search(rb"startxref\s+(\d+)", data).group(1))
        assert data[offset : offset + 4] == b"xref"
        lines = data[offset:].split(b"\n")
        count = int(lines[1].split()[1])
        for number in range(1, count):
            entry = int(lines[2 + number].split()[0])
            assert data[entry:].startswith(b"%d 0 obj" % number), f"{relative} 对象 {number} 偏移错"


def test_gen_assets_check_mode_passes():
    """`gen_assets.py --check` 是可复跑再生的对外承诺，这里当命令行跑一遍。"""
    proc = subprocess.run([sys.executable, str(GEN_ASSETS), "--check"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
