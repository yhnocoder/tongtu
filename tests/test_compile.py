"""compile 阶段：回填 → 注入 → latexmk 回环 → 失败分诊（架构 §3 compile 行、决策 13）。

**本机没有 TeX 也要能测**：编译封在 `tongtu.compiler.Compiler` 接口后面，这里用可编程的
假编译器——「谁在 tex 里出现就编不过」的谓词式失败模式，让块 → 段落两级二分、坏段回退、
重译救活、预算超限、全局问题走关节⑥这些逻辑全部可在秒级验证。假编译器把 tex 原文写进
「PDF」，于是「PDF 里到底进了译文还是原文」是可断言的。

真 latexmk 只是一层薄封装，单独一小节测，`skipif(没装 latexmk)`。
"""

import shutil
from pathlib import Path

import pytest

from tongtu.compiler import CompileRunResult, detect_engine, latexmk_compiler, parse_log
from tongtu.stages import compile as cp
from tongtu.stages.mask import MaskResult
from tongtu.workdir import Workdir

PREAMBLE = "\\documentclass{article}\n\\usepackage{amsmath}\n\\begin{document}"
END = "\\end{document}\n"

#: 假编译器的「炸弹」标记：出现在 tex 里就编不过。
BOMB = "BADSEG"


# --------------------------------------------------------------------------- #
# 夹具：可编程假编译器 + 合成的原译块清单
# --------------------------------------------------------------------------- #


def compiler(
    predicate=lambda text: BOMB in text,
    *,
    error="! Undefined control sequence.",
    line=999,
):
    """假编译器。`predicate(tex文本)` 为 True 即编不过；成功时把 tex 写进「PDF」。

    `line` 是日志里 `l.<N>` 的行号——分诊靠它判断错误是否落在前导区，默认取一个远大于
    `\\begin{document}` 行号的值（= 正文错误）。
    """
    calls: list[str] = []

    def run(tex: Path, build_dir: Path) -> CompileRunResult:
        text = tex.read_text(encoding="utf-8")
        calls.append(text)
        pdf = build_dir / f"{tex.stem}.pdf"
        if predicate(text):
            if pdf.exists():
                pdf.unlink()
            log = f"{error}\nl.{line} \\foo\n" if line is not None else f"{error}\n"
            return CompileRunResult(ok=False, log=log, returncode=12, engine="xelatex")
        pdf.write_text(text, encoding="utf-8")
        return CompileRunResult(
            ok=True,
            pdf=pdf,
            log="This is XeTeX\nOutput written on zh.pdf\n",
            returncode=0,
            engine="xelatex",
        )

    run.calls = calls  # type: ignore[attr-defined]
    return run


def build_units(*, chunks=3, paragraphs=4, bombs=(), preamble=PREAMBLE):
    """合成块清单：每块 `paragraphs` 段，`bombs` 里的 (块, 段) 在**译文**里埋炸弹。

    第 0 块前面挂导言区（自成一段，故其正文段落序号从 1 起——真实论文的 front matter
    也是这样），最后一块末尾挂 `\\end{document}`。
    """
    units = []
    for c in range(chunks):
        cid = f"c{c:03d}"
        src = [f"EN-{cid}-p{p} lorem ipsum dolor sit amet." for p in range(paragraphs)]
        trans = [f"ZH-{cid}-p{p} {BOMB + ' ' if (c, p) in bombs else ''}这是译文内容。" for p in range(paragraphs)]
        source = "\n\n".join(src) + "\n\n"
        translation = "\n\n".join(trans) + "\n\n"
        if c == 0:
            source = preamble + "\n\n" + source
            translation = preamble + "\n\n" + translation
        if c == chunks - 1:
            source += END
            translation += END
        units.append(cp.TranslatedChunk(id=cid, source=source, translation=translation, section=f"第{c}节"))
    return units


def masked_of(units) -> MaskResult:
    """把块原文拼回掩码流。测试不需要真掩码块——回填对无占位符的流是恒等变换。"""
    return MaskResult(masked="".join(u.source for u in units), blocks=(), captions=(), environments=())


