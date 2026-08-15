"""export 阶段：产物包组装、自包含 pack、契约自校验（架构 §3 export 行、§7）。

`tests/test_e2e_identity.py` 覆盖的是「三篇 fixture 全流水线跑到底、产物包契约齐全」，这里
覆盖的是 export 自己的行为边界，且**不跑流水线**——工作目录按最小形态手搭出来，于是每条
断言只被一件事影响：

* 契约文件齐不齐、chunks.json 有没有写回 `out/`（权威翻译记忆，决策 3）；
* 自包含 pack 装了什么、没装什么（编译中间文件不该进包，字体该进包）；
* **出口判据**：任一 JSON 不过 schema 即 `failed`，且 report.json 如实记 `schema_valid`；
* `zh.pdf` 缺席时不许假装出了包。
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from tongtu import CONTRACT_VERSION
from tongtu.memory import CHUNKS_NAME
from tongtu.schema_check import load_schema, validate_schema
from tongtu.stages import export as export_stage
from tongtu.workdir import Workdir

# --------------------------------------------------------------------------- #
# 最小工作目录
# --------------------------------------------------------------------------- #

FIGURE_TEX = "\\begin{figure}\n\\includegraphics{figs/plot}\n\\caption{⟦CAP-0⟧}\n\\end{figure}"

BLOCKS = {
    "contract_version": CONTRACT_VERSION,
    "source": {"path": "build/flat.tex"},
    "blocks": [
        {
            "id": "BLK-0",
            "placeholder": "⟦BLK-0⟧",
            "category": "preamble",
            "tex": "\\documentclass{article}\n",
            "span": {"start": 0, "end": 24},
        },
        {
            "id": "BLK-1",
            "placeholder": "⟦BLK-1⟧",
            "category": "figure",
            "tex": FIGURE_TEX,
            "label": "fig:plot",
            "caption_ids": ["CAP-0"],
            "span": {"start": 30, "end": 90},
        },
    ],
    "captions": [
        {
            "id": "CAP-0",
            "placeholder": "⟦CAP-0⟧",
            "block_id": "BLK-1",
            "kind": "caption",
            "text": "The residual plot.",
            "stream_text": "The residual plot.",
        }
    ],
}

#: 译文掩码流：CAP 行被改写过 ⇒ 那是译文（与 unmask 同一条规则）。
ZH_STREAM = "⟦BLK-0⟧\n正文一段。\n⟦BLK-1⟧\n⟦CAP-0⟧ 残差图。\n"

CHUNKS = {
    "contract_version": CONTRACT_VERSION,
    "chunks": [
        {
            "id": "c000",
            "src_hash": "a" * 64,
            "cache_key": "b" * 64,
            "status": "translated",
            "src": "⟦BLK-0⟧\n原文一段。\n⟦BLK-1⟧\n⟦CAP-0⟧ The residual plot.\n",
            "translation": ZH_STREAM,
        }
    ],
}

BRIEF = {
    "contract_version": CONTRACT_VERSION,
    "abstract": "We study placeholders.",
    "sections": [{"id": "1", "title": "Introduction", "summary": "背景。"}],
    "paper": {"title": "On Placeholders"},
}

GLOSSARY = {"contract_version": CONTRACT_VERSION, "style": {"style_version": "v1"}}

FIGURES = {
    "contract_version": CONTRACT_VERSION,
    "max_long_edge_px": 1568,
    "figures": [
        {
            "id": "fig-001",
            "label": "fig:plot",
            "block_id": "BLK-1",
            "caption": {"source": "The residual plot.", "translation": ""},
            "source": {"path": "figs/plot.png", "format": "png"},
            "render": {
                "path": "figures/fig-001.png",
                "format": "png",
                "width_px": 100,
                "height_px": 80,
            },
        }
    ],
}

ZH_TEX = (
    "\\documentclass{article}\n"
    "\\usepackage{custom}\n"
    "\\begin{document}\n"
    "正文一段。\n"
    f"{FIGURE_TEX.replace('⟦CAP-0⟧', '残差图。')}\n"
    "\\end{document}\n"
)

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000640000005008060000") + b"\x00" * 20


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    elif isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def paper(tmp_path) -> Workdir:
    """一个「compile 与 figures 刚跑完」的工作目录（最小形态，不跑流水线）。"""
    workdir = Workdir(path=tmp_path / "work", arxiv_id="2401.00001").create()
    build = workdir.build
    _write(build / "blocks.json", BLOCKS)
    _write(build / "brief.json", BRIEF)
    _write(build / "glossary.json", GLOSSARY)
    _write(build / "zh-chunks" / CHUNKS_NAME, CHUNKS)

    zh = build / "zh"
    _write(zh / "zh.tex", ZH_TEX)
    _write(zh / "zh.pdf", b"%PDF-1.4\n" + ZH_TEX.encode("utf-8") + b"\n%%EOF\n")
    _write(zh / "zh.log", "This is fake latexmk\n")  # 中间文件，不该进包
    _write(zh / "zh.aux", "\\relax\n")
    _write(zh / "zh.bbl", "\\begin{thebibliography}{1}\\end{thebibliography}\n")
    _write(zh / "custom.sty", "\\ProvidesPackage{custom}\n")
    _write(zh / "figs" / "plot.png", PNG)
    _write(zh / "fonts" / "LXGWWenKai-Light.ttf", b"not really a font")

    figures = build / "figures"
    _write(figures / "figures.json", FIGURES)
    _write(figures / "fig-001.png", PNG)
    _write(figures / "cache.json", {"cache_version": 1})  # build 区私事，不进产物包
    return workdir


REPORT = {
    "paper": {"arxiv_id": "2401.00001"},
    "status": "ok",
    "validation": {"chunks_total": 1, "translated": 1},
    "compile": {"passed": True, "engine": "xelatex", "passes": 1},
}


def run(paper: Workdir, **kwargs) -> export_stage.ExportResult:
    return export_stage.export(paper, report=dict(REPORT), **kwargs)


# --------------------------------------------------------------------- 产物


def test_export_assembles_the_contract_files(paper):
    result = run(paper)

    assert result.ok, result.message
    out = paper.out
    for name in (
        "zh.tex",
        "zh.pdf",
        "blocks.json",
        "chunks.json",
        "brief.json",
        "glossary.json",
        "anchors.json",
        "report.json",
        "report.html",
        "report-data.js",
    ):
        assert (out / name).is_file(), f"产物包缺 {name}"
    assert (out / "figures" / "fig-001.png").is_file()
    assert (out / "figures" / "figures.json").is_file()
    assert not (out / "figures" / "cache.json").exists(), "逐图缓存是 build 区的私事"
    assert (out / "zh.tex").read_text("utf-8") == ZH_TEX
    # 权威翻译记忆写回（决策 3）：build/ 删了也能从这里全量命中
    assert json.loads((out / CHUNKS_NAME).read_text("utf-8")) == CHUNKS


def test_every_json_artifact_passes_its_schema(paper):
    result = run(paper)

    assert result.ok
    assert result.invalid == ()
    by_path = {a.path: a for a in result.artifacts}
    for name, schema in export_stage.CONTRACT_SCHEMAS.items():
        assert by_path[name].schema_valid is True, name
        document = json.loads((paper.out / name).read_text("utf-8"))
        assert validate_schema(document, load_schema(schema)) == []
    # 非 JSON 产物记 null（它们的正确性由编译与浏览器裁决，不是这一层的事）
    assert by_path["zh.pdf"].schema_valid is None
    assert by_path["report.html"].schema_valid is None


def test_report_json_is_written_and_valid(paper):
    result = run(paper)

    report = json.loads((paper.out / "report.json").read_text("utf-8"))
    assert validate_schema(report, load_schema("report")) == []
    assert report["contract_version"] == CONTRACT_VERSION
    assert report["status"] == "ok"
    assert report["artifacts"], "artifacts 是产物包的自校验账"
    assert "report.json" not in {a["path"] for a in report["artifacts"]}
    assert result.report_path == paper.out / "report.json"


def test_caption_translation_is_backfilled_into_figures(paper):
    run(paper)

    figures = json.loads((paper.out / "figures" / "figures.json").read_text("utf-8"))
    assert figures["figures"][0]["caption"]["translation"] == "残差图。"
    assert figures["figures"][0]["caption"]["source"] == "The residual plot."


def test_untranslated_captions_stay_empty():
    """流里的 CAP 行与掩码时一模一样 ⇒ 没人翻过，不许当成译文（同 unmask 的规则）。"""
    stream = "⟦BLK-1⟧\n⟦CAP-0⟧ The residual plot.\n"

    assert export_stage.caption_translations(stream, BLOCKS) == {}
    assert export_stage.caption_translations(ZH_STREAM, BLOCKS) == {"The residual plot.": "残差图。"}


# ------------------------------------------------------------------ 自包含包


def test_pack_is_self_contained(paper):
    result = run(paper)
    pack = result.pack_dir

    assert (pack / "zh.tex").read_text("utf-8") == ZH_TEX, "与顶层契约文件逐字节相同"
    assert (pack / "custom.sty").is_file(), "缺 .sty 就编不了"
    assert (pack / "zh.bbl").is_file(), "预编译参考文献要随包走（免跑 bibtex）"
    assert (pack / "figs" / "plot.png").is_file(), "图目录不猜名字，编译看到什么就装什么"
    assert (pack / "fonts" / "LXGWWenKai-Light.ttf").is_file()
    assert "latexmk -xelatex zh.tex" in (pack / "README.md").read_text("utf-8")
    for stale in ("zh.log", "zh.aux", "zh.pdf", "zh.synctex.gz"):
        assert not (pack / stale).exists(), f"{stale} 是编译中间产物，不该进包"


def test_pack_can_skip_the_fonts(paper):
    """体积敏感场景可以不带字体——代价（豆腐）写进包内 README，默认仍是带。"""
    result = run(paper, bundle_fonts=False)

    assert not (result.pack_dir / "fonts").exists()
    assert (result.pack_dir / "zh.tex").is_file()
    assert "本包不带字体" in (result.pack_dir / "README.md").read_text("utf-8")
    assert result.pack_files < run(paper).pack_files
    assert result.pack_bytes > 0, "体积照样记账（云上批量存包时看这个数）"


# ------------------------------------------------------------------- anchors


def test_anchors_degrade_without_synctex(paper):
    result = run(paper)

    assert result.anchors is not None and result.anchors.synctex_used is False
    assert not (paper.out / "zh.synctex.gz").exists()
    document = json.loads((paper.out / "anchors.json").read_text("utf-8"))
    assert {a["source"] for a in document["anchors"]} == {"blocks"}
    assert any("降级为页级" in w for w in result.warnings)


def test_synctex_is_shipped_and_used(paper):
    """有 synctex 就搬进包、并用它定位（真 xelatex 路径）。"""
    sample = (
        "SyncTeX Version:1\nInput:1:zh.tex\nUnit:1\nX Offset:0\nY Offset:0\nContent:\n"
        "{1\n[1,1:4736286,4241067:26214400,45613056,0\n"
        "h1,5:4736286,20000000:13107200,655360,0\n]\n}1\n"
    )
    _write(paper.build / "zh" / "zh.synctex.gz", gzip.compress(sample.encode("utf-8")))

    result = run(paper)

    assert (paper.out / "zh.synctex.gz").is_file()
    assert result.anchors is not None and result.anchors.synctex_used is True
    document = json.loads((paper.out / "anchors.json").read_text("utf-8"))
    assert validate_schema(document, load_schema("anchors")) == []
    assert "synctex" in {a["source"] for a in document["anchors"]}


def test_anchors_use_the_span_file_left_by_compile(paper):
    """compile 落的 `build/zh-spans.json` 是精确输入：块指到哪一行由它说了算。"""
    sample = (
        "SyncTeX Version:1\nInput:1:zh.tex\nUnit:1\nX Offset:0\nY Offset:0\nContent:\n"
        "{1\n[1,1:4736286,4241067:26214400,45613056,0\n"
        # 第 4 行（图块所在行）有一个盒子；文本查找那条路会把图块定位到第 5 行
        "h1,4:4736286,20000000:13107200,655360,0\n]\n}1\n"
    )
    _write(paper.build / "zh" / "zh.synctex.gz", gzip.compress(sample.encode("utf-8")))
    figure_at = ZH_TEX.index("\\begin{figure}")
    # 故意把区间往前挪一行（指向「正文一段。」），证明用的是这份文件而不是文本查找
    prose_at = ZH_TEX.index("正文一段。")
    _write(
        paper.build / "zh-spans.json",
        {"tex": "zh.tex", "blocks": {"BLK-1": [prose_at, prose_at + len("正文一段。")]}},
    )

    result = run(paper)

    figure = next(a for a in result.anchors.anchors if a.block_id == "BLK-1")
    assert figure.source == "synctex", "区间指向第 4 行，那里正好有 synctex 盒子"
    # 不给这份文件（旧产物）时退回文本查找：图块回到第 5 行，没有盒子 ⇒ 页级降级
    (paper.build / "zh-spans.json").unlink()
    fallback = next(a for a in run(paper).anchors.anchors if a.block_id == "BLK-1")
    assert fallback.source == "blocks"
    assert ZH_TEX.count("\n", 0, figure_at) + 1 == 5  # 文本查找看到的是第 5 行


def test_a_stale_synctex_is_removed_from_the_package(paper):
    """上一轮的 synctex 不许留在包里，被当成这一轮的映射。"""
    _write(paper.out / "zh.synctex.gz", b"stale")

    run(paper)

    assert not (paper.out / "zh.synctex.gz").exists()


# --------------------------------------------------------------------- 失败


def test_missing_pdf_is_a_failure(paper):
    """`zh.pdf` 是产物契约的核心：没有它就不是包，不许假装出了包。"""
    (paper.build / "zh" / "zh.pdf").unlink()

    result = run(paper)

    assert result.status == export_stage.FAILED
    assert "zh.pdf" in result.message
    assert not (paper.out / "report.json").exists()


def test_a_schema_violation_fails_the_stage(paper):
    """自家组装的产物不合契约 = 通途自己违约，当场判失败并在 report 里记明白。"""
    blocks = {**BLOCKS, "blocks": [{"id": "BLK-9", "nonsense": True}]}
    _write(paper.build / "blocks.json", blocks)

    result = run(paper)

    assert result.status == export_stage.FAILED
    assert [a.path for a in result.invalid] == ["blocks.json"]
    assert "blocks.json" in result.message
    report = json.loads((paper.out / "report.json").read_text("utf-8"))
    assert report["status"] == "failed", "报告不许说自己没事"
    assert validate_schema(report, load_schema("report")) == [], "报告本身仍要合契约"
    entry = next(a for a in report["artifacts"] if a["path"] == "blocks.json")
    assert entry["schema_valid"] is False


def test_missing_upstream_products_are_warned_not_crashed(paper):
    """少一份上游产物只该记警告（并让 schema 那一关说话），不该抛栈。"""
    (paper.build / "brief.json").unlink()

    result = run(paper)

    assert result.ok, result.message
    assert any("brief.json" in w for w in result.warnings)
    assert not (paper.out / "brief.json").exists()
