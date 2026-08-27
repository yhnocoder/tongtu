from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import tongtu.model
from tongtu import processes
from tongtu.artifacts.precompile import PrecompileStatus
from tongtu.model.work import StopReason, WorkOutcome
from tongtu.pipeline import outputs_present
from tongtu.processes import ProcessOutcome
from tongtu.stages import precompile
from tongtu.workdir import Workdir

PLAIN_PAPER = """\\documentclass[11pt]{article}
\\usepackage{graphicx}
\\begin{document}
Hello world.
\\end{document}
"""

LOG_OK = """Output written on flat.xdv (7 pages, 12345 bytes).
Overfull \\hbox (10.0pt too wide) in paragraph
LaTeX Warning: Reference `fig:x' on page 1 undefined
LaTeX Warning: Citation `adam' on page 2 undefined
Missing character: There is no X in font
"""

LOG_ERROR = """! Undefined control sequence.
l.42 \\pdfoutput
Output written on flat.xdv (0 pages, 8 bytes).
"""


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def write_models_toml(tmp_path: Path, text: str) -> None:
    path = tmp_path / "config" / "tongtu" / "models.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_workdir(tmp_path: Path, files: dict[str, str]) -> Workdir:
    workdir = Workdir(tmp_path / "paper")
    workdir.create()
    for name, content in files.items():
        path = workdir.src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return workdir


def wire_expand(monkeypatch: pytest.MonkeyPatch, returncode: int = 0, stderr: bytes = b"") -> None:
    def run(command: list[str], cwd: Path, capture_output: bool, check: bool) -> subprocess.CompletedProcess[bytes]:
        stdout = (Path(cwd) / command[-1]).read_bytes() if returncode == 0 else b""
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(precompile.subprocess, "run", run)


def wire_latexmk(monkeypatch: pytest.MonkeyPatch, specs: list[dict]) -> dict[str, int]:
    calls = {"compile": 0, "clean": 0}

    def run(command: list[str], cwd: Path, timeout: float, **kwargs: object) -> ProcessOutcome:
        if "-C" in command:
            calls["clean"] += 1
            (cwd / precompile.PDF_FILENAME).unlink(missing_ok=True)
            (cwd / precompile.LOG_FILENAME).unlink(missing_ok=True)
            return ProcessOutcome(returncode=0, stderr=b"", timed_out=False, duration_seconds=0.1)
        spec = specs[min(calls["compile"], len(specs) - 1)]
        calls["compile"] += 1
        if spec.get("timeout"):
            return ProcessOutcome(returncode=-9, stderr=b"", timed_out=True, duration_seconds=600.0)
        if spec.get("pdf", True):
            (cwd / precompile.PDF_FILENAME).write_bytes(b"%PDF-1.5 fake body")
        (cwd / precompile.LOG_FILENAME).write_text(spec.get("log", LOG_OK), encoding="utf-8")
        return ProcessOutcome(returncode=spec.get("returncode", 0), stderr=b"", timed_out=False, duration_seconds=2.5)

    monkeypatch.setattr(processes, "run_in_process_group", run)
    return calls


def wire_work(
    monkeypatch: pytest.MonkeyPatch,
    stop_reason: StopReason = StopReason.FINISHED,
    detail: str = "",
    edit=None,
) -> list[dict]:
    calls: list[dict] = []

    def fake_work(
        role: str,
        workdir: Path,
        *,
        trace_path: Path,
        model: str | None = None,
        effort: str | None = None,
        report=None,
    ):
        calls.append({"role": role, "workdir": workdir, "trace_path": trace_path, "model": model, "effort": effort})
        if report is not None:
            report("Bash: ls")
        if edit is not None:
            edit(workdir)
        return WorkOutcome(stop_reason=stop_reason, detail=detail)

    monkeypatch.setattr(tongtu.model, "work", fake_work)
    return calls


def run_ok_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> tuple[Workdir, dict]:
    workdir = make_workdir(tmp_path, files)
    wire_expand(monkeypatch)
    calls = wire_latexmk(monkeypatch, [{}])
    return workdir, calls


