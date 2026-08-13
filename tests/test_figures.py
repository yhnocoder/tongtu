"""figures 阶段：引用提取、caption/label 配对、引用段落、逐图缓存与降级记录。

本文件的组织原则与阶段本身一致——**渲染是注入点，元数据是产物契约**：

* 源码侧（引用提取、扩展名解析、caption/label 配对、`\\ref` 段落收集）全部可在无任何
  外部工具的机器上钉死，用三篇 fixture 的真图（PNG/PDF 各有）+ 合成源码覆盖；
* 渲染侧用假渲染器计数，把「逐图缓存」这条增量性质变成可断言的事实（改一图只重渲一图）；
* 真渲染（pdftocairo）只在装了工具的机器上跑，skipif 掉——本机没有工具是一等公民，
  纯 Python 兜底的两种降级（`missing_tool` / `downscale_skipped`）自己有用例。
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tongtu.schema_check import check as schema_check
from tongtu.stages import figures as fg
from tongtu.stages.chunk import chunk_masked
from tongtu.stages.mask import mask
from tongtu.workdir import Workdir

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAPERS = FIXTURES / "papers"

sys.path.insert(0, str(FIXTURES))
import gen_assets  # noqa: E402  —— 与入库图同源的生成器，测试里造图也用它

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fixtures import flatten  # noqa: E402  —— fixture 的近似 flat 视图，两处共用


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@dataclass
class FakeRenderer:
    """计数用的假渲染器：记下每次调用，产出一张真 PNG（下游断言的是元数据，不是像素）。"""

    width: int = 120
    height: int = 80
    calls: list[Path] = field(default_factory=list)

    def render(self, src: Path, dst: Path, max_long_edge: int) -> fg.RenderResult:
        self.calls.append(src)
        data = gen_assets.build_png(width=self.width, height=self.height, pattern="lanes")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        return fg.RenderResult(
            ok=True,
            width_px=self.width,
            height_px=self.height,
            dpi=300.0,
            bytes=len(data),
            tool="fake",
        )

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.calls]


def build_paper(tmp_path: Path, name: str) -> tuple[Workdir, object, str]:
    """把一篇 fixture 论文摆进工作目录的 `src/`，并给出它的掩码结果。"""
    workdir = Workdir(path=tmp_path / name).create()
    paper = PAPERS / name
    shutil.copytree(paper, workdir.src, dirs_exist_ok=True)
    result = mask(flatten(paper / "main.tex"))
    return workdir, result, result.masked


def build_source(
    tmp_path: Path,
    tex: str,
    assets: dict[str, bytes] | None = None,
    preamble: str = "",
):
    """合成一篇最小论文：给定正文 + 给定资产文件，返回 (workdir, mask 结果)。"""
    workdir = Workdir(path=tmp_path / "synthetic").create()
    for relative, payload in (assets or {}).items():
        target = workdir.src / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    src = (
        "\\documentclass{article}\n\\usepackage{graphicx}\n"
        + (preamble + "\n" if preamble else "")
        + "\\begin{document}\n"
        + tex
        + "\n\\end{document}\n"
    )
    return workdir, mask(src)


def png(width: int = 40, height: int = 24) -> bytes:
    return gen_assets.build_png(width=width, height=height, pattern="lanes")


def pdf(width: int = 144, height: int = 108) -> bytes:
    return gen_assets.build_pdf(
        width=width, height=height, rects=[(4, 4, width - 8, height - 8, (0.5, 0.5, 0.5))]
    )


# --------------------------------------------------------------------------- #
# 源码侧：\includegraphics 提取
# --------------------------------------------------------------------------- #


def test_iter_includegraphics_reads_options_star_and_nested_args():
    tex = (
        "\\includegraphics[width=0.5\\linewidth]{figures/a.png}\n"
        "\\includegraphics*[0,0][1,1]{b}\n"
        "\\includegraphics[\n  width=\\textwidth,\n  trim={1 2 3 4},\n]{sub/dir/c.pdf}\n"
        "% \\includegraphics{commented.png}\n"
        "\\includegraphics {\n  spaced.png\n}\n"
    )
    found = fg.iter_includegraphics(tex)
    assert [g.argument for g in found] == [
        "figures/a.png",
        "b",
        "sub/dir/c.pdf",
        "spaced.png",
    ]
    assert found[0].options == ("width=0.5\\linewidth",)
    assert found[1].options == ("0,0", "1,1")
    assert "trim={1 2 3 4}" in found[2].options[0]


def test_iter_includegraphics_survives_unbalanced_argument():
    """参数不配平只漏这一处，不影响同块内其他图（宁可漏，不许崩）。"""
    tex = "\\includegraphics{broken\n\\includegraphics{ok.png}"
    assert [g.argument for g in fg.iter_includegraphics(tex)] == ["ok.png"]


# --------------------------------------------------------------------------- #
# 源码侧：文件解析（无扩展名 / 子目录 / graphicspath）
# --------------------------------------------------------------------------- #


def test_resolve_graphic_extension_order_prefers_pdf(tmp_path):
    root = tmp_path / "src"
    (root / "figures").mkdir(parents=True)
    for suffix in (".pdf", ".png", ".jpg"):
        (root / "figures" / f"plot{suffix}").write_bytes(b"x")
    assert fg.resolve_graphic("figures/plot", [root]).name == "plot.pdf"
    # 显式给了扩展名就用它，不再按序试
    assert fg.resolve_graphic("figures/plot.png", [root]).name == "plot.png"
    # png 在 jpg 之前
    (root / "figures" / "plot.pdf").unlink()
    assert fg.resolve_graphic("figures/plot", [root]).name == "plot.png"


def test_resolve_graphic_handles_dotted_names_and_missing(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "fig.v2.pdf").write_bytes(b"x")
    # `.v2` 不是已知图片扩展名 → 继续按序补扩展名，命中 fig.v2.pdf
    assert fg.resolve_graphic("fig.v2", [root]).name == "fig.v2.pdf"
    assert fg.resolve_graphic("./fig.v2", [root]).name == "fig.v2.pdf"
    assert fg.resolve_graphic("nope", [root]) is None


def test_resolve_graphic_refuses_to_escape_src(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (tmp_path / "outside.png").write_bytes(b"x")
    assert fg.resolve_graphic("../outside.png", [root]) is None


def test_parse_graphicspath_and_lookup(tmp_path):
    assert fg.parse_graphicspath("\\graphicspath{{figs/}{img/}}") == ("figs/", "img/")
    assert fg.parse_graphicspath("\\graphicspath{ {a/} }") == ("a/",)
    assert fg.parse_graphicspath("no graphicspath here") == ()

    # 真实论文把 \graphicspath 写在导言区——mask 把整个导言区收进 BLK-0，故块清单里找得到。
    workdir, result = build_source(
        tmp_path,
        "\\begin{figure}\n\\includegraphics{plot}\n\\caption{Elsewhere.}\\label{fig:e}\n"
        "\\end{figure}",
        {"art/plot.png": png()},
        preamble="\\graphicspath{{art/}}",
    )
    (spec,) = fg.collect_figures(workdir, result)
    assert spec.rel_path == "art/plot.png"
    assert spec.format == "png"

    # 调用方直接指定搜索目录也行（graphicspath 藏在没展开的 .sty 里时的后路）。
    bare = build_source(
        tmp_path / "bare",
        "\\begin{figure}\n\\includegraphics{plot}\n\\caption{E}\\label{fig:e}\n\\end{figure}",
        {"art/plot.png": png()},
    )
    assert fg.collect_figures(*bare)[0].found is False
    assert fg.collect_figures(*bare, graphicspath=["art"])[0].rel_path == "art/plot.png"


def test_collect_figures_reports_missing_file(tmp_path):
    workdir, result = build_source(
        tmp_path,
        "\\begin{figure}\n\\includegraphics{ghost}\n\\caption{Nothing.}\\label{fig:g}\n"
        "\\end{figure}",
    )
    warnings: list[str] = []
    (spec,) = fg.collect_figures(workdir, result, warnings=warnings)
    assert not spec.found and spec.sha256 == ""
    assert any("ghost" in w for w in warnings)

    stage = fg.figures(workdir, result, renderer=FakeRenderer())
    assert stage.status == fg.DEGRADED
    assert [(s.id, s.reason) for s in stage.skipped] == [("fig-001", fg.MISSING_FILE)]
    assert stage.records == ()


# --------------------------------------------------------------------------- #
# 源码侧：caption / label 配对
# --------------------------------------------------------------------------- #


def test_caption_pairing_ignores_optional_short_title(tmp_path):
    """`\\caption[短标题]{正文}` 的短标题进目录，不是图注——配对只认必选槽位。"""
    workdir, result, masked = build_paper(tmp_path, "article")
    specs = {s.rel_path: s for s in fg.collect_figures(workdir, result, masked=masked)}
    pipeline = specs["figures/pipeline.pdf"]
    assert pipeline.label == "fig:pipeline"
    assert pipeline.caption.startswith("Schematic of the")
    assert "Placeholder pipeline" not in pipeline.caption


def test_label_inside_caption_is_found(tmp_path):
    """revtex 惯用 `\\caption{\\label{fig:x}…}`——label 在 CAP 槽位里，照样要认出来。"""
    workdir, result, masked = build_paper(tmp_path, "revtex")
    (spec,) = fg.collect_figures(workdir, result, masked=masked)
    assert spec.label == "fig:spectrum"
    assert spec.source_json()["format"] == "pdf"
    assert spec.size_pt == (120.0, 96.0)  # MediaBox，纯 Python 读出来的


def test_subfigure_block_yields_one_record_per_image(tmp_path):
    tex = (
        "\\begin{figure}\n"
        "  \\begin{subfigure}{0.45\\linewidth}\n"
        "    \\includegraphics[width=\\linewidth]{figs/left}\n"
        "    \\caption{The left one.}\\label{fig:left}\n"
        "  \\end{subfigure}\n"
        "  \\begin{subfigure}{0.45\\linewidth}\n"
        "    \\includegraphics[width=\\linewidth]{figs/right.png}\n"
        "    \\caption{The right one.}\\label{fig:right}\n"
        "  \\end{subfigure}\n"
        "  \\caption{Both of them.}\\label{fig:both}\n"
        "\\end{figure}\n"
    )
    workdir, result = build_source(
        tmp_path, tex, {"figs/left.pdf": pdf(), "figs/right.png": png()}
    )
    specs = fg.collect_figures(workdir, result)
    assert [s.id for s in specs] == ["fig-001", "fig-002"]
    assert [s.rel_path for s in specs] == ["figs/left.pdf", "figs/right.png"]
    assert [s.label for s in specs] == ["fig:left", "fig:right"]
    assert [s.caption for s in specs] == ["The left one.", "The right one."]
    assert {s.block_id for s in specs} == {specs[0].block_id}  # 同一个 figure 块


def test_figure_without_own_label_falls_back_to_block_label(tmp_path):
    workdir, result = build_source(
        tmp_path,
        "\\begin{figure}\n\\label{fig:only}\n\\includegraphics{a.png}\n"
        "\\caption{No label of its own.}\n\\end{figure}",
        {"a.png": png()},
    )
    (spec,) = fg.collect_figures(workdir, result)
    assert spec.label == "fig:only"


# --------------------------------------------------------------------------- #
# 源码侧：label → \ref 引用段落
# --------------------------------------------------------------------------- #


def test_unknown_category_blocks_are_scanned_too(tmp_path):
    """`.sty` 里自定义的浮动体环境被 mask 保守地记成 unknown——里面的图照样要收。"""
    workdir, result = build_source(
        tmp_path,
        "\\begin{myfloat}\n\\includegraphics{x.png}\n\\caption{Custom float.}"
        "\\label{fig:x}\n\\end{myfloat}",
        {"x.png": png()},
    )
    (block,) = [b for b in result.blocks if b.environment == "myfloat"]
    assert block.category == "unknown", "前提：表外环境走保守默认"
    (spec,) = fg.collect_figures(workdir, result)
    assert (spec.rel_path, spec.label, spec.caption) == ("x.png", "fig:x", "Custom float.")
    # 想只认 figure 的调用方照样可以收紧
    assert fg.collect_figures(workdir, result, categories=["figure"]) == ()


def test_references_collect_paragraphs_and_chunk_ids(tmp_path):
    workdir, result, masked = build_paper(tmp_path, "article")
    plan = chunk_masked(masked)
    specs = {s.rel_path: s for s in fg.collect_figures(workdir, result, masked=masked, plan=plan)}

    residuals = specs["figures/residuals.png"]
    assert residuals.referenced_in, "fig:residuals 在 results 节里被 \\ref 过"
    first = residuals.referenced_in[0]
    assert "\\ref{fig:residuals}" in first.text
    assert first.chunk_id and first.chunk_id in {c.id for c in plan.chunks}
    assert first.section

    pipeline = specs["figures/pipeline.pdf"]
    assert any("shows the same thing with boxes" in r.text for r in pipeline.referenced_in)


def test_references_dedupe_and_accept_ref_families():
    masked = (
        "\\section{One}\n\nSee \\cref{fig:a,fig:b} and again \\autoref{fig:a}.\n\n"
        "\\section{Two}\n\nAnd \\hyperref[fig:b]{that one}, plus \\pageref{fig:a}.\n"
    )
    refs = fg.collect_references(masked, ["fig:a", "fig:b", "fig:unused"])
    assert set(refs) == {"fig:a", "fig:b"}
    # 同段两次引用只记一条
    assert [r.paragraph for r in refs["fig:a"]] == sorted({r.paragraph for r in refs["fig:a"]})
    assert len(refs["fig:a"]) == 2
    assert [r.section for r in refs["fig:b"]] == ["One", "Two"]


def test_references_absent_without_masked_stream(tmp_path):
    """figures 不为了引用段落去依赖翻译轨产物：没给掩码流就是空，而不是报错。"""
    workdir, result, _ = build_paper(tmp_path, "article")
    specs = fg.collect_figures(workdir, result)
    assert all(s.referenced_in == () for s in specs)


# --------------------------------------------------------------------------- #
# 逐图缓存
# --------------------------------------------------------------------------- #


def test_cache_skips_unchanged_figures(tmp_path):
    assets = {f"figs/f{i}.png": png(width=10 + i) for i in range(3)}
    tex = "\n".join(
        f"\\begin{{figure}}\n\\includegraphics{{figs/f{i}}}\n"
        f"\\caption{{Number {i}.}}\\label{{fig:f{i}}}\n\\end{{figure}}"
        for i in range(3)
    )
    workdir, result = build_source(tmp_path, tex, assets)

    renderer = FakeRenderer()
    first = fg.figures(workdir, result, renderer=renderer)
    assert first.status == fg.OK
    assert (first.rendered, first.cached) == (3, 0)
    assert len(renderer.calls) == 3

    # 什么都没变 → 一张都不重渲
    renderer.calls.clear()
    second = fg.figures(workdir, result, renderer=renderer)
    assert (second.rendered, second.cached) == (0, 3)
    assert renderer.calls == []

    # 改一张图 → 只重渲这一张（源文件 hash 是唯一的 key）
    (workdir.src / "figs/f1.png").write_bytes(png(width=99))
    renderer.calls.clear()
    third = fg.figures(workdir, result, renderer=renderer)
    assert (third.rendered, third.cached) == (1, 2)
    assert renderer.names == ["f1.png"]

    # --force 无视缓存
    renderer.calls.clear()
    forced = fg.figures(workdir, result, renderer=renderer, force=True)
    assert (forced.rendered, forced.cached) == (3, 0)


def test_cache_survives_id_shift(tmp_path):
    """前面插一张图会让后面的 id 全部平移——图本身没变就不该重渲。"""
    body = "\\begin{{figure}}\n\\includegraphics{{{name}}}\n\\caption{{{cap}}}\\end{{figure}}"
    assets = {"a.png": png(width=12), "b.png": png(width=14)}
    workdir, first_mask = build_source(
        tmp_path, body.format(name="a.png", cap="A"), assets
    )
    renderer = FakeRenderer()
    fg.figures(workdir, first_mask, renderer=renderer)
    assert renderer.names == ["a.png"]

    two = mask(
        "\\documentclass{article}\\begin{document}\n"
        + body.format(name="b.png", cap="B")
        + "\n"
        + body.format(name="a.png", cap="A")
        + "\n\\end{document}\n"
    )
    renderer.calls.clear()
    result = fg.figures(workdir, two, renderer=renderer)
    assert renderer.names == ["b.png"], "a.png 只是换了 id，不该重渲"
    assert (result.rendered, result.cached) == (1, 1)
    files = {r.spec.rel_path: r.file for r in result.records}
    assert files == {"b.png": "fig-001.png", "a.png": "fig-002.png"}
    assert (workdir.build / "figures/fig-002.png").is_file()


def test_cache_survives_id_swap(tmp_path):
    """两张图对调 id：先写的那张不许毁掉后写那张要读的缓存文件（内容必须各归各位）。"""
    body = "\\begin{{figure}}\n\\includegraphics{{{name}}}\n\\caption{{{cap}}}\\end{{figure}}"

    def document(first: str, second: str) -> str:
        return (
            "\\documentclass{article}\\begin{document}\n"
            + body.format(name=first, cap=first)
            + "\n"
            + body.format(name=second, cap=second)
            + "\n\\end{document}\n"
        )

    workdir = Workdir(path=tmp_path / "swap").create()
    payloads = {"a.png": png(width=30, height=10), "b.png": png(width=31, height=11)}
    for name, data in payloads.items():
        (workdir.src / name).write_bytes(data)

    renderer = fg.PurePythonRenderer()  # 拷贝语义 → 产物字节可直接比对
    fg.figures(workdir, mask(document("a.png", "b.png")), renderer=renderer)
    swapped = fg.figures(workdir, mask(document("b.png", "a.png")), renderer=renderer)
    assert (swapped.rendered, swapped.cached) == (0, 2)

    out = workdir.build / fg.FIGURES_DIRNAME
    assert (out / "fig-001.png").read_bytes() == payloads["b.png"]
    assert (out / "fig-002.png").read_bytes() == payloads["a.png"]
    assert not any(p.name.startswith(fg.STAGE_PREFIX) for p in out.iterdir())


def test_same_source_twice_renders_once(tmp_path):
    """同一份源图被引两次：第二次走缓存（key 是文件 hash，与它在哪儿被引无关）。"""
    workdir, result = build_source(
        tmp_path,
        "\\begin{figure}\\includegraphics{same.png}\\caption{Once}\\end{figure}\n"
        "\\begin{figure}\\includegraphics{same.png}\\caption{Twice}\\end{figure}",
        {"same.png": png()},
    )
    renderer = FakeRenderer()
    stage = fg.figures(workdir, result, renderer=renderer)
    assert len(renderer.calls) == 1
    assert (stage.rendered, stage.cached) == (1, 1)
    assert [r.file for r in stage.records] == ["fig-001.png", "fig-002.png"]
    assert (workdir.build / "figures/fig-002.png").is_file()


def test_stale_pngs_are_swept(tmp_path):
    workdir, result = build_source(
        tmp_path,
        "\\begin{figure}\\includegraphics{a.png}\\caption{A}\\end{figure}",
        {"a.png": png()},
    )
    out = workdir.build / fg.FIGURES_DIRNAME
    fg.figures(workdir, result, renderer=FakeRenderer())
    (out / "fig-042.png").write_bytes(png())
    fg.figures(workdir, result, renderer=FakeRenderer())
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [fg.CACHE_NAME, fg.FIGURES_JSON, "fig-001.png"]
    )


# --------------------------------------------------------------------------- #
# 产物契约
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("paper", ["article", "conference", "revtex"])
def test_figures_json_passes_schema(tmp_path, paper):
    workdir, result, masked = build_paper(tmp_path, paper)
    plan = chunk_masked(masked)
    stage = fg.figures(workdir, result, masked=masked, plan=plan, renderer=FakeRenderer())
    assert stage.ok and stage.status != fg.FAILED

    document = json.loads(stage.json_path.read_text(encoding="utf-8"))
    assert schema_check(document, "figures") == []
    assert document["max_long_edge_px"] == fg.DEFAULT_MAX_LONG_EDGE
    assert document["figures"], f"{paper} 有图，清单不该是空的"
    for entry in document["figures"]:
        assert entry["render"]["path"] == f"figures/{entry['id']}.png"
        assert (workdir.build / entry["render"]["path"]).is_file()
        assert entry["render"]["upscaled"] is False
        assert entry["caption"]["translation"] == "", "译文由 export 从 CAP 槽位回填"
        assert entry["source"]["sha256"]
    assert schema_check(stage.to_figures_json(), "figures") == []


def test_empty_paper_still_writes_a_valid_manifest(tmp_path):
    workdir, result = build_source(tmp_path, "No floats here at all.\n")
    stage = fg.figures(workdir, result, renderer=FakeRenderer())
    assert stage.status == fg.OK and stage.records == ()
    document = json.loads(stage.json_path.read_text(encoding="utf-8"))
    assert document["figures"] == []
    assert schema_check(document, "figures") == []


# --------------------------------------------------------------------------- #
# 降级：没有工具 / 超上限的位图
# --------------------------------------------------------------------------- #


def test_missing_tool_skips_vector_figures(tmp_path):
    """本机没有 pdftocairo：PDF 图跳过并记 missing_tool，PNG 图照常出。"""
    workdir, result = build_source(
        tmp_path,
        "\\begin{figure}\\includegraphics{v.pdf}\\caption{Vector}\\label{fig:v}\\end{figure}\n"
        "\\begin{figure}\\includegraphics{r.png}\\caption{Raster}\\label{fig:r}\\end{figure}",
        {"v.pdf": pdf(), "r.png": png()},
    )
    stage = fg.figures(workdir, result, renderer=fg.PurePythonRenderer())
    assert stage.status == fg.DEGRADED and stage.ok
    assert [(s.id, s.reason) for s in stage.skipped] == [("fig-001", fg.MISSING_TOOL)]
    assert any("pdftocairo" in w for w in stage.warnings)
    assert [r.spec.rel_path for r in stage.records] == ["r.png"]
    assert not (workdir.build / "figures/fig-001.png").exists()
    assert schema_check(stage.to_figures_json(), "figures") == []


def test_oversized_png_is_kept_but_marked(tmp_path):
    """零依赖缩放不现实：原样拷贝 + `downscale_skipped`，绝不假装缩过。"""
    workdir, result = build_source(
        tmp_path,
        "\\begin{figure}\\includegraphics{wide.png}\\caption{Wide}\\label{fig:w}\\end{figure}",
        {"wide.png": png(width=2000, height=40)},
    )
    stage = fg.figures(workdir, result, renderer=fg.PurePythonRenderer())
    assert stage.status == fg.DEGRADED
    (record,) = stage.records
    assert record.render.degradation == fg.DOWNSCALE_SKIPPED
    assert (record.render.width_px, record.render.height_px) == (2000, 40)
    assert record.render.upscaled is False
    assert any(fg.DOWNSCALE_SKIPPED in w or "超过上限" in w for w in stage.warnings)
    assert schema_check(stage.to_figures_json(), "figures") == []
    # 降级也进缓存：重跑不会反复抄一张大图
    again = fg.figures(workdir, result, renderer=fg.PurePythonRenderer())
    assert (again.rendered, again.cached) == (0, 1)
    assert again.records[0].render.degradation == fg.DOWNSCALE_SKIPPED


def test_pure_python_renderer_rejects_corrupt_png(tmp_path):
    workdir, result = build_source(
        tmp_path,
        "\\begin{figure}\\includegraphics{bad.png}\\caption{Bad}\\end{figure}",
        {"bad.png": b"\x89PNG\r\n\x1a\nnot really a png"},
    )
    stage = fg.figures(workdir, result, renderer=fg.PurePythonRenderer())
    assert [s.reason for s in stage.skipped] == [fg.UNREADABLE]


def test_default_renderer_falls_back_when_tools_absent(tmp_path):
    """工具全缺的默认渲染器 == 纯 Python 兜底（本机 CI 的常态）。"""
    renderer = fg.DefaultRenderer.detect(pdftocairo=None, epstopdf=None, magick=None)
    src = tmp_path / "a.png"
    src.write_bytes(png(width=30, height=20))
    result = renderer.render(src, tmp_path / "out.png", fg.DEFAULT_MAX_LONG_EDGE)
    assert result.ok and result.tool == "copy" and (result.width_px, result.height_px) == (30, 20)

    vector = tmp_path / "a.pdf"
    vector.write_bytes(pdf())
    failed = renderer.render(vector, tmp_path / "out2.png", fg.DEFAULT_MAX_LONG_EDGE)
    assert not failed.ok and failed.degradation == fg.MISSING_TOOL


# --------------------------------------------------------------------------- #
# 纯函数：尺寸解析与 DPI
# --------------------------------------------------------------------------- #


def test_size_parsers():
    assert fg.png_size(png(width=48, height=32)) == (48, 32)
    assert fg.png_size(b"not a png") is None
    assert fg.pdf_page_size(pdf(width=144, height=108)) == (144.0, 108.0)
    assert fg.pdf_page_size(b"%PDF-1.4 no mediabox") is None
    assert fg.eps_bounding_box(b"%!PS-Adobe\n%%BoundingBox: 0 0 200 100\n") == (200.0, 100.0)


def test_dpi_is_derived_from_the_long_edge_and_capped():
    # 2 英寸的长边要撑到 1568px → 784 dpi，但按 MAX_DPI 封顶
    assert fg._dpi_for((144.0, 108.0), 1568) == pytest.approx(fg.MAX_DPI)
    # 大页面（A4 横放，842pt）反算出来的 DPI 在封顶之下
    assert fg._dpi_for((842.0, 595.0), 1568) == pytest.approx(72.0 * 1568 / 842.0)
    assert fg._dpi_for(None, 1568) == fg.DEFAULT_DPI


def test_source_format_mapping():
    assert fg.source_format("a.PDF") == "pdf"
    assert fg.source_format("a.jpeg") == "jpg"
    assert fg.source_format("a.tif") == "other"


# --------------------------------------------------------------------------- #
# 真渲染（装了工具才跑）
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("pdftocairo") is None, reason="本机没有 pdftocairo")
def test_real_renderer_respects_the_long_edge_cap(tmp_path):
    renderer = fg.default_renderer()
    src = tmp_path / "page.pdf"
    src.write_bytes(pdf(width=144, height=108))
    result = renderer.render(src, tmp_path / "page.png", 400)
    assert result.ok and result.tool == "pdftocairo"
    assert max(result.width_px, result.height_px) <= 400 + 2  # 取整容差
    assert result.dpi and result.bytes > 0
    assert fg.png_size((tmp_path / "page.png").read_bytes()) == (
        result.width_px,
        result.height_px,
    )


@pytest.mark.skipif(shutil.which("pdftocairo") is None, reason="本机没有 pdftocairo")
def test_real_renderer_end_to_end_on_fixture(tmp_path):
    workdir, result, masked = build_paper(tmp_path, "revtex")
    stage = fg.figures(workdir, result, masked=masked)
    assert stage.status == fg.OK and len(stage.records) == 1
    record = stage.records[0]
    assert max(record.render.width_px, record.render.height_px) <= fg.DEFAULT_MAX_LONG_EDGE + 2
    assert schema_check(stage.to_figures_json(), "figures") == []