@pytest.fixture
def paper(tmp_path) -> Workdir:
    return Workdir(path=tmp_path / "work" / "2401.01234", arxiv_id="2401.01234").create()


def run_compile(paper, units, **kwargs):
    return cp.compile_zh(paper, units, masked_of(units), **kwargs)


def pdf_text(result) -> str:
    assert result.pdf is not None and result.pdf.is_file()
    return result.pdf.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 段落工具与输入规范化
# --------------------------------------------------------------------------- #


def test_paragraph_pieces_rejoin_exactly():
    text = "\n\n  首段。\n\n\n  二段\n续行\n\n三段  \n\n"
    pieces, at = cp.paragraph_pieces(text)
    assert "".join(pieces) == text  # 原样重组是段落替换的地基
    assert len(at) == 3
    assert pieces[at[1]].strip().startswith("二段")


def test_paragraph_pieces_agrees_with_validate():
    from tongtu import validate

    for text in ("a\n\nb\n\nc", "\n\n单段\n\n", "x", "", "a\n\n\n\nb\n"):
        assert len(cp.paragraph_pieces(text)[1]) == validate.paragraph_count(text)


def test_normalize_units_accepts_string_triple_and_mapping():
    units = build_units(chunks=2, paragraphs=2)
    mask = masked_of(units)

    whole = cp.normalize_units("译文流", mask)
    assert len(whole) == 1 and whole[0].id == cp.WHOLE_ID
    assert whole[0].source == mask.masked and whole[0].translation == "译文流"

    triples = cp.normalize_units([("c000", "EN", "ZH")], mask)
    assert triples[0] == cp.TranslatedChunk(id="c000", source="EN", translation="ZH")

    mapped = cp.normalize_units([{"id": "c009", "source": "EN", "translation": "ZH"}], mask)
    assert mapped[0].id == "c009"

    with pytest.raises(cp.CompileError):
        cp.normalize_units([{"id": "c000"}], mask)


# --------------------------------------------------------------------------- #
# 一次通过
# --------------------------------------------------------------------------- #


def test_first_pass_ok(paper):
    units = build_units()
    result = run_compile(paper, units, compiler=compiler())

    assert result.status == cp.OK and result.ok
    assert result.passes == 1 and result.probes == 0
    assert result.fallbacks == () and result.retranslated == ()
    assert result.pdf is not None and result.pdf.is_file()
    assert result.tex == paper.build / "zh" / "zh.tex"
    assert result.raw_tex == paper.build / cp.RAW_NAME and result.raw_tex.is_file()
    # 决策 13：中间产物 zh-raw.tex 留在 build/ 供调试，注入摘要进结果
    assert result.inject["branch"] == "inject" and result.inject["engine"] == "xelatex"
    assert "xeCJK" in pdf_text(result)
    assert (paper.build / "zh" / "fonts").exists()


def test_whole_stream_input_also_works(paper):
    units = build_units(chunks=1, paragraphs=3)
    mask = masked_of(units)
    result = cp.compile_zh(paper, units[0].translation, mask, compiler=compiler())

    assert result.status == cp.OK
    assert "ZH-c000-p1" in pdf_text(result)


# --------------------------------------------------------------------------- #
# 坏段：块 → 段落两级二分
# --------------------------------------------------------------------------- #


def test_single_bad_paragraph_is_located_and_reverted(paper):
    units = build_units(bombs={(1, 2)})
    result = run_compile(paper, units, compiler=compiler())

    assert result.status == cp.OK_WITH_FALLBACK and result.ok
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [("c001", (2,))]
    assert result.fallbacks[0].reason == "compile_failed"
    assert result.fallbacks[0].section == "第1节"

    text = pdf_text(result)
    assert BOMB not in text
    assert "ZH-c001-p2" not in text  # 坏段的译文被换掉了
    assert "EN-c001-p2 lorem" in text  # 换成了原文段
    for kept in ("ZH-c001-p1", "ZH-c001-p3", "ZH-c000-p0", "ZH-c002-p0"):
        assert kept in text  # 其余译文一段没伤
    assert not result.budget_exhausted