def test_main_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"notes.tex": "no class here\n", "old.tex": "% \\documentclass{article}\n"})
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.MAIN_NOT_FOUND
    assert manifest.main_file == ""
    assert manifest.report is None
    assert "\\documentclass" in manifest.message
    assert workdir.manifest_path("precompile").is_file()
    assert not outputs_present(workdir, "precompile")


def test_main_ambiguous_lists_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paper = PLAIN_PAPER
    workdir = make_workdir(tmp_path, {"a.tex": paper, "b.tex": paper})
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.MAIN_AMBIGUOUS
    assert "a.tex" in manifest.message
    assert "b.tex" in manifest.message


def test_main_selection_prefers_begin_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, _ = run_ok_setup(
        tmp_path, monkeypatch, {"style.tex": "\\documentclass{article}\n", "paper.tex": PLAIN_PAPER}
    )
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.OK
    assert manifest.main_file == "paper.tex"


def test_main_selection_prefers_main_basename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER, "supplement.tex": PLAIN_PAPER})
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.OK
    assert manifest.main_file == "main.tex"


def test_expand_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"main.tex": PLAIN_PAPER})
    wire_expand(monkeypatch, returncode=1, stderr=b"missing input file body.tex\n")
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.EXPAND_FAILED
    assert manifest.main_file == "main.tex"
    assert "exited with code 1" in manifest.message
    assert any("body.tex" in line for line in manifest.warnings)


def test_expand_result_must_be_a_complete_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"main.tex": "\\documentclass{article}\n\\begin{document}\ntruncated\n"})
    wire_expand(monkeypatch)
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.EXPAND_FAILED
    assert "\\end{document}" in manifest.message


def test_ok_without_fix_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, calls = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.OK
    assert manifest.main_file == "main.tex"
    assert manifest.fix_session is None
    assert manifest.message == ""
    assert manifest.report is not None
    assert manifest.report.pages == 7
    assert manifest.report.pdf_bytes > 0
    assert manifest.report.overfull_hboxes == 1
    assert manifest.report.undefined_references == 1
    assert manifest.report.undefined_citations == 1
    assert manifest.report.missing_characters == 1
    assert manifest.report.duration_seconds > 0
    assert calls == {"compile": 1, "clean": 0}
    assert outputs_present(workdir, "precompile")
    tree = workdir.build / "sandbox" / "precompile"
    assert (tree / "flat.tex").is_file()
    assert (tree / "fonts" / "LXGWWenKai-Light.ttf").is_file()
    written = json.loads(workdir.manifest_path("precompile").read_text(encoding="utf-8"))
    assert written["status"] == "ok"
    assert written["report"]["pages"] == 7


def test_injects_xecjk_after_documentclass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert output.index("\\documentclass") < output.index("\\usepackage{xeCJK}") < output.index("\\begin{document}")
    assert "% ---- injected by tongtu (precompile) ----" in output
    assert "LXGWWenKai-Light.ttf" in output


def test_injects_default_fonts_without_models_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert "\\setCJKmainfont[Path={fonts/},BoldFont=LXGWWenKai-Medium.ttf]{LXGWWenKai-Light.ttf}" in output
    assert "\\IfFontExistsTF{Hiragino Sans GB}" in output
    assert "\\setCJKmonofont[Path={fonts/}]{LXGWWenKai-Light.ttf}" in output


