from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tongtu import masking
from tongtu.artifacts.mask import BlocksFile, MaskManifest, MaskStatus
from tongtu.pipeline import outputs_present
from tongtu.stages import mask
from tongtu.workdir import Workdir

from ..conftest import FIXTURE_PAPERS, tex_sources

PLAIN_FRAGMENTS = [
    "This is a sentence.",
    "第二段文本，含中文标点。",
    "A line with numbers 1234 and symbols &~#.",
    "",
]

COMMENT_FRAGMENTS = [
    "% a trailing comment",
    "%% doubled percent",
    r"text before % comment after",
    r"escaped \% percent stays inline",
]

MATH_FRAGMENTS = [
    "inline $x^2 + y^2 = z^2$ math",
    r"inline \(a \neq b\) math",
    r"\[ E = mc^2 \]",
    "$$ \\sum_{i=1}^{n} i $$",
]

KNOWN_ENVIRONMENT_FRAGMENTS = [
    r"\begin{equation}E = mc^2\label{eq:mass}\end{equation}",
    "\\begin{align}a &= b \\\\ c &= d\\end{align}",
    r"\begin{tabular}{cc}1 & 2 \\ 3 & 4\end{tabular}",
    r"\begin{tikzpicture}\draw (0,0) -- (1,1);\end{tikzpicture}",
]

UNKNOWN_ENVIRONMENT_FRAGMENTS = [
    r"\begin{widetext}wide content here\end{widetext}",
    r"\begin{sidenote}a note aside\end{sidenote}",
]

VERBATIM_FRAGMENTS = [
    "\\begin{verbatim}raw $ % \\ text\\end{verbatim}",
    r"\begin{lstlisting}for i in range(3): print(i)\end{lstlisting}",
    r"inline \verb|x % $ y| verbatim",
]

CAPTION_FRAGMENTS = [
    r"\begin{figure}\includegraphics{a.png}\caption{A figure caption}\label{fig:a}\end{figure}",
    r"\begin{table}\caption[short]{A table caption}\begin{tabular}{c}1\end{tabular}\end{table}",
    r"\begin{figure*}\includegraphics{b.pdf}\caption{Spanning caption}\end{figure*}",
]

TEXT_ENVIRONMENT_FRAGMENTS = [
    r"\begin{itemize}\item one \item two\end{itemize}",
    "\\begin{quote}quoted text\n\nwith a blank line\\end{quote}",
]

COMMAND_FRAGMENTS = [
    r"\section{A Section}",
    r"\textbf{bold} and \emph{emphasis}",
    r"\newtheorem{thm}{Theorem}",
    r"\newenvironment{takeaway}{\begin{quote}}{\end{quote}}",
    r"\ref{eq:mass} and \cite{someone2020}",
]

ALL_FRAGMENTS = (
    PLAIN_FRAGMENTS
    + COMMENT_FRAGMENTS
    + MATH_FRAGMENTS
    + KNOWN_ENVIRONMENT_FRAGMENTS
    + UNKNOWN_ENVIRONMENT_FRAGMENTS
    + VERBATIM_FRAGMENTS
    + CAPTION_FRAGMENTS
    + TEXT_ENVIRONMENT_FRAGMENTS
    + COMMAND_FRAGMENTS
)

SENTINEL_CHARACTERS = (masking.SENTINEL_OPEN, masking.SENTINEL_CLOSE)

AWKWARD_BODIES: tuple[str, ...] = (
    "",
    "   ",
    "\n\n\n",
    "100% pure",
    r"\% escaped percent",
    "$x + y$ paired dollar",
    "a \\ backslash",
    "{unbalanced brace",
    "}closing first{",
    "中文正文与全角标点。",
    "emoji 🙂 与组合字符 e\u0301",
    "tab\tand\rcarriage",
    r"\cmd*[opt]{arg}",
    "%\n%\n% three comment lines",
)

UNCLOSABLE_MARKERS: tuple[str, ...] = ("\\begin", "\\end", "\\verb", "$", "\\[", "\\]", "\\(", "\\)")

SAMPLE_PAPER = r"""\documentclass{article}
\title{A Title}
\begin{abstract}
An abstract.
\end{abstract}
\begin{document}
\maketitle
\section{Intro}
Text with $x$ inline.
\begin{equation}
E = mc^2 \label{eq:mass}
\end{equation}
\begin{figure}
\includegraphics{a.png}
\caption{A figure caption}
\label{fig:a}
\end{figure}
\end{document}
"""


