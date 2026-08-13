"""baseline 阶段：原样编译原文、隔离环境问题（架构 §3 baseline 行、关节②）。

**本机没有 TeX 也要能测**：编译被封在 `tongtu.compiler.Compiler` 接口后面，这里全程用
可编程的假编译器（想让它成功就成功、想让它失败就失败），覆盖资产链接、引擎探测、
关节②回调与 `env_failed` 终止语义。真 latexmk 的薄封装另有 `skipif` 保护的测试
（见 `test_compile.py`）。
"""

from pathlib import Path

import pytest

from tongtu.compiler import CompileRunResult
from tongtu.stages import baseline as bl
from tongtu.workdir import Workdir

PREAMBLE = "\\documentclass{article}\n"
BODY = "\\begin{document}\nHello \\emph{world}.\n\\end{document}\n"


@pytest.fixture
def paper(tmp_path) -> Workdir:
    work = Workdir(path=tmp_path / "work" / "2401.01234", arxiv_id="2401.01234").create()
    (work.build / "flat.tex").write_text(PREAMBLE + BODY, encoding="utf-8")
    return work


def fake_compiler(*, fails_while=(), log="! Undefined control sequence.\nl.999 \\foo\n"):
    """假编译器：`fails_while` 是一个可变容器，非空即失败（测试可中途清空它）。

    成功时把 tex 原文写进「PDF」，测试据此断言 PDF 里到底进了什么。
    """
    calls: list[Path] = []

    def run(tex: Path, build_dir: Path) -> CompileRunResult:
        calls.append(tex)
        pdf = build_dir / f"{tex.stem}.pdf"
        if fails_while:
            if pdf.exists():
                pdf.unlink()
            return CompileRunResult(ok=False, log=log, returncode=12, status="failed")
        pdf.write_text(tex.read_text(encoding="utf-8"), encoding="utf-8")
        return CompileRunResult(ok=True, pdf=pdf, returncode=0, status="ok")

    run.calls = calls  # type: ignore[attr-defined]
    return run


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


def test_compiles_original_in_isolated_build_dir(paper):
    result = bl.baseline(paper, compiler=fake_compiler())

    assert result.status == bl.OK and result.ok
    assert result.passes == 1
    assert result.build_dir == paper.build / "baseline"
    assert result.tex == paper.build / "baseline" / "flat.tex"
    assert result.pdf is not None and result.pdf.is_file()
    # 主文件是拷贝进 build 的，src/ 不被弄脏
    assert result.tex.is_file() and not result.tex.is_symlink()
    assert result.to_json()["passed"] is True


def test_links_all_subdirs_and_toplevel_files(paper):
    (paper.src / "custom.cls").write_text("% cls\n", encoding="utf-8")
    (paper.src / "refs.bib").write_text("@article{a}\n", encoding="utf-8")
    # v2 只链 figures/logo/tables/images 四个写死的名字——这个目录会整批丢图
    (paper.src / "plots").mkdir()
    (paper.src / "plots" / "fig1.pdf").write_text("PDF", encoding="utf-8")
    (paper.src / "cover.png").write_text("PNG", encoding="utf-8")

    result = bl.baseline(paper, compiler=fake_compiler())

    build = result.build_dir
    assert result.ok
    for name in ("custom.cls", "refs.bib", "plots", "cover.png"):
        assert name in result.assets.linked or name in result.assets.copied
        assert (build / name).exists()
    assert (build / "plots" / "fig1.pdf").read_text() == "PDF"