def test_fonts_config_switches_to_system_font(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_models_toml(tmp_path, '[fonts]\nmain = "Noto Serif CJK SC"\n')
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    manifest = precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert manifest.status is PrecompileStatus.OK
    assert "\\setCJKmainfont{Noto Serif CJK SC}" in output
    assert "\\setCJKmonofont{Noto Serif CJK SC}" in output
    assert not any("bold" in line for line in manifest.warnings)


def test_fonts_config_system_bold_pairs_with_system_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_models_toml(tmp_path, '[fonts]\nmain = "Source Han Serif SC"\nbold = "Source Han Serif SC Bold"\n')
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert "\\setCJKmainfont[BoldFont=Source Han Serif SC Bold]{Source Han Serif SC}" in output


def test_fonts_config_switches_to_repo_font_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_models_toml(tmp_path, '[fonts]\nmain = "LXGWWenKai-Medium.ttf"\n')
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert "\\setCJKmainfont[Path={fonts/}]{LXGWWenKai-Medium.ttf}" in output
    assert "\\setCJKmonofont[Path={fonts/}]{LXGWWenKai-Medium.ttf}" in output


def test_fonts_config_external_file_is_copied_into_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    font_path = tmp_path / "custom" / "MyFont.otf"
    font_path.parent.mkdir(parents=True)
    font_path.write_bytes(b"font-bytes")
    write_models_toml(tmp_path, f'[fonts]\nmain = "{font_path}"\n')
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    manifest = precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert manifest.status is PrecompileStatus.OK
    assert "\\setCJKmainfont[Path={fonts/}]{MyFont.otf}" in output
    copied = workdir.build / "sandbox" / "precompile" / "fonts" / "MyFont.otf"
    assert copied.read_bytes() == b"font-bytes"


def test_fonts_config_missing_file_falls_back_to_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_models_toml(tmp_path, '[fonts]\nmain = "Ghost.ttf"\n')
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    manifest = precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert manifest.status is PrecompileStatus.OK
    assert "\\setCJKmainfont[Path={fonts/},BoldFont=LXGWWenKai-Medium.ttf]{LXGWWenKai-Light.ttf}" in output
    assert any("Ghost.ttf" in line for line in manifest.warnings)


def test_fonts_config_bold_kind_mismatch_is_ignored_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_models_toml(tmp_path, '[fonts]\nmain = "Noto Serif CJK SC"\nbold = "LXGWWenKai-Light.ttf"\n')
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    manifest = precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert "\\setCJKmainfont{Noto Serif CJK SC}" in output
    assert any("bold is ignored" in line for line in manifest.warnings)


def test_fonts_config_list_builds_fallback_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_models_toml(tmp_path, '[fonts]\nmain = ["Source Han Serif SC", "Noto Serif CJK SC"]\n')
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    manifest = precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert manifest.status is PrecompileStatus.OK
    expected = (
        "\\IfFontExistsTF{Source Han Serif SC}\n"
        "  {\\setCJKmainfont{Source Han Serif SC}}\n"
        "  {\\IfFontExistsTF{Noto Serif CJK SC}\n"
        "    {\\setCJKmainfont{Noto Serif CJK SC}}\n"
        "    {\\setCJKmainfont[Path={fonts/},BoldFont=LXGWWenKai-Medium.ttf]{LXGWWenKai-Light.ttf}}}\n"
    )
    assert expected in output
    assert (
        "\\IfFontExistsTF{Source Han Serif SC}\n"
        "  {\\setCJKmonofont{Source Han Serif SC}}\n"
        "  {\\IfFontExistsTF{Noto Serif CJK SC}\n"
        "    {\\setCJKmonofont{Noto Serif CJK SC}}\n"
        "    {\\setCJKmonofont[Path={fonts/}]{LXGWWenKai-Light.ttf}}}\n"
    ) in output


def test_fonts_config_list_ends_at_file_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_models_toml(tmp_path, '[fonts]\nmain = ["Noto Serif CJK SC", "LXGWWenKai-Medium.ttf", "Unreached"]\n')
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    manifest = precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert (
        "\\IfFontExistsTF{Noto Serif CJK SC}\n"
        "  {\\setCJKmainfont{Noto Serif CJK SC}}\n"
        "  {\\setCJKmainfont[Path={fonts/}]{LXGWWenKai-Medium.ttf}}\n"
    ) in output
    assert "Unreached" not in output
    assert any("never used" in line for line in manifest.warnings)


def test_fonts_config_list_skips_missing_file_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_models_toml(tmp_path, '[fonts]\nmain = ["Ghost.ttf", "Noto Serif CJK SC"]\n')
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    manifest = precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert "Ghost.ttf" not in output
    assert "\\setCJKmainfont{Noto Serif CJK SC}" in output
    assert any("Ghost.ttf" in line and "skipped" in line for line in manifest.warnings)


def test_fonts_config_overrides_sans_and_mono(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_models_toml(tmp_path, '[fonts]\nsans = "Noto Sans CJK SC"\nmono = "LXGWWenKai-Medium.ttf"\n')
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert "\\setCJKsansfont{Noto Sans CJK SC}" in output
    assert "\\IfFontExistsTF" not in output
    assert "\\setCJKmonofont[Path={fonts/}]{LXGWWenKai-Medium.ttf}" in output
    assert "\\setCJKmainfont[Path={fonts/},BoldFont=LXGWWenKai-Medium.ttf]{LXGWWenKai-Light.ttf}" in output


def test_inject_passes_through_existing_xecjk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paper = PLAIN_PAPER.replace("\\usepackage{graphicx}", "\\usepackage{graphicx}\n\\usepackage{xeCJK}")
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": paper})
    precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert "injected by tongtu" not in output


def test_inject_passes_through_ctex_class(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paper = PLAIN_PAPER.replace("\\documentclass[11pt]{article}", "\\documentclass{ctexart}")
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": paper})
    precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert "injected by tongtu" not in output


def test_inject_replaces_cjkutf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paper = """\\documentclass{article}
\\usepackage{CJKutf8}
\\usepackage{amsmath,CJKspace}
\\begin{document}
\\begin{CJK*}{UTF8}{gbsn}
\\CJKfamily{gbsn}
正文保留。
\\end{CJK*}
\\end{document}
"""
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": paper})
    manifest = precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert manifest.status is PrecompileStatus.OK
    assert "CJKutf8" not in output
    assert "\\begin{CJK*}" not in output
    assert "\\end{CJK*}" not in output
    assert "\\CJKfamily" not in output
    assert "\\usepackage{amsmath}" in output
    assert "\\usepackage{xeCJK}" in output
    assert "正文保留。" in output
    assert any("CJKutf8" in line for line in manifest.warnings)


def test_bbl_is_inlined_when_exactly_one_bibliography(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paper = PLAIN_PAPER.replace("Hello world.", "Hello world.\n\\bibliography{refs}")
    files = {"main.tex": paper, "main.bbl": "\\begin{thebibliography}{1}\n\\end{thebibliography}\n"}
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, files)
    manifest = precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert "\\begin{thebibliography}" in output
    assert "\\bibliography{refs}" not in output
    assert any("main.bbl" in line and "inlined" in line for line in manifest.warnings)


def test_bbl_not_inlined_when_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paper = PLAIN_PAPER.replace("Hello world.", "\\bibliography{refs}\n\\bibliography{refs}")
    files = {"main.tex": paper, "main.bbl": "\\begin{thebibliography}{1}\n\\end{thebibliography}\n"}
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, files)
    manifest = precompile.run(workdir)
    output = (workdir.build / "precompile.tex").read_text(encoding="utf-8")
    assert "thebibliography" not in output
    assert any("not inlined" in line for line in manifest.warnings)


def test_first_failure_starts_a_fix_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"main.tex": PLAIN_PAPER})
    wire_expand(monkeypatch)
    calls = wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])
    work_calls = wire_work(monkeypatch)
    manifest = precompile.run(workdir, model_override="claude_code/claude-sonnet-5", effort="high")
    assert manifest.status is PrecompileStatus.OK
    assert manifest.fix_session is not None
    assert manifest.fix_session.stop_reason == "finished"
    assert manifest.fix_session.duration_seconds >= 0
    assert calls == {"compile": 2, "clean": 1}
    assert work_calls[0]["role"] == "precompile_fix"
    assert work_calls[0]["workdir"] == workdir.build / "sandbox" / "precompile"
    assert work_calls[0]["trace_path"] == workdir.logs / "precompile-fix.jsonl"
    assert work_calls[0]["model"] == "claude_code/claude-sonnet-5"
    assert work_calls[0]["effort"] == "high"