def test_multiple_bad_paragraphs_in_different_chunks(paper):
    units = build_units(bombs={(1, 2), (2, 0)})
    result = run_compile(paper, units, compiler=compiler(), budget=40)

    assert result.status == cp.OK_WITH_FALLBACK
    assert sorted((f.chunk_id, f.paragraphs) for f in result.fallbacks) == [
        ("c001", (2,)),
        ("c002", (0,)),
    ]
    text = pdf_text(result)
    assert BOMB not in text
    assert "EN-c001-p2" in text and "EN-c002-p0" in text
    assert "ZH-c001-p3" in text and "ZH-c002-p1" in text


def test_two_bad_paragraphs_in_one_chunk(paper):
    units = build_units(bombs={(1, 0), (1, 3)})
    result = run_compile(paper, units, compiler=compiler(), budget=40)

    assert result.status == cp.OK_WITH_FALLBACK
    # 同一块的多个坏段合成一条记录（schema 的 paragraphs 是数组）
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [("c001", (0, 3))]
    text = pdf_text(result)
    assert "EN-c001-p0" in text and "EN-c001-p3" in text
    assert "ZH-c001-p1" in text and "ZH-c001-p2" in text


def test_chunk_whose_paragraphs_do_not_line_up_falls_back_whole(paper):
    """validate 保证原译段落一一对应；万一没对上，只能整块回退（绝不猜段落对应）。"""
    units = build_units(chunks=2, paragraphs=3)
    broken = units[1]
    units[1] = cp.TranslatedChunk(
        id=broken.id,
        source=broken.source,
        # 译文比原文多出一段：段落对不上，段落级二分无从谈起
        translation=f"{BOMB} 多出来的一段\n\n" + broken.translation,
        section=broken.section,
    )
    result = run_compile(paper, units, compiler=compiler(), budget=20)

    assert result.status == cp.OK_WITH_FALLBACK
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [("c001", ())]
    assert any("段落数对不上" in w for w in result.warnings)
    assert BOMB not in pdf_text(result)


def test_budget_exhaustion_falls_back_whole_chunks(paper):
    units = build_units(bombs={(1, 2)})
    # 预算 2 = 一次恒等回填分诊 + 一次块探测，不够下钻到段落
    result = run_compile(paper, units, compiler=compiler(), budget=2)

    assert result.status == cp.OK_WITH_FALLBACK
    assert result.budget_exhausted is True
    assert result.probes == 2  # 预算就这么多，一次也不多花
    # 超限 → 剩下的块整块回退（paragraphs 为空即整块），保守但保证出 PDF；
    # 已经单独验证过没问题的 c000 保住译文
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [
        ("c001", ()),
        ("c002", ()),
    ]
    text = pdf_text(result)
    assert BOMB not in text
    assert "ZH-c000-p0" in text and "EN-c001-p0" in text and "ZH-c001-p0" not in text


def test_zero_budget_delivers_the_identity_fallback(paper):
    """预算连分诊都不够也必须出 PDF：整篇回退原文，全部块记回退。"""
    units = build_units(bombs={(1, 2)})
    result = run_compile(paper, units, compiler=compiler(), budget=0)

    assert result.status == cp.OK_WITH_FALLBACK
    assert result.probes == 0 and result.budget_exhausted is True
    assert [f.chunk_id for f in result.fallbacks] == ["c000", "c001", "c002"]
    assert all(f.paragraphs == () for f in result.fallbacks)
    text = pdf_text(result)
    assert BOMB not in text and "EN-c001-p2" in text