def test_skips_assets_pointing_outside_workdir(paper, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.sty").write_text("% 不该被链进来\n", encoding="utf-8")
    (paper.src / "secret.sty").symlink_to(outside / "secret.sty")
    (paper.src / "ok.sty").write_text("% ok\n", encoding="utf-8")

    result = bl.baseline(paper, compiler=fake_compiler())

    assert "secret.sty" in result.assets.skipped
    assert not (result.build_dir / "secret.sty").exists()
    assert "ok.sty" in result.assets.linked or "ok.sty" in result.assets.copied
    assert any("工作目录之外" in w for w in result.warnings)


def test_engine_detection(paper):
    assert bl.baseline(paper, compiler=fake_compiler()).engine == "pdflatex"

    (paper.build / "flat.tex").write_text(
        "\\documentclass{article}\n\\usepackage{fontspec}\n" + BODY, encoding="utf-8"
    )
    assert bl.baseline(paper, compiler=fake_compiler()).engine == "xelatex"


def test_engine_detection_ignores_commented_out_hint(paper):
    """v2 的 `grep 'xeCJK\\|fontspec'` 会被注释骗到——arXiv 源码里这行极常见。"""
    (paper.build / "flat.tex").write_text(
        "\\documentclass{article}\n% \\usepackage{fontspec}\n" + BODY, encoding="utf-8"
    )
    assert bl.baseline(paper, compiler=fake_compiler()).engine == "pdflatex"


def test_pdf_with_errors_still_passes_the_gate(paper):
    """出了 PDF 就放行：真实论文带几个 `!` 错误照样能编，门控只隔离环境问题。"""

    def run(tex: Path, build_dir: Path) -> CompileRunResult:
        pdf = build_dir / f"{tex.stem}.pdf"
        pdf.write_text("pdf", encoding="utf-8")
        return CompileRunResult(
            ok=False, pdf=pdf, returncode=12, log="! Undefined control sequence.\n"
        )

    result = bl.baseline(paper, compiler=run)

    assert result.status == bl.OK
    assert result.error_count == 1
    assert any("不比原文更糟" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# 失败路径与关节②
# --------------------------------------------------------------------------- #


def test_missing_flat_tex(tmp_path):
    work = Workdir(path=tmp_path / "w", arxiv_id="x").create()
    result = bl.baseline(work, compiler=fake_compiler())
    assert result.status == bl.MISSING_SOURCE and not result.ok
    assert "flatten" in result.message


def test_env_failed_without_session(paper):
    compiler = fake_compiler(fails_while=["always"])

    result = bl.baseline(paper, compiler=compiler)

    assert result.status == bl.ENV_FAILED and not result.ok
    assert result.passes == 1 and result.session_used == 0
    assert result.first_error == "! Undefined control sequence."
    assert "环境问题" in result.message
    assert result.log_path is not None and result.log_path.parent == paper.logs


def test_session_fixes_environment_then_recompiles(paper):
    failing = ["missing.sty"]
    compiler = fake_compiler(fails_while=failing)
    seen = []

    def session(request):
        seen.append(request)
        failing.clear()  # 「修好了」：下一次编译就过

    result = bl.baseline(paper, compiler=compiler, session=session)

    assert result.status == bl.OK
    assert result.passes == 2 and result.session_used == 1
    assert len(seen) == 1
    request = seen[0]
    assert request.joint == bl.JOINT == "build_env"
    assert request.tex == paper.build / "baseline" / "flat.tex"
    assert "环境问题" in request.prompt and "! Undefined control sequence." in request.prompt


def test_session_that_fixes_nothing_still_ends_in_env_failed(paper):
    """裁决权在编译，不在会话自述（架构 §9）。"""
    compiler = fake_compiler(fails_while=["always"])
    calls = []

    result = bl.baseline(paper, compiler=compiler, session=lambda request: calls.append(request))

    assert result.status == bl.ENV_FAILED
    assert result.passes == 2 and result.session_used == 1 and len(calls) == 1


def test_missing_latexmk_does_not_burn_a_session(paper):
    """PATH 里没有 latexmk 时拉 agent 没有意义——直接 env_failed。"""

    def run(tex: Path, build_dir: Path) -> CompileRunResult:
        return CompileRunResult(ok=False, status="missing_tool", message="PATH 中没有 latexmk")

    calls = []
    result = bl.baseline(paper, compiler=run, session=lambda request: calls.append(request))

    assert result.status == bl.ENV_FAILED
    assert result.passes == 1 and result.session_used == 0 and calls == []
    assert "latexmk" in result.message