def test_fix_session_error_still_goes_to_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"main.tex": PLAIN_PAPER})
    wire_expand(monkeypatch)
    calls = wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])
    wire_work(monkeypatch, stop_reason=StopReason.ERROR, detail="运行时 claude_code 不在 PATH 里")
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.OK
    assert manifest.fix_session is not None
    assert manifest.fix_session.stop_reason == "error"
    assert any("the fix session ended with error" in line and "claude_code" in line for line in manifest.warnings)
    assert calls == {"compile": 2, "clean": 1}
    assert outputs_present(workdir, "precompile")


def test_fix_session_error_then_verification_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"main.tex": PLAIN_PAPER})
    wire_expand(monkeypatch)
    calls = wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}])
    wire_work(monkeypatch, stop_reason=StopReason.ERROR, detail="运行时 claude_code 不在 PATH 里")
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.COMPILE_FAILED
    assert manifest.fix_session is not None
    assert manifest.fix_session.stop_reason == "error"
    assert any("the fix session ended with error" in line for line in manifest.warnings)
    assert manifest.message.startswith("after the fix session the verify compile still fails the exit checks:")
    assert calls == {"compile": 2, "clean": 1}
    assert not outputs_present(workdir, "precompile")


def test_fix_session_timeout_still_goes_to_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"main.tex": PLAIN_PAPER})
    wire_expand(monkeypatch)
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])
    wire_work(monkeypatch, stop_reason=StopReason.TIMEOUT)
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.OK
    assert manifest.fix_session is not None
    assert manifest.fix_session.stop_reason == "timeout"
    assert any("timeout" in line for line in manifest.warnings)