def test_bad_segment_in_whole_stream_input(paper):
    """只给一整串译文时第一级二分退化为空转，第二级段落二分照常定位。"""
    units = build_units(chunks=1, paragraphs=5, bombs={(0, 3)})
    mask = masked_of(units)
    result = cp.compile_zh(paper, units[0].translation, mask, compiler=compiler(), budget=20)

    assert result.status == cp.OK_WITH_FALLBACK
    # 第 0 段是导言区，正文段落序号从 1 起，故坏段是 4
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [(cp.WHOLE_ID, (4,))]
    text = pdf_text(result)
    assert BOMB not in text and "EN-c000-p3" in text and "ZH-c000-p2" in text


def test_bisection_probe_count_stays_logarithmic(paper):
    units = build_units(chunks=8, paragraphs=8, bombs={(5, 6)})
    result = run_compile(paper, units, compiler=compiler(), budget=cp.DEFAULT_BUDGET)

    assert result.status == cp.OK_WITH_FALLBACK
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [("c005", (6,))]
    assert result.probes <= cp.DEFAULT_BUDGET and not result.budget_exhausted


def test_interaction_failure_escalates_to_whole_chunk_fallback(paper):
    """二分假设「坏项各自独立」；交互型失败时终局编译兜不住 → 坏块整块回退再编一次。"""
    units = build_units(chunks=3, paragraphs=4)

    def predicate(text):  # c001 只要有两段以上译文就炸
        return text.count("ZH-c001-p") >= 2

    result = run_compile(paper, units, compiler=compiler(predicate=predicate), budget=20)

    assert result.status == cp.OK_WITH_FALLBACK
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [("c001", ())]
    assert "整块回退" in result.fallbacks[0].detail
    text = pdf_text(result)
    assert "ZH-c001-p" not in text and "EN-c001-p0" in text
    assert "ZH-c000-p0" in text and "ZH-c002-p0" in text


# --------------------------------------------------------------------------- #
# 坏段重译（关节⑤复用）
# --------------------------------------------------------------------------- #


def test_retranslate_saves_a_segment(paper):
    units = build_units(bombs={(1, 2)})
    seen = []

    def retranslate(segment):
        seen.append(segment)
        return segment.translation.replace(BOMB + " ", "")

    result = run_compile(paper, units, compiler=compiler(), retranslate=retranslate, budget=20)

    assert result.status == cp.OK and result.fallbacks == ()
    assert result.retranslated == ("c001#2",)
    assert len(seen) == 1
    segment = seen[0]
    assert segment.chunk_id == "c001" and segment.para_index == 2
    assert segment.source.startswith("EN-c001-p2")
    assert "! Undefined control sequence." in segment.detail

    text = pdf_text(result)
    assert BOMB not in text
    assert "ZH-c001-p2" in text  # 重译版进了 PDF，原文没有回来
    assert "EN-c001-p2" not in text


def test_retranslate_that_stays_broken_falls_back(paper):
    units = build_units(bombs={(1, 2)})
    result = run_compile(
        paper,
        units,
        compiler=compiler(),
        retranslate=lambda segment: segment.translation + " 还是坏的",
        budget=20,
    )

    assert result.status == cp.OK_WITH_FALLBACK
    assert result.retranslated == ()
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [("c001", (2,))]
    assert "EN-c001-p2" in pdf_text(result)


def test_retranslate_rescues_one_of_two(paper):
    units = build_units(bombs={(1, 1), (2, 2)})

    def retranslate(segment):
        if segment.chunk_id == "c001":
            return segment.translation.replace(BOMB + " ", "")
        return segment.translation  # 原样返回 = 放弃

    result = run_compile(paper, units, compiler=compiler(), retranslate=retranslate, budget=40)

    assert result.status == cp.OK_WITH_FALLBACK
    assert result.retranslated == ("c001#1",)
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [("c002", (2,))]
    text = pdf_text(result)
    assert "ZH-c001-p1" in text and "EN-c002-p2" in text