@st.composite
def latex_bodies(draw: st.DrawFn) -> str:
    fragments = draw(st.lists(st.sampled_from(ALL_FRAGMENTS), min_size=0, max_size=12))
    separators = draw(
        st.lists(st.sampled_from(["\n", "\n\n", "\n \n"]), min_size=len(fragments), max_size=len(fragments))
    )
    return "".join(fragment + separator for fragment, separator in zip(fragments, separators, strict=True))


@st.composite
def latex_documents(draw: st.DrawFn) -> str:
    body = draw(latex_bodies())
    documentclass = draw(st.sampled_from(["article", "revtex4-2", "IEEEtran"]))
    title = draw(st.sampled_from([r"\title{A Title}", r"\title{标题}", ""]))
    abstract = draw(st.sampled_from([r"\begin{abstract}An abstract.\end{abstract}", ""]))
    return f"\\documentclass{{{documentclass}}}\n{title}\n{abstract}\n\\begin{{document}}\n{body}\\end{{document}}\n"


def wrap_in_skeleton(body: str) -> str:
    return f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"


def assert_roundtrip(source: str, table) -> masking.MaskOutcome:
    outcome = masking.mask_document(source, table)
    masking.verify_roundtrip(source, outcome)
    return outcome


def assert_placeholders_consistent(outcome: masking.MaskOutcome, path: Path) -> None:
    tokens = masking.TOKEN_RE.findall(outcome.masked)
    block_tokens = [kind for kind, _ in tokens if kind == masking.BLOCK_ID_PREFIX]
    assert len(block_tokens) == len(outcome.blocks), f"{path}：block placeholder 数与记录条数不符"
    assert outcome.masked.count(masking.SENTINEL_OPEN) == len(tokens), f"{path}：掩码文本里有残缺的 placeholder"
    assert outcome.masked.count(masking.SENTINEL_CLOSE) == len(tokens), f"{path}：掩码文本里有残缺的 placeholder"


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(source=latex_documents())
def test_roundtrip_on_generated_documents(source: str, environments_table) -> None:
    assert_roundtrip(source, environments_table)


@pytest.mark.parametrize(
    "body",
    ["$unclosed dollar", r"\[ unclosed display", r"\begin{equation}unclosed"],
    ids=["dollar", "display", "environment"],
)
def test_unclosed_structures_are_rejected(body: str, environments_table) -> None:
    with pytest.raises(masking.MaskError):
        masking.mask_document(wrap_in_skeleton(body), environments_table)


@pytest.mark.parametrize("body", [r"\verb|unclosed", r"\verb*|unclosed", r"text \verb|a| and \verb|b"], ids=range(3))
def test_unclosed_verb_falls_back_to_line_end(body: str, environments_table) -> None:
    assert_roundtrip(wrap_in_skeleton(body), environments_table)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=st.text(max_size=300).filter(lambda text: not any(ch in text for ch in SENTINEL_CHARACTERS)))
def test_roundtrip_on_arbitrary_body(body: str, environments_table) -> None:
    source = wrap_in_skeleton(body)
    try:
        outcome = masking.mask_document(source, environments_table)
    except masking.MaskError:
        assert any(marker in body for marker in UNCLOSABLE_MARKERS), (
            f"正文里没有可能未闭合的结构，掩码却判失败：{body!r}"
        )
        return
    masking.verify_roundtrip(source, outcome)


@pytest.mark.parametrize("body", AWKWARD_BODIES, ids=lambda value: repr(value)[:24])
def test_roundtrip_on_awkward_bodies(body: str, environments_table) -> None:
    assert_roundtrip(wrap_in_skeleton(body), environments_table)


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(text=st.text(max_size=200).filter(lambda value: r"\begin{document}" not in value))
def test_bare_text_is_rejected(text: str, environments_table) -> None:
    stripped = "".join(ch for ch in text if ch not in SENTINEL_CHARACTERS)
    with pytest.raises(masking.MaskError):
        masking.mask_document(stripped, environments_table)


@pytest.mark.parametrize("paper", FIXTURE_PAPERS)
def test_roundtrip_on_fixture_papers(paper: str, environments_table) -> None:
    sources = [path for path in tex_sources(paper) if r"\begin{document}" in path.read_text(encoding="utf-8")]
    assert sources, f"{paper} 没有含 \\begin{{document}} 的 .tex 文件"
    for path in sources:
        source = path.read_text(encoding="utf-8")
        outcome = assert_roundtrip(source, environments_table)
        assert_placeholders_consistent(outcome, path)


