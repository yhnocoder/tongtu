"""unmask 的文本层测试：参数化回填、caption 回填规则、完整性校验（架构 §3、决策 11）。

往返恒等本身在 `test_mask.py` 里测；这里测的是「回填怎么被参数化」——survey 的选择性
回填视图、译文与原文的取舍、以及流被译坏时的机械报错。
"""

import json
from pathlib import Path

import pytest

from tongtu.stages.mask import mask
from tongtu.stages.unmask import (
    Restore,
    UnmaskError,
    survey_view,
    unmask,
    unmask_detail,
)

DATA = Path(__file__).resolve().parent / "data" / "mask"

SRC = """\\documentclass{article}
\\title{Round Trips}
\\begin{document}
\\section{Intro}% side note
Prose with $x$ and \\cite{a}.
\\begin{equation}\\label{eq}a=b\\end{equation}
\\begin{table}\\caption{Results}\\end{table}
\\begin{lstlisting}
code
\\end{lstlisting}
\\begin{tikzpicture}\\draw(0,0);\\end{tikzpicture}
\\end{document}
"""


@pytest.fixture
def masked():
    return mask(SRC)


def categories(result):
    return {b.category: b.placeholder for b in result.blocks}


# ------------------------------------------------------------- 参数化回填


def test_default_restores_everything(masked):
    assert unmask(masked.masked, masked) == SRC


def test_selective_restore_math_only(masked):
    out = unmask(
        masked.masked,
        masked,
        restore={"table": Restore.PLACEHOLDER, "code": Restore.PLACEHOLDER},
        strict=False,
    )
    assert "\\begin{equation}\\label{eq}a=b\\end{equation}" in out  # 数学回填原文
    assert categories(masked)["table"] in out  # 表格保持占位符
    assert categories(masked)["code"] in out
    assert "code" not in out.replace("⟦BLK", "")


def test_restore_accepts_a_callable(masked):
    out = unmask(
        masked.masked,
        masked,
        restore=lambda block: Restore.DROP if block.category == "code" else Restore.ORIGINAL,
    )
    assert "lstlisting" not in out
    assert "\\begin{table}" in out


def test_survey_view_matches_the_architecture_table(masked):
    view = survey_view(masked.masked, masked)
    assert "\\begin{equation}\\label{eq}a=b\\end{equation}" in view  # 数学回填
    for category in ("table", "code", "tikz"):
        assert categories(masked)[category] in view  # 表格/代码/tikz 保持占位符
    assert "\\documentclass" not in view  # 前导区删掉
    assert "% side note" not in view  # 注释块删掉
    assert "⟦CAP-1⟧ Results" in view  # caption 行留着给通读
    assert "Prose with $x$" in view


def test_survey_stats_are_reported(masked):
    detail = unmask_detail(
        masked.masked, masked, restore="survey", caption_mode="keep", strict=False
    )
    assert set(detail.dropped) >= {"BLK-0"}
    assert detail.kept  # 保持占位符的块数 > 0
    assert not detail.missing


def test_unknown_restore_preset_is_rejected(masked):
    with pytest.raises(UnmaskError):
        unmask(masked.masked, masked, restore="whatever")
    with pytest.raises(UnmaskError):
        unmask(masked.masked, masked, caption_mode="whatever")


# --------------------------------------------------------------- caption


def test_translated_caption_wins(masked):
    stream = masked.masked.replace("⟦CAP-1⟧ Results", "⟦CAP-1⟧ 实验结果")
    detail = unmask_detail(stream, masked)
    assert "\\caption{实验结果}" in detail.text
    assert detail.caption_translated == ("CAP-1",)


def test_untouched_caption_falls_back_to_the_byte_exact_original():
    """v2 把 caption 单行化后就回不去了；这里未改动的槽位必须回填原文。"""
    src = (
        "\\documentclass{a}\n\\begin{document}\n"
        "\\begin{figure}\\caption{Two lines\n  and a %% comment\n}\\end{figure}\n\\end{document}\n"
    )
    result = mask(src)
    assert "\n" not in result.masked.split("⟦CAP-0⟧")[1].split("\n")[0]  # 流里是一行
    assert unmask(result.masked, result) == src  # 回填的是原文，不是那一行


def test_missing_caption_line_falls_back_to_the_original(masked):
    stream = masked.masked.replace("⟦CAP-1⟧ Results", "")
    detail = unmask_detail(stream, masked)
    assert "\\caption{Results}" in detail.text
    assert "CAP-1" in detail.caption_fallbacks


def test_emptied_caption_line_falls_back_to_the_original(masked):
    stream = masked.masked.replace("⟦CAP-1⟧ Results", "⟦CAP-1⟧")
    assert "\\caption{Results}" in unmask(stream, masked)


def test_caption_mode_keep_leaves_the_lines_alone(masked):
    out = unmask(masked.masked, masked, caption_mode="keep", strict=False)
    assert "⟦CAP-1⟧ Results" in out
    assert "\\caption{Results}" in out  # 块里的槽位仍填原文


# --------------------------------------------------------------- 块区间