def test_retranslate_exception_is_survivable(paper):
    units = build_units(bombs={(1, 2)})

    def boom(segment):
        raise RuntimeError("agent 挂了")

    result = run_compile(paper, units, compiler=compiler(), retranslate=boom, budget=20)

    assert result.status == cp.OK_WITH_FALLBACK
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [("c001", (2,))]
    assert any("agent 挂了" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# 全局问题与关节⑥
# --------------------------------------------------------------------------- #


def test_global_problem_without_session_fails(paper):
    """恒等回填也编不过 = 与译文无关的全局问题。没有关节⑥就只能失败。"""
    units = build_units()
    result = run_compile(paper, units, compiler=compiler(predicate=lambda text: True))

    assert result.status == cp.FAILED and not result.ok
    assert result.probes == 1  # 只花一次恒等回填探测就分诊清楚了
    assert "恒等回填" in result.message
    assert result.session_used == 0


def test_global_problem_session_fixes_then_loop_recompiles(paper):
    """关节⑥改完**直接回环重编译**，且不重新组装——否则会清掉 agent 的改动。"""
    units = build_units()
    fixed = []

    def predicate(text):
        return "\\usepackage{amsmath}" in text  # 假装这个包与注入块冲突

    def session(request):
        fixed.append(request)
        text = request.tex.read_text(encoding="utf-8")
        request.tex.write_text(text.replace("\\usepackage{amsmath}", "% 已删"), encoding="utf-8")

    result = run_compile(paper, units, compiler=compiler(predicate=predicate), session=session)

    assert result.status == cp.OK
    assert result.session_used == 1 and len(fixed) == 1
    request = fixed[0]
    assert request.joint == cp.JOINT == "fixup"
    assert "适配" in request.prompt and "documentclass" in request.prompt
    text = pdf_text(result)
    assert "% 已删" in text
    assert "ZH-c001-p2" in text  # 译文全须全尾地留着


def test_session_that_fixes_nothing_ends_in_failed(paper):
    units = build_units()
    calls = []
    result = run_compile(
        paper,
        units,
        compiler=compiler(predicate=lambda text: True),
        session=lambda request: calls.append(request),
    )

    assert result.status == cp.FAILED
    assert result.session_used == 1 and len(calls) == 1
    assert "仍编不过" in result.message


def test_preamble_error_skips_the_identity_probe(paper):
    """错误落在前导区 → 直接判全局问题，省掉恒等回填那次编译。"""
    units = build_units()
    calls = []
    result = run_compile(
        paper,
        units,
        compiler=compiler(
            predicate=lambda text: True,
            error="! LaTeX Error: File `sigconf.cls' not found.",
            line=3,
        ),
        session=lambda request: calls.append(request),
    )

    assert result.status == cp.FAILED
    assert result.probes == 0 and result.session_used == 1
    assert "前导区" in calls[0].prompt


def test_error_line_inside_preamble_counts_as_global(paper):
    units = build_units()
    result = run_compile(
        paper,
        units,
        compiler=compiler(predicate=lambda text: True, error="! Undefined control sequence.", line=2),
    )
    assert result.status == cp.FAILED and result.probes == 0 and "前导区" in result.message


def test_inject_failure_is_not_a_bad_segment(paper):
    """找不到 documentclass 时二分毫无意义——注入失败与译文无关。"""
    units = build_units(preamble="\\begin{document}")
    result = run_compile(paper, units, compiler=compiler())
    assert result.status == cp.FAILED and "inject_cjk" in result.message
    assert result.passes == 0


# --------------------------------------------------------------------------- #
# 「不比原文更糟」
# --------------------------------------------------------------------------- #


def test_paper_with_pre_existing_errors_is_not_judged_failed(paper):
    """原文自身就带 `!` 错误却出 PDF（真实论文常见）——判据放宽为不比原文更糟。"""
    units = build_units()

    def run(tex: Path, build_dir: Path) -> CompileRunResult:
        pdf = build_dir / f"{tex.stem}.pdf"
        pdf.write_text(tex.read_text(encoding="utf-8"), encoding="utf-8")
        return CompileRunResult(ok=False, pdf=pdf, returncode=12, log="! Undefined control sequence.\nl.999 x\n")

    result = cp.compile_zh(paper, units, masked_of(units), compiler=run)

    assert result.status == cp.OK
    assert result.fallbacks == ()
    assert any("不比原文更糟" in w for w in result.warnings)
    assert "ZH-c001-p2" in pdf_text(result)


def test_extra_errors_on_top_of_a_dirty_paper_are_still_bad_segments(paper):
    """原文带 1 个错误，译文段又添一个 → 相对判据照样定位得出坏段。"""
    units = build_units(bombs={(1, 2)})

    def run(tex: Path, build_dir: Path) -> CompileRunResult:
        text = tex.read_text(encoding="utf-8")
        errors = ["! Undefined control sequence."] + (["! Extra bomb."] if BOMB in text else [])
        pdf = build_dir / f"{tex.stem}.pdf"
        pdf.write_text(text, encoding="utf-8")
        return CompileRunResult(ok=False, pdf=pdf, returncode=12, log="\n".join(errors) + "\nl.999 x\n")

    result = cp.compile_zh(paper, units, masked_of(units), budget=20, compiler=run)

    assert result.status == cp.OK_WITH_FALLBACK
    assert [(f.chunk_id, f.paragraphs) for f in result.fallbacks] == [("c001", (2,))]
    assert BOMB not in pdf_text(result)


# --------------------------------------------------------------------------- #
# 块区间落盘（build/zh-spans.json）
# --------------------------------------------------------------------------- #

#: 带图与 caption 的小论文——回填后 caption 变成译文，块文本不再逐字节等于 blocks.json。
SPANS_SRC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "First prose paragraph.\n\n"
    "\\begin{figure}\\includegraphics{a}\\caption{Pipeline}\\end{figure}\n\n"
    "Second prose paragraph.\n\n"
    "\\begin{equation}a=b\\end{equation}\n\n"
    "\\end{document}\n"
)