def test_final_verification_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"main.tex": PLAIN_PAPER})
    wire_expand(monkeypatch)
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}])
    wire_work(monkeypatch)
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.COMPILE_FAILED
    assert manifest.report is None
    assert "verify" in manifest.message
    assert "! Undefined control sequence." in manifest.message
    assert not (workdir.build / "precompile.tex").exists()


def test_first_compile_timeout_skips_the_fix_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"main.tex": PLAIN_PAPER})
    wire_expand(monkeypatch)
    wire_latexmk(monkeypatch, [{"timeout": True}])
    work_calls = wire_work(monkeypatch)
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.COMPILE_FAILED
    assert "timeout" in manifest.message
    assert work_calls == []
    assert manifest.fix_session is None


def test_zero_pages_fails_the_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"main.tex": PLAIN_PAPER})
    wire_expand(monkeypatch)
    wire_latexmk(monkeypatch, [{"log": "Output written on flat.xdv (0 pages, 8 bytes).\n"}])
    wire_work(monkeypatch)
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.COMPILE_FAILED
    assert "page count" in manifest.message


def test_session_changes_outside_flat_tex_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {"main.tex": PLAIN_PAPER, "macros.sty": "\\newcommand{\\x}{1}\n"}
    workdir = make_workdir(tmp_path, files)
    wire_expand(monkeypatch)
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])

    def edit(tree: Path) -> None:
        (tree / "macros.sty").write_text("\\newcommand{\\x}{2}\n", encoding="utf-8")

    wire_work(monkeypatch, edit=edit)
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.OK
    assert any("macros.sty" in line for line in manifest.warnings)


def test_rerun_clears_previous_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"broken.tex": "no documentclass\n"})
    (workdir.build / "sandbox" / "precompile").mkdir(parents=True)
    (workdir.build / "sandbox" / "precompile" / "flat.pdf").write_bytes(b"stale")
    (workdir.build / "precompile.tex").write_text("stale", encoding="utf-8")
    (workdir.logs / "precompile-fix.jsonl").write_text("stale", encoding="utf-8")
    manifest = precompile.run(workdir)
    assert manifest.status is PrecompileStatus.MAIN_NOT_FOUND
    assert not (workdir.build / "sandbox" / "precompile").exists()
    assert not (workdir.build / "precompile.tex").exists()
    assert not (workdir.logs / "precompile-fix.jsonl").exists()


def test_report_traces_the_compile_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, _ = run_ok_setup(tmp_path, monkeypatch, {"main.tex": PLAIN_PAPER})
    actions: list[str] = []
    manifest = precompile.run(workdir, report=actions.append)
    assert manifest.status is PrecompileStatus.OK
    assert actions == ["compiling flat.tex"]


def test_report_traces_the_fix_session_and_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = make_workdir(tmp_path, {"main.tex": PLAIN_PAPER})
    wire_expand(monkeypatch)
    wire_latexmk(monkeypatch, [{"returncode": 1, "log": LOG_ERROR}, {}])
    wire_work(monkeypatch)
    actions: list[str] = []
    manifest = precompile.run(workdir, report=actions.append)
    assert manifest.status is PrecompileStatus.OK
    assert actions == ["compiling flat.tex", "fix session running", "fix session Bash: ls", "verifying compile"]