@pytest.mark.parametrize("character", SENTINEL_CHARACTERS)
def test_sentinel_in_source_is_rejected(character: str, environments_table) -> None:
    with pytest.raises(masking.MaskError):
        masking.mask_document(f"text with {character} inside", environments_table)


def make_workdir(tmp_path: Path, source: str | bytes | None = SAMPLE_PAPER) -> Workdir:
    workdir = Workdir(tmp_path / "paper")
    workdir.create()
    path = workdir.build / mask.PRECOMPILE_FILENAME
    if isinstance(source, bytes):
        path.write_bytes(source)
    elif source is not None:
        path.write_text(source, encoding="utf-8")
    return workdir


def read_manifest(workdir: Workdir) -> MaskManifest:
    return MaskManifest.model_validate_json(workdir.manifest_path(mask.STAGE_NAME).read_text(encoding="utf-8"))


def test_run_ok_writes_outputs_and_manifest(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    manifest = mask.run(workdir)
    assert manifest.status is MaskStatus.OK
    assert manifest == read_manifest(workdir)
    assert outputs_present(workdir, "mask")
    masked = (workdir.build / mask.MASKED_FILENAME).read_text(encoding="utf-8")
    blocks = BlocksFile.model_validate_json((workdir.build / mask.BLOCKS_FILENAME).read_text(encoding="utf-8"))
    assert manifest.blocks_total == len(blocks.blocks) == masked.count(f"{masking.SENTINEL_OPEN}BLK-")
    assert manifest.captions_total == len(blocks.captions) == 2
    assert {caption.kind for caption in blocks.captions} == {masking.CaptionKind.ABSTRACT, masking.CaptionKind.CAPTION}
    assert manifest.precompile_chars == len(SAMPLE_PAPER)
    assert manifest.masked_chars == len(masked)
    assert manifest.masked_chars_ratio == round(len(masked) / len(SAMPLE_PAPER), 4)
    assert manifest.environments["equation"].classification is masking.EnvironmentClass.NON_TRANSLATABLE
    assert manifest.environments["equation"].category == "math"
    assert manifest.environments["equation"].blocks == 1
    assert manifest.environments["abstract"].classification is masking.EnvironmentClass.TEXT
    assert [block.labels for block in blocks.blocks if block.environment == "equation"] == [["eq:mass"]]
    restored = masking.unmask(
        masked,
        [masking.Block(**block.model_dump()) for block in blocks.blocks],
        [masking.Caption(**caption.model_dump()) for caption in blocks.captions],
    )
    assert restored.text == SAMPLE_PAPER


def test_manifest_fields_match_card(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    mask.run(workdir)
    keys = set(json.loads(workdir.manifest_path(mask.STAGE_NAME).read_text(encoding="utf-8")))
    assert keys == {
        "status",
        "environments",
        "blocks_total",
        "captions_total",
        "precompile_chars",
        "masked_chars",
        "masked_chars_ratio",
        "warnings",
        "message",
    }


@pytest.mark.parametrize(
    "source",
    [
        None,
        b"\\documentclass{article}\n\\begin{document}\n\xff\xfe\n\\end{document}\n",
        wrap_in_skeleton(f"text with {masking.SENTINEL_OPEN} inside"),
        wrap_in_skeleton(r"\begin{equation}unclosed"),
        "no document environment at all\n",
    ],
    ids=["precompile_missing", "invalid_utf8", "sentinel", "unclosed_environment", "no_begin_document"],
)
def test_run_failures_write_mask_failed_without_outputs(tmp_path: Path, source) -> None:
    workdir = make_workdir(tmp_path, source)
    (workdir.build / mask.MASKED_FILENAME).write_text("stale", encoding="utf-8")
    (workdir.build / mask.BLOCKS_FILENAME).write_text("{}", encoding="utf-8")
    manifest = mask.run(workdir)
    assert manifest.status is MaskStatus.MASK_FAILED
    assert manifest.message
    assert manifest == read_manifest(workdir)
    assert not (workdir.build / mask.MASKED_FILENAME).exists()
    assert not (workdir.build / mask.BLOCKS_FILENAME).exists()
    assert not outputs_present(workdir, "mask")


def test_run_broken_environments_table_is_mask_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    table_path = tmp_path / "environments.json"
    table_path.write_text('{"equation": {"class": "nonsense"}}', encoding="utf-8")
    monkeypatch.setattr(masking, "ENVIRONMENTS_TABLE_PATH", table_path)
    workdir = make_workdir(tmp_path)
    manifest = mask.run(workdir)
    assert manifest.status is MaskStatus.MASK_FAILED
    assert "environment table" in manifest.message
    assert not outputs_present(workdir, "mask")