def spans_paper(paper, *, translation=None, **kwargs):
    """把 `SPANS_SRC` 掩码后整流回填一次，返回 `(结果, mask 结果, 落盘的区间)`。"""
    import json

    from tongtu.stages.mask import mask

    masked = mask(SPANS_SRC)
    stream = masked.masked if translation is None else translation(masked.masked)
    result = cp.compile_zh(paper, stream, masked, **kwargs)
    path = paper.build / cp.SPANS_NAME
    spans = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    return result, masked, spans


def test_spans_file_locates_every_block_in_zh_tex(paper):
    """落盘的区间必须指向 `zh.tex` 里那一段——注入在导言区插了一整块，偏移不是常数。"""
    result, masked, spans = spans_paper(paper, compiler=compiler())

    assert result.status == cp.OK and result.spans_path == paper.build / cp.SPANS_NAME
    assert spans["tex"] == "zh.tex"
    tex = result.tex.read_text(encoding="utf-8")
    by_id = {b.id: b for b in masked.blocks}
    assert set(spans["blocks"]) == set(by_id)
    for block_id, (start, end) in spans["blocks"].items():
        block = by_id[block_id]
        if block.category == "preamble":
            # 注入点落在前导区块内部：区域如实把注入块也算进去（前导区不产锚点）
            assert tex[start:end].startswith("\\documentclass{article}")
            assert "injected by tongtu" in tex[start:end]
            assert tex[start:end].endswith("\\begin{document}")
            continue
        # caption 没被翻译 ⇒ 回填逐字节原文（unmask 的「未改动 ⇒ 原文」规则）
        expected = block.tex
        for placeholder, caption in masked.caption_map.items():
            expected = expected.replace(placeholder, caption.text)
        assert tex[start:end] == expected
    # 注入块确实插在了正文之前（否则这个用例证明不了偏移换算）
    figure = next(b for b in masked.blocks if b.category == "figure")
    assert spans["blocks"][figure.id][0] > SPANS_SRC.index("\\begin{figure}")


