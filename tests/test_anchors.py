"""anchors 三来源合成的测试（架构 §7 anchors 行、附录 B 开放问题 4）。

三条主线：

1. **synctex 解析**：本机没有真 `.synctex.gz`（要真 xelatex 才有），故用**手写的最小合法
   样本**——格式按 synctex 规范造，覆盖头部、`Input` 映射、页面块与盒子记录；
2. **降级路径**：没有 synctex 时锚点必须退化为页级并**如实标注来源**，绝不伪造精确矩形
   （假编译器路径下 e2e 走的正是这一条）；
3. **契约**：产物过 `docs/schemas/anchors.schema.json`。
"""

from __future__ import annotations

import gzip
import json
import math

import pytest

from tongtu import CONTRACT_VERSION, anchors
from tongtu.schema_check import load_schema, validate_schema

# --------------------------------------------------------------------------- #
# 手写的最小合法 synctex 样本
#
# 格式（synctex v1）：头部若干 `键:值` 行 + `Content:` + 页面块。页面以 `{<页码>` 开始、
# `}<页码>` 结束；盒子记录形如 `<类型><tag>,<行>:<x>,<y>:<宽>,<高>,<深>`，坐标是相对
# 页面**左上角**的 sp（TeX 小点），`y` 落在基线上。
# --------------------------------------------------------------------------- #

SYNCTEX_SAMPLE = """SyncTeX Version:1
Input:1:zh.tex
Input:2:/usr/share/texlive/tex/latex/base/article.cls
Output:pdf
Magnification:1000.00
Unit:1
X Offset:0
Y Offset:0
Content:
{1
[1,1:4736286,4241067:26214400,45613056,0
(1,4:4736286,10000000:26214400,655360,196608
h1,4:4736286,10000000:13107200,655360,196608
h1,5:4736286,11000000:13107200,655360,0
)
g1,4:4736286,10000000
]
}1
{2
[2,1:4736286,4241067:26214400,45613056,0
h1,9:4736286,20000000:6553600,655360,0
]
}2
Postamble:
Count:6
Post scriptum:
"""

PT = anchors.SYNCTEX_SCALE  # sp → pt


def gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


BLOCK_TEX = "\\begin{equation}\n  E = mc^2\n  \\label{eq:e}\n\\end{equation}"

ZH_TEX = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "\\section{Introduction}\n"
    f"{BLOCK_TEX}\n"
    "Some prose.\n"
    "\\end{document}\n"
)

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
            "category": "math",
            "tex": BLOCK_TEX,
            "label": "eq:e",
            "span": {"start": 60, "end": 120},
        },
    ],
    "captions": [],
}


# --------------------------------------------------------------- synctex 解析


def test_parse_synctex_reads_inputs_and_boxes():
    mapping = anchors.parse_synctex(gz(SYNCTEX_SAMPLE))

    assert mapping.version == "1"
    assert mapping.inputs[1] == "zh.tex"
    assert mapping.tag_for("zh.tex") == 1
    assert mapping.tag_for("article.cls") == 2  # 绝对路径也按 basename 认得出
    assert mapping.tag_for("nope.tex") is None
    # 只收得出矩形的四种记录：`[`、`(`、`h`、`v`；`g`（glue）没有尺寸，跳过。
    assert len(mapping.records) == 6
    assert {r.page for r in mapping.records} == {1, 2}


def test_parse_synctex_converts_to_pdf_points():
    """坐标换算：sp → pt，基线 + 高 + 深还原成上边沿矩形（origin=top-left）。"""
    mapping = anchors.parse_synctex(gz(SYNCTEX_SAMPLE))
    box = next(r for r in mapping.records if r.line == 5)

    assert math.isclose(box.rect.x, 4736286 * PT, rel_tol=1e-9)
    assert math.isclose(box.rect.w, 13107200 * PT, rel_tol=1e-9)
    # y = 基线 - 高；h = 高 + 深
    assert math.isclose(box.rect.y, (11000000 - 655360) * PT, rel_tol=1e-9)
    assert math.isclose(box.rect.h, 655360 * PT, rel_tol=1e-9)
    # 换算结果应当落在一页纸的量级里（1 in ≈ 72 pt 的页边距）
    assert 70 < box.rect.x < 74


def test_parse_synctex_survives_garbage():
    """认不出的行一律跳过——一条怪记录不该让整份映射作废。"""
    broken = SYNCTEX_SAMPLE.replace("h1,5:4736286,11000000:13107200,655360,0", "h1,5:这不是数字")
    mapping = anchors.parse_synctex(gz(broken))

    assert mapping.tag_for("zh.tex") == 1
    assert len(mapping.records) == 5


def test_parse_synctex_accepts_plain_text():
    """未压缩的 `.synctex` 也读（latexmk 不一定 gzip）。"""
    assert anchors.parse_synctex(SYNCTEX_SAMPLE.encode("utf-8")).records


def test_parse_synctex_rejects_broken_gzip():
    with pytest.raises(ValueError):
        anchors.parse_synctex(b"\x1f\x8b" + b"not really gzip")