def test_block_spans_point_at_the_restored_text(masked):
    detail = unmask_detail(masked.masked, masked)

    assert set(detail.block_spans) == {b.id for b in masked.blocks}
    for block in masked.blocks:
        start, end = detail.block_spans[block.id]
        if "⟦CAP-" in block.tex:
            continue  # caption 槽位另有下一个用例
        assert detail.text[start:end] == block.tex


def test_block_spans_follow_a_translated_caption(masked):
    """caption 被翻译后块文本变长/变短，区间必须跟着走（这正是文本查找做不到的那一档）。"""
    stream = masked.masked.replace("⟦CAP-1⟧ Results", "⟦CAP-1⟧ 实验结果（更长的中文标题）")
    detail = unmask_detail(stream, masked)

    table = next(b for b in masked.blocks if b.category == "table")
    start, end = detail.block_spans[table.id]
    assert detail.text[start:end] == table.tex.replace("⟦CAP-1⟧", "实验结果（更长的中文标题）")
    assert "实验结果（更长的中文标题）" in detail.text[start:end]
    # 其后的块也要跟着位移，不能停在回填前的旧偏移上
    for block in masked.blocks:
        if "⟦CAP-" not in block.tex:
            first, last = detail.block_spans[block.id]
            assert detail.text[first:last] == block.tex


def test_block_spans_survive_repeated_block_text():
    """两个块的 tex 一模一样时，区间必须各归各位（文本查找只能靠游标猜）。"""
    src = (
        "\\documentclass{a}\n\\begin{document}\n"
        "\\begin{equation}a=b\\end{equation}\n"
        "Prose between.\n"
        "\\begin{equation}a=b\\end{equation}\n"
        "\\end{document}\n"
    )
    result = mask(src)
    maths = [b for b in result.blocks if b.category == "math"]
    assert len(maths) == 2 and maths[0].tex == maths[1].tex

    detail = unmask_detail(result.masked, result)
    first, second = detail.block_spans[maths[0].id], detail.block_spans[maths[1].id]

    assert first != second and first[0] < second[0]
    assert detail.text[first[0] : first[1]] == maths[0].tex
    assert detail.text[second[0] : second[1]] == maths[1].tex
    assert detail.text.index("Prose between.") > first[1]
    assert detail.text.index("Prose between.") < second[0]


def test_survey_view_records_kept_placeholders_and_skips_dropped(masked):
    """选择性回填如实记：保持占位符的记占位符本身，删掉的块不记（空区间只会误导下游）。"""
    detail = unmask_detail(
        masked.masked, masked, restore="survey", caption_mode="keep", strict=False
    )

    by_id = {b.id: b for b in masked.blocks}
    for block_id, (start, end) in detail.block_spans.items():
        block = by_id[block_id]
        expected = block.placeholder if block.id in detail.kept else block.tex
        assert detail.text[start:end] == expected
    assert set(detail.dropped).isdisjoint(detail.block_spans), "删掉的块不记区间"
    assert set(detail.kept) <= set(detail.block_spans)


def test_duplicated_placeholder_records_only_the_first_occurrence(masked):
    """重复使用（非 strict）时记第一次——重复本身由 `duplicated` 交代。"""
    math = next(b for b in masked.blocks if b.category == "math")
    stream = masked.masked + math.placeholder
    detail = unmask_detail(stream, masked, strict=False)

    start, end = detail.block_spans[math.id]
    assert detail.text[start:end] == math.tex
    assert detail.text.rindex(math.tex) > start, "第二次出现在后面，记的是第一次"
    assert math.placeholder in detail.duplicated


# ----------------------------------------------------------------- 完整性


def test_missing_block_placeholder_is_an_error(masked):
    stream = masked.masked.replace(categories(masked)["math"], "")
    with pytest.raises(UnmaskError, match="丢失"):
        unmask(stream, masked)


def test_duplicated_block_placeholder_is_an_error(masked):
    stream = masked.masked + categories(masked)["math"]
    with pytest.raises(UnmaskError, match="重复"):
        unmask(stream, masked)


def test_unknown_block_placeholder_is_an_error(masked):
    with pytest.raises(UnmaskError, match="未知块占位符"):
        unmask(masked.masked + "⟦BLK-999⟧", masked)


def test_stray_caption_token_in_prose_is_an_error(masked):
    stream = masked.masked.replace("Prose with", "⟦CAP-9⟧ Prose with")
    with pytest.raises(UnmaskError):
        unmask(stream, masked)


def test_non_strict_mode_tolerates_partial_streams(masked):
    """translate 的分块内环只看得到一段流，不能因为「缺块」就报错。"""
    piece = "Prose with $x$ and \\cite{a}.\n" + categories(masked)["math"]
    out = unmask(piece, masked, strict=False)
    assert out.startswith("Prose with")
    assert "\\begin{equation}" in out


# ------------------------------------------------------- 从 blocks.json 走


@pytest.mark.parametrize("path", sorted(DATA.glob("*.tex")), ids=lambda p: p.stem)
def test_roundtrip_through_serialized_blocks_json(path):
    src = path.read_text(encoding="utf-8")
    result = mask(src)
    document = json.loads(json.dumps(result.to_blocks_json(), ensure_ascii=False))
    assert unmask(result.masked, document) == src


def test_unmask_rejects_garbage_block_list(masked):
    with pytest.raises(UnmaskError):
        unmask(masked.masked, object())