def test_spans_follow_a_translated_caption(paper):
    """caption 被译成中文之后，图块的区间跟着长——文本查找到这里就只能靠启发式了。"""
    result, masked, spans = spans_paper(
        paper,
        translation=lambda stream: stream.replace("⟦CAP-0⟧ Pipeline", "⟦CAP-0⟧ 流水线总览"),
        compiler=compiler(),
    )

    tex = result.tex.read_text(encoding="utf-8")
    figure = next(b for b in masked.blocks if b.category == "figure")
    start, end = spans["blocks"][figure.id]
    assert tex[start:end] == figure.tex.replace("⟦CAP-0⟧", "流水线总览")
    # 其后的块也在正确位置上（区间是按最终文本累计出来的，不是按块清单猜的）
    equation = next(b for b in masked.blocks if b.category == "math")
    first, last = spans["blocks"][equation.id]
    assert tex[first:last] == equation.tex


def test_spans_are_dropped_when_the_fixup_session_edits_zh_tex(paper):
    """关节⑥可以任意改 `zh.tex`，此时区间失效：不写、并删掉旧文件（宁可少一份精确输入）。"""

    def predicate(text):
        return "\\usepackage{amsmath}" not in text  # 第一次编不过，加了包才过

    def session(request):
        text = request.tex.read_text(encoding="utf-8")
        request.tex.write_text(
            text.replace("\\begin{document}", "\\usepackage{amsmath}\n\\begin{document}"),
            encoding="utf-8",
        )

    stale = paper.build / cp.SPANS_NAME
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"blocks": {"BLK-0": [0, 1]}}\n', encoding="utf-8")

    result, _, spans = spans_paper(paper, compiler=compiler(predicate=predicate), session=session)

    assert result.status == cp.OK and result.session_used == 1
    assert result.spans_path is None and spans is None
    assert not stale.is_file(), "上一次的区间必须删掉，错的区间比没有更糟"


def test_spans_file_is_absent_without_a_block_list(paper):
    """块清单为空（本文件多数用例的合成流）时无从记区间，如实不写。"""
    units = build_units(chunks=1, paragraphs=2)
    result = run_compile(paper, units, compiler=compiler())

    assert result.status == cp.OK and result.spans_path is None
    assert not (paper.build / cp.SPANS_NAME).exists()


# --------------------------------------------------------------------------- #
# 结果契约
# --------------------------------------------------------------------------- #


def test_result_json_matches_report_contract(paper):
    units = build_units(bombs={(1, 2)})
    result = run_compile(paper, units, compiler=compiler())
    data = result.to_json()

    assert data["status"] == "ok_with_fallback" and data["passed"] is True
    assert data["engine"] == "xelatex"
    assert data["fallbacks"] == [
        {
            "chunk_id": "c001",
            "reason": "compile_failed",
            "paragraphs": [2],
            "detail": "! Undefined control sequence.",
            "section": "第1节",
        }
    ]
    assert data["inject"]["branch"] == "inject"
    assert data["passes"] >= 3 and data["probes"] >= 1
    assert result.log_path is not None and result.log_path.parent == paper.logs


def test_src_assets_are_linked_into_the_zh_build(paper):
    (paper.src / "custom.cls").write_text("% cls\n", encoding="utf-8")
    (paper.src / "figs").mkdir()
    (paper.src / "figs" / "a.pdf").write_text("PDF", encoding="utf-8")

    units = build_units(chunks=1, paragraphs=2)
    result = run_compile(paper, units, compiler=compiler())

    assert (result.build_dir / "custom.cls").exists()
    assert (result.build_dir / "figs" / "a.pdf").read_text() == "PDF"
    assert (result.build_dir / "fonts" / "LXGWWenKai-Light.ttf").exists()


