from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tongtu.artifacts.reconstruct import RegionsFile
from tongtu.reconstruct.rebuild import rebuild

RECOGNITION_SOURCE = r"""\documentclass{article}
\usepackage{amsmath,amssymb,bm,booktabs}
\begin{document}
Sequence transduction models translate a sentence of one language into a sentence of another language,
and recent models replace their recurrent layers with attention layers that relate every pair of positions.
\section{Introduction}
The attention function maps a query and a set of key value pairs to an output, where the query, keys, values,
and output are all vectors. We scale the dot products by $1/\sqrt{d_k}$ to counteract the effect of large
magnitudes, and we write $x_i^2 + y_i$ for the combined score of each position in the sequence.
\begin{equation}
\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right) V
\end{equation}
\section{Experimental Results on Translation}
The results of the experiments on the translation tasks are summarized in the following table, which reports
the scores of each model on both benchmarks together with the training cost.
\begin{table}[h]
\centering
\begin{tabular}{lcc}
\toprule
Model & BLEU & Cost \\
\midrule
Baseline & 25.8 & 1.0 \\
Transformer & \textbf{28.4} & 0.5 \\
\bottomrule
\end{tabular}
\caption{Results of the models on two benchmarks.}
\end{table}
\section{Conclusion and Future Work}
The model achieves a new state of the art while requiring a small fraction of the training cost of the best
models from the literature, as shown by the numbers above.
\end{document}
"""

DISPLAY_LATEX = r"\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right) V \quad (1)"

INLINE_LATEX = {"/√dk": r"/\sqrt{d_k}", "xi2": "x_i^2", "yi": "y_i"}

TABLE_LATEX = (
    r"\begin{tabular}{lcc}\toprule Model & BLEU & Cost \\ \midrule Baseline & 25.8 & 1.0 \\"
    r" Transformer & \textbf{28.4} & 0.5 \\ \bottomrule\end{tabular}"
)


@pytest.fixture(scope="session")
def recognition_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("recognition-source")
    (folder / "main.tex").write_text(RECOGNITION_SOURCE, encoding="utf-8")
    subprocess.run(
        ["latexmk", "-pdf", "-interaction=nonstopmode", "main.tex"], cwd=folder, capture_output=True, check=True
    )
    return folder / "main.pdf"


@pytest.fixture(scope="session")
def recognition_tree(recognition_pdf: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, RegionsFile]:
    tree = tmp_path_factory.mktemp("recognition-tree")
    return tree, rebuild(recognition_pdf, tree)