# ------------------------------------------------------------------ pdf-scan


MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] >>\nendobj\n"
    b"%%EOF\n"
)


def test_scan_pdf_reads_pages_and_mediabox():
    info = anchors.scan_pdf(MINIMAL_PDF)

    assert info.page_count == 1
    assert info.parsed is True
    assert info.size(1) == pytest.approx((595.28, 841.89))


def test_scan_pdf_falls_back_when_nothing_readable():
    """假编译器的「PDF」里没有 /MediaBox：如实记 parsed=False，用兜底画布，不猜。"""
    info = anchors.scan_pdf(b"%PDF-1.4\nhello\n%%EOF\n")

    assert info.page_count == 1
    assert info.parsed is False
    assert info.size(1) == anchors.DEFAULT_PAGE_SIZE


# ------------------------------------------------------------- 源码位置工具


def test_block_line_spans_locates_blocks_in_zh_tex():
    spans = anchors.block_line_spans(ZH_TEX, anchors._normalize_blocks(BLOCKS)[0])

    assert spans["BLK-1"] == (4, 7)  # \begin{equation} 起，\end{equation} 止


def test_block_line_spans_survives_translated_captions():
    """caption 被翻译之后块 tex 不再逐字节出现，靠最长的无 CAP 片段仍能定位。"""
    blocks = {
        "blocks": [
            {
                "id": "BLK-1",
                "placeholder": "⟦BLK-1⟧",
                "category": "figure",
                "tex": "\\begin{figure}\n\\includegraphics{f}\n\\caption{⟦CAP-0⟧}\n\\end{figure}",
                "span": {"start": 0, "end": 1},
            }
        ],
        "captions": [],
    }
    text = "\\begin{document}\n\\begin{figure}\n\\includegraphics{f}\n\\caption{图一：流水线}\n\\end{figure}\n"

    spans = anchors.block_line_spans(text, anchors._normalize_blocks(blocks)[0])

    assert spans["BLK-1"][0] == 2


def test_block_line_spans_prefers_exact_offsets():
    """精确路径：compile 记下的字符区间直接换算成行号，不再拿块内容去反查。"""
    blocks = anchors._normalize_blocks(BLOCKS)[0]
    start = ZH_TEX.index(BLOCK_TEX)
    offsets = {"BLK-1": (start, start + len(BLOCK_TEX))}

    spans = anchors.block_line_spans(ZH_TEX, blocks, offsets)

    assert spans["BLK-1"] == (4, 7) == anchors.block_line_spans(ZH_TEX, blocks)["BLK-1"]


def test_exact_offsets_beat_text_search_on_repeated_blocks():
    """两个块内容一模一样时文本查找只能靠游标，精确区间各归各位。"""
    text = "\\begin{document}\n\\begin{equation}a=b\\end{equation}\nProse.\n\\begin{equation}a=b\\end{equation}\n"
    same = "\\begin{equation}a=b\\end{equation}"
    blocks = anchors._normalize_blocks(
        {
            "blocks": [
                {"id": "BLK-1", "placeholder": "⟦BLK-1⟧", "category": "math", "tex": same},
                {"id": "BLK-2", "placeholder": "⟦BLK-2⟧", "category": "math", "tex": same},
            ]
        }
    )[0]
    second = text.rindex(same)
    offsets = {"BLK-2": (second, second + len(same))}

    spans = anchors.block_line_spans(text, blocks, offsets)

    assert spans["BLK-2"] == (4, 4), "精确区间指向第二处"
    assert spans["BLK-1"] == (2, 2), "没给区间的块走兼容分支（文本查找）"


def test_block_line_spans_falls_back_when_offsets_are_unusable():
    """越界的区间（产物与 zh.tex 对不上）不信，退回文本查找。"""
    blocks = anchors._normalize_blocks(BLOCKS)[0]

    spans = anchors.block_line_spans(ZH_TEX, blocks, {"BLK-1": (10**6, 10**6 + 5)})

    assert spans["BLK-1"] == (4, 7)


def test_block_char_spans_reads_the_file_and_skips_garbage(tmp_path):
    path = tmp_path / "zh-spans.json"
    path.write_text(
        json.dumps(
            {
                "tex": "zh.tex",
                "blocks": {
                    "BLK-1": [3, 9],
                    "BLK-2": "不是区间",
                    "BLK-3": [5],
                    "BLK-4": [9, 3],  # 止在起之前
                },
            }
        ),
        encoding="utf-8",
    )

    assert anchors.block_char_spans(path) == {"BLK-1": (3, 9)}
    assert anchors.block_char_spans(tmp_path / "nope.json") == {}
    assert anchors.block_char_spans(None) == {}
    assert anchors.block_char_spans({"BLK-1": [0, 2]}) == {"BLK-1": (0, 2)}