def test_missing_fonts_is_a_structured_warning_not_a_crash(paper, tmp_path):
    units = build_units(chunks=1, paragraphs=2)
    result = cp.compile_zh(
        paper,
        units,
        masked_of(units),
        compiler=compiler(),
        fonts=tmp_path / "nowhere",
    )
    assert result.status == cp.OK
    assert any("字体目录不可用" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# 编译器封装本体
# --------------------------------------------------------------------------- #


def test_parse_log_picks_first_error_and_line():
    log = "This is XeTeX\n! Undefined control sequence.\nl.42 \\foo\n! Missing $ inserted.\n"
    summary = parse_log(log)
    assert summary.error_count == 2
    assert summary.first_error == "! Undefined control sequence."
    assert summary.error_line == 42
    assert summary.to_json()["error_count"] == 2


def test_detect_engine():
    assert detect_engine("\\documentclass{article}\n") == "pdflatex"
    assert detect_engine("\\usepackage{xeCJK}\n") == "xelatex"
    assert detect_engine("% \\usepackage{xeCJK}\n") == "pdflatex"


@pytest.mark.skipif(shutil.which("latexmk") is None, reason="本机没有 latexmk")
def test_latexmk_compiler_really_compiles(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    tex = build / "mini.tex"
    tex.write_text("\\documentclass{article}\n\\begin{document}\nhello\n\\end{document}\n", encoding="utf-8")

    result = latexmk_compiler("pdflatex")(tex, build)

    assert result.ok and result.pdf is not None and result.pdf.is_file()
    assert result.engine == "pdflatex" and result.returncode == 0
    assert "-interaction=nonstopmode" in result.command and "-f" in result.command


@pytest.mark.skipif(shutil.which("latexmk") is None, reason="本机没有 latexmk")
def test_latexmk_compiler_reports_errors(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    tex = build / "broken.tex"
    tex.write_text(
        "\\documentclass{article}\n\\begin{document}\n\\undefinedcmd\n\\end{document}\n",
        encoding="utf-8",
    )

    result = latexmk_compiler("pdflatex")(tex, build)

    assert not result.ok
    assert result.error_count >= 1
    assert "Undefined control sequence" in (result.first_error or "")


def test_latexmk_compiler_reports_missing_tool(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    tex = build / "x.tex"
    tex.write_text("\\documentclass{article}\n", encoding="utf-8")

    result = latexmk_compiler("pdflatex", latexmk="latexmk-does-not-exist")(tex, build)

    assert not result.ok and result.missing_tool
    assert "tongtu doctor" in result.message


# --------------------------------------------------------------------------- #
# 字体查找链（wheel 打包缺口，M2 补齐）
# --------------------------------------------------------------------------- #


def test_find_fonts_locates_the_repo_copy():
    """源码树 / editable 安装态：从包文件逐级向上找到仓库 fonts/。"""
    from tongtu.compiler import FONT_FILES, find_fonts

    fonts = find_fonts()

    assert fonts.is_dir() and all((fonts / name).is_file() for name in FONT_FILES)


def test_find_fonts_honours_explicit_and_env(tmp_path, monkeypatch):
    from tongtu.compiler import FONT_FILES, FONTS_ENV, AssetError, find_fonts

    fake = tmp_path / "fonts"
    fake.mkdir()
    (fake / FONT_FILES[0]).write_bytes(b"not really a font")

    assert find_fonts(fake) == fake.absolute()
    monkeypatch.setenv(FONTS_ENV, str(fake))
    assert find_fonts() == fake.absolute()

    monkeypatch.setenv(FONTS_ENV, str(tmp_path / "nope"))
    with pytest.raises(AssetError) as exc:
        find_fonts()
    assert exc.value.kind == "missing_fonts"


def test_packaged_fonts_are_declared_for_the_wheel():
    """pyproject 必须把仓库 fonts/ force-include 进 wheel。

    wheel 安装态里没有仓库根，查找链的最后一环是包内 `tongtu/data/fonts/`——这一条只能
    在打包配置里保证，故直接盯住配置本身（真打包验证在 CI 的构建 job / 手工 uv build）。
    """
    import tomllib
    from pathlib import Path

    from tongtu.compiler import PACKAGED_FONTS

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert include["fonts"] == f"tongtu/{PACKAGED_FONTS}"
    assert (root / "fonts").is_dir()
