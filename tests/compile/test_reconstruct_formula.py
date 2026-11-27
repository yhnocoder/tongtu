from __future__ import annotations

import json
from pathlib import Path

import pytest

from tongtu.artifacts.reconstruct import RegionKind, RegionsFile
from tongtu.manifests import load_manifest
from tongtu.reconstruct import formula, recognition
from tongtu.reconstruct.crops import crop_path
from tongtu.reconstruct.rebuild import MAIN_FILENAME

from ..conftest import run_skill_check
from .conftest import DISPLAY_LATEX, INLINE_LATEX

pytestmark = pytest.mark.compile


def region_id(regions: RegionsFile, kind: RegionKind, hint: str = "") -> str:
    return next(region.id for region in regions.regions if region.kind is kind and region.hint == hint)


def test_correct_display_latex_passes(recognition_tree: tuple[Path, RegionsFile]) -> None:
    tree, regions = recognition_tree
    key = region_id(regions, RegionKind.DISPLAY)
    verdict = formula.verify(tree / crop_path(key), DISPLAY_LATEX, RegionKind.DISPLAY)
    assert verdict.passed, verdict.problem


def test_wrong_display_character_names_the_difference(recognition_tree: tuple[Path, RegionsFile]) -> None:
    tree, regions = recognition_tree
    key = region_id(regions, RegionKind.DISPLAY)
    verdict = formula.verify(tree / crop_path(key), DISPLAY_LATEX.replace(") V", ") W"), RegionKind.DISPLAY)
    assert not verdict.passed
    assert verdict.problem.startswith("Glyph sequence differs")
    assert "dkV(1)' vs yours" in verdict.problem and "dkW(1)'" in verdict.problem


def test_wrong_display_script_is_a_placement_difference(recognition_tree: tuple[Path, RegionsFile]) -> None:
    tree, regions = recognition_tree
    key = region_id(regions, RegionKind.DISPLAY)
    verdict = formula.verify(tree / crop_path(key), DISPLAY_LATEX.replace("K^T", "K_T"), RegionKind.DISPLAY)
    assert not verdict.passed
    assert "vertical placement or size" in verdict.problem


def test_inline_formulas_pass_and_a_wrong_script_fails(recognition_tree: tuple[Path, RegionsFile]) -> None:
    tree, regions = recognition_tree
    for hint, latex in INLINE_LATEX.items():
        key = region_id(regions, RegionKind.INLINE, hint)
        verdict = formula.verify(tree / crop_path(key), latex, RegionKind.INLINE)
        assert verdict.passed, (hint, verdict.problem)
    key = region_id(regions, RegionKind.INLINE, "/√dk")
    wrong = formula.verify(tree / crop_path(key), r"/\sqrt{d^k}", RegionKind.INLINE)
    assert not wrong.passed
    assert "vertical placement or size" in wrong.problem


def test_latex_that_does_not_compile_fails(recognition_tree: tuple[Path, RegionsFile]) -> None:
    tree, regions = recognition_tree
    key = region_id(regions, RegionKind.INLINE, "yi")
    verdict = formula.verify(tree / crop_path(key), r"y_{i", RegionKind.INLINE)
    assert not verdict.passed
    assert verdict.problem.startswith("LaTeX failed to compile.")


def test_prepare_writes_images_hints_and_crops(recognition_tree: tuple[Path, RegionsFile], tmp_path: Path) -> None:
    tree, regions = recognition_tree
    keys = formula.recognizable((tree / MAIN_FILENAME).read_text(encoding="utf-8"), regions)
    assert sorted(keys.values()) == [RegionKind.DISPLAY, RegionKind.INLINE, RegionKind.INLINE, RegionKind.INLINE]
    formula.prepare(tree, tmp_path, keys)
    hints = load_manifest(tmp_path / recognition.HINTS_FILENAME, formula.FormulaHints)
    assert hints is not None and set(hints.root) == set(keys)
    display = region_id(regions, RegionKind.DISPLAY)
    assert hints.root[display].kind is RegionKind.DISPLAY
    assert "CMR10#000000:(1)" in hints.root[display].spans
    for key in keys:
        assert recognition.image_path(tmp_path, key).is_file()
        assert (tmp_path / crop_path(key)).is_file()


def test_check_script_reports_passed_and_failed_formulas(
    recognition_tree: tuple[Path, RegionsFile], tmp_path: Path
) -> None:
    tree, regions = recognition_tree
    keys = formula.recognizable((tree / MAIN_FILENAME).read_text(encoding="utf-8"), regions)
    formula.prepare(tree, tmp_path, keys)
    display = region_id(regions, RegionKind.DISPLAY)
    inline = {region_id(regions, RegionKind.INLINE, hint): latex for hint, latex in INLINE_LATEX.items()}
    rec = tmp_path / recognition.TRANSCRIPTIONS_FILENAME
    rec.write_text(json.dumps({display: DISPLAY_LATEX, **inline}), encoding="utf-8")
    passed = run_skill_check(tmp_path, "reconstruct_formula")
    assert (passed.returncode, passed.stdout.strip()) == (0, "passed 4/4")
    rec.write_text(json.dumps({display: DISPLAY_LATEX.replace(") V", ") W"), **inline}), encoding="utf-8")
    failed = run_skill_check(tmp_path, "reconstruct_formula")
    assert failed.returncode == 1
    lines = failed.stdout.splitlines()
    assert lines[0] == "passed 3/4"
    assert lines[1].startswith(f"{display}: Glyph sequence differs")
    only = run_skill_check(tmp_path, "reconstruct_formula", *inline)
    assert (only.returncode, only.stdout.strip()) == (0, "passed 3/3")