def test_build_consumes_exact_spans():
    """`build(spans=…)` 走精确路径：块指到哪一行由区间说了算。"""
    start = ZH_TEX.index(BLOCK_TEX)
    result = anchors.build(
        zh_tex=ZH_TEX,
        blocks=BLOCKS,
        pdf=MINIMAL_PDF,
        synctex=gz(SYNCTEX_SAMPLE),
        spans={"blocks": {"BLK-1": [start, start + len(BLOCK_TEX)]}},
    )

    block = next(a for a in result.anchors if a.block_id == "BLK-1")
    assert block.source == "synctex"
    assert (
        block.rects
        == next(
            a
            for a in anchors.build(zh_tex=ZH_TEX, blocks=BLOCKS, pdf=MINIMAL_PDF, synctex=gz(SYNCTEX_SAMPLE)).anchors
            if a.block_id == "BLK-1"
        ).rects
    )


def test_sections_are_scanned_from_zh_tex():
    found = anchors.sections_in(ZH_TEX)

    assert [s.title for s in found] == ["Introduction"]
    assert found[0].line == 3


# --------------------------------------------------------------------- 合成


def test_synctex_gives_precise_anchors():
    result = anchors.build(zh_tex=ZH_TEX, blocks=BLOCKS, pdf=MINIMAL_PDF, synctex=gz(SYNCTEX_SAMPLE))

    assert result.synctex_used is True
    block = next(a for a in result.anchors if a.block_id == "BLK-1")
    assert block.source == "synctex"
    assert block.confidence == anchors.CONFIDENCE["synctex"]
    assert block.type == "equation" and block.label == "eq:e"
    # 精确矩形：不是整页，且落在页内
    rect = block.rects[0]
    assert 0 < rect.w < 595.28 and 0 < rect.h < 841.89
    assert rect.x >= 0 and rect.y >= 0
    # 前导区不产锚点（它根本不排版）
    assert all(a.block_id != "BLK-0" for a in result.anchors)


def test_multi_page_object_gets_one_anchor_per_page():
    """跨页对象每页一条锚点，id 不许撞。"""
    sample = SYNCTEX_SAMPLE.replace("h1,9:", "h1,4:")
    result = anchors.build(zh_tex=ZH_TEX, blocks=BLOCKS, pdf=MINIMAL_PDF, synctex=gz(sample))

    ids = [a.id for a in result.anchors if a.block_id == "BLK-1"]
    assert ids == ["BLK-1", "BLK-1@p2"]
    assert len(set(ids)) == len(ids)


def test_missing_synctex_degrades_to_page_anchors():
    """没有 synctex → 页级锚点：整页矩形、来源如实记 blocks、置信度压低。"""
    result = anchors.build(zh_tex=ZH_TEX, blocks=BLOCKS, pdf=MINIMAL_PDF, synctex=None)

    assert result.synctex_used is False
    assert result.degraded is True
    assert {a.source for a in result.anchors} == {"blocks"}
    for anchor in result.anchors:
        assert anchor.confidence == anchors.CONFIDENCE["blocks"]
        assert anchor.rects[0].w == pytest.approx(595.28)
        assert anchor.rects[0].h == pytest.approx(841.89)
    assert any("降级为页级" in w for w in result.warnings)


def test_unknown_input_file_also_degrades():
    """synctex 在，但里面没有 zh.tex 的 Input 记录 → 同样降级，并说清原因。"""
    sample = SYNCTEX_SAMPLE.replace("Input:1:zh.tex", "Input:1:other.tex")
    result = anchors.build(zh_tex=ZH_TEX, blocks=BLOCKS, pdf=MINIMAL_PDF, synctex=gz(sample))

    assert result.synctex_used is False
    assert {a.source for a in result.anchors} == {"blocks"}
    assert any("Input 记录" in w for w in result.warnings)


def test_chunk_ids_come_from_translation_memory():
    chunks = {"chunks": [{"id": "c007", "src": "前文 ⟦BLK-1⟧ 后文"}]}
    result = anchors.build(zh_tex=ZH_TEX, blocks=BLOCKS, pdf=MINIMAL_PDF, synctex=None, chunks=chunks)

    block = next(a for a in result.anchors if a.block_id == "BLK-1")
    assert block.chunk_id == "c007"


def test_anchors_json_passes_schema():
    for synctex in (None, gz(SYNCTEX_SAMPLE)):
        document = anchors.build(zh_tex=ZH_TEX, blocks=BLOCKS, pdf=MINIMAL_PDF, synctex=synctex).to_anchors_json()

        assert validate_schema(document, load_schema("anchors")) == []
        assert document["contract_version"] == CONTRACT_VERSION
        assert document["coordinate_system"] == {"origin": "top-left", "unit": "pt"}
        # 可 JSON 序列化（float 不许出 NaN/Inf）
        assert "NaN" not in json.dumps(document)


def test_page_estimate_is_monotonic():
    """页级锚点的页码是**估计**：单调、落在 [1, page_count] 内即可（开放问题 4）。"""
    assert anchors._estimate_page(1, 100, 10) == 1
    assert anchors._estimate_page(100, 100, 10) == 10
    assert 1 <= anchors._estimate_page(50, 100, 10) <= 10
    assert anchors._estimate_page(50, 100, 1) == 1
