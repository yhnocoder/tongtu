from __future__ import annotations

import json
from pathlib import Path

import pytest

from tongtu.artifacts.reconstruct import RegionKind, RegionsFile
from tongtu.manifests import load_manifest
from tongtu.reconstruct import recognition, table
from tongtu.reconstruct.crops import crop_path
from tongtu.reconstruct.rebuild import MAIN_FILENAME

from ..conftest import run_skill_check
from .conftest import TABLE_LATEX

pytestmark = pytest.mark.compile


def table_crop(recognition_tree: tuple[Path, RegionsFile]) -> Path:
    tree, regions = recognition_tree
    key = next(region.id for region in regions.regions if region.kind is RegionKind.TABLE)
    return tree / crop_path(key)


def test_correct_table_passes(recognition_tree: tuple[Path, RegionsFile]) -> None:
    verdict = table.verify(table_crop(recognition_tree), TABLE_LATEX)
    assert verdict.passed, verdict.problem


def test_wrapped_header_cell_passes_by_characters(recognition_tree: tuple[Path, RegionsFile]) -> None:
    wrapped = TABLE_LATEX.replace("Model &", r"\makecell{Mo\\del} &")
    assert table.verify(table_crop(recognition_tree), wrapped).passed


def test_wrong_number_names_the_characters(recognition_tree: tuple[Path, RegionsFile]) -> None:
    verdict = table.verify(table_crop(recognition_tree), TABLE_LATEX.replace("25.8", "25.9"))
    assert not verdict.passed
    assert verdict.problem == (
        "Characters in the original but missing from yours: '8'. Characters in yours but not in the original: '9'."
        " Words differing (original-only: ['25.8']; yours-only: ['25.9'])."
    )


def test_missing_bold_names_the_cell(recognition_tree: tuple[Path, RegionsFile]) -> None:
    verdict = table.verify(table_crop(recognition_tree), TABLE_LATEX.replace(r"\textbf{28.4}", "28.4"))
    assert not verdict.passed
    assert verdict.problem == "Bold differs for: 28.4"


def test_swapped_rows_are_a_layout_difference(recognition_tree: tuple[Path, RegionsFile]) -> None:
    swapped = TABLE_LATEX.replace(
        r"Baseline & 25.8 & 1.0 \\ Transformer & \textbf{28.4} & 0.5 \\",
        r"Transformer & \textbf{28.4} & 0.5 \\ Baseline & 25.8 & 1.0 \\",
    )
    verdict = table.verify(table_crop(recognition_tree), swapped)
    assert not verdict.passed
    assert "'baseline' is above 'transformer' in the original but below it in yours" in verdict.problem


def test_latex_that_does_not_compile_fails(recognition_tree: tuple[Path, RegionsFile]) -> None:
    verdict = table.verify(table_crop(recognition_tree), TABLE_LATEX.replace(r"\end{tabular}", ""))
    assert not verdict.passed
    assert verdict.problem.startswith("Your LaTeX failed: !")


def test_prepare_writes_images_hints_and_crops(recognition_tree: tuple[Path, RegionsFile], tmp_path: Path) -> None:
    tree, regions = recognition_tree
    keys = table.recognizable((tree / MAIN_FILENAME).read_text(encoding="utf-8"), regions)
    assert len(keys) == 1
    table.prepare(tree, tmp_path, keys)
    hints = load_manifest(tmp_path / recognition.HINTS_FILENAME, table.TableHints)
    assert hints is not None and list(hints.root) == keys
    hint = hints.root[keys[0]]
    assert hint.width_bp > 100
    assert hint.spans_y_x[0].endswith("CMR10#000000:Model")
    assert recognition.image_path(tmp_path, keys[0]).is_file()
    assert (tmp_path / crop_path(keys[0])).is_file()


@pytest.mark.parametrize(
    ("latex", "returncode", "line"),
    [(TABLE_LATEX, 0, ""), (TABLE_LATEX.replace("25.8", "25.9"), 1, ": Characters in the original")],
)
def test_check_script_reports_passed_and_failed_tables(
    recognition_tree: tuple[Path, RegionsFile], tmp_path: Path, latex: str, returncode: int, line: str
) -> None:
    tree, regions = recognition_tree
    keys = table.recognizable((tree / MAIN_FILENAME).read_text(encoding="utf-8"), regions)
    table.prepare(tree, tmp_path, keys)
    (tmp_path / recognition.TRANSCRIPTIONS_FILENAME).write_text(json.dumps({keys[0]: latex}), encoding="utf-8")
    result = run_skill_check(tmp_path, "reconstruct_table")
    assert result.returncode == returncode
    lines = result.stdout.splitlines()
    assert lines[0] == f"passed {1 - returncode}/1"
    if line:
        assert lines[1].startswith(keys[0] + line)
