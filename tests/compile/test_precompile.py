from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import tongtu.model
from tongtu import processes
from tongtu.artifacts.precompile import PrecompileStatus
from tongtu.model.config import FontsConfig
from tongtu.model.work import StopReason, WorkOutcome
from tongtu.pipeline import outputs_present
from tongtu.stages import precompile
from tongtu.workdir import Workdir

pytestmark = pytest.mark.compile


@pytest.mark.parametrize("repair_all", [True, False])
def test_arxiv_1905_12322v3_double_missing_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repair_all: bool
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    workdir = Workdir(tmp_path / "1905.12322v3")
    workdir.create()
    source = (
        "\\documentclass{article}\n"
        "\\include{macros_new}\n"
        "\\include{new_cmds}\n"
        "\\begin{document}\n"
        "Preserved body with citation \\cite{reference}.\n"
        "\\bibliography{references}\n"
        "\\end{document}\n"
    )
    (workdir.src / "main.tex").write_text(source)
    bbl = "\\begin{thebibliography}{1}\n\\bibitem{reference} Preserved reference.\n\\end{thebibliography}\n"
    (workdir.src / "main.bbl").write_text(bbl)
    initial = processes.run_in_process_group(
        [*precompile.LATEXPAND_COMMAND, "main.tex"], workdir.src, precompile.LATEXPAND_TIMEOUT_SECONDS
    )
    assert initial.returncode == 2 and not initial.timed_out
    assert "ERROR: Could not find file [macros_new.tex]" in initial.stderr_text
    calls: list[Path] = []

    def work(
        role: str,
        tree: Path,
        *,
        trace_path: Path,
        model: str | None = None,
        effort: str | None = None,
        report=None,
    ) -> WorkOutcome:
        assert role == "precompile_fix"
        calls.append(tree)
        text = source.replace("\\include{macros_new}", "%\\include{macros_new}")
        if repair_all:
            text = text.replace("\\include{new_cmds}", "%\\include{new_cmds}")
        (tree / "main.tex").write_text(text)
        trace_path.write_text("single simulated repair\n")
        return WorkOutcome(stop_reason=StopReason.FINISHED, model="test/offline")

    monkeypatch.setattr(tongtu.model, "work", work)
    manifest = precompile.run(workdir)
    assert len(calls) == 1
    assert manifest.fix_session is not None and manifest.fix_session.model == "test/offline"
    assert (workdir.src / "main.tex").read_text() == source
    assert (workdir.src / "main.bbl").read_text() == bbl
    written = json.loads(workdir.manifest_path("precompile").read_text())
    assert written["fix_session"]["model"] == "test/offline"
    if repair_all:
        assert manifest.status is PrecompileStatus.OK
        assert manifest.report is not None and manifest.report.pages > 0
        assert manifest.report.undefined_citations == 0
        assert manifest.report.undefined_references == 0
        assert manifest.report.missing_characters == 0
        assert workdir.precompile_pdf.read_bytes().startswith(b"%PDF")
        output = workdir.precompile_tex.read_text()
        assert "Preserved body" in output and "Preserved reference" in output
        assert "\\usepackage{xeCJK}" in output
        assert outputs_present(workdir, "precompile")
    else:
        assert manifest.status is PrecompileStatus.EXPAND_FAILED
        assert "new_cmds.tex" in manifest.message
        assert manifest.report is None
        assert not workdir.precompile_tex.exists() and not workdir.precompile_pdf.exists()
        assert not outputs_present(workdir, "precompile")


PDFLATEX_LEFTOVERS = {
    "pdfoutput": ("\\pdfoutput=1\n\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n", ()),
    "pdfinfo": (
        "\\documentclass{article}\n\\pdfinfo{\n/Title (A title)\n/Author (An author)\n}\n"
        "\\begin{document}\nHello\n\\end{document}\n",
        (),
    ),
    "pdftex_graphicx": (
        "\\documentclass{article}\n\\usepackage[pdftex]{graphicx}\n\\begin{document}\nHello\n\\end{document}\n",
        (),
    ),
    "microtype_tfm_font": (
        "\\documentclass{article}\n\\renewcommand{\\rmdefault}{ptm}\n\\usepackage{microtype}\n"
        "\\begin{document}\nHello, world: a paragraph long enough to be protruded.\n\\end{document}\n",
        ("microtype.sty",),
    ),
    "fdsymbol_no_math": (
        "\\documentclass{article}\n\\usepackage{fdsymbol}\n\\begin{document}\n$\\mdblksquare$ costs \\$1\n\\end{document}\n",
        ("fdsymbol.sty",),
    ),
    "ucs_bxcjkjatype": (
        "\\documentclass{article}\n\\usepackage[postscript, cjkjis]{ucs}\n\\usepackage[whole]{bxcjkjatype}\n"
        "\\begin{document}\nHello\n\\end{document}\n",
        (),
    ),
    "fontawesome": (
        "\\documentclass{article}\n\\usepackage{fontawesome}\n\\begin{document}\n\\faGithub\n\\end{document}\n",
        ("fontawesome.sty", "FontAwesome.otf"),
    ),
    "legacy_cjk": (
        "\\documentclass{article}\n\\usepackage{CJKutf8}\n\\begin{document}\n"
        "\\begin{CJK*}{UTF8}{gbsn}\n中文正文\n\\end{CJK*}\n\\end{document}\n",
        (),
    ),
}


@pytest.mark.parametrize("name", sorted(PDFLATEX_LEFTOVERS))
def test_pdflatex_leftovers_compile_after_adapt(tmp_path: Path, name: str) -> None:
    source, required = PDFLATEX_LEFTOVERS[name]
    if required and subprocess.run(["kpsewhich", *required], capture_output=True, check=False).returncode != 0:
        pytest.skip(f"{' '.join(required)} not installed")
    warnings: list[str] = []
    injected, font_files = precompile._inject_cjk(source.encode("utf-8"), warnings, FontsConfig())
    precompile._assemble_tree(tmp_path, injected, warnings, font_files)
    completed = subprocess.run(
        ["xelatex", "-interaction=nonstopmode", "-halt-on-error", precompile.FLAT_FILENAME],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-2000:]
    assert (tmp_path / "flat.pdf").stat().st_size > 0
