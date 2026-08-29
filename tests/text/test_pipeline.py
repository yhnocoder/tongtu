from __future__ import annotations

from pathlib import Path

from tongtu.pipeline import STAGES, clean_from, downstream, first_pending, outputs_present
from tongtu.workdir import Workdir

OUTPUT_FILES: dict[str, tuple[str, ...]] = {
    "fetch": ("src/main.tex",),
    "precompile": ("build/precompile.tex", "build/precompile.pdf"),
    "mask": ("build/masked.tex", "build/blocks.json"),
    "survey": ("build/brief.json", "build/chunks/c000.tex"),
    "translate": ("build/translated/c000.tex",),
    "review": ("build/reviewed/c000.tex",),
    "compile": ("build/zh.tex", "out/zh.pdf"),
}

SIDE_FILES: dict[str, tuple[str, ...]] = {
    "fetch": ("build/e-print.bin",),
    "precompile": ("build/sandbox/tex/flat.tex", "logs/precompile-fix.jsonl"),
    "mask": (),
    "survey": ("logs/survey-terms.json",),
    "translate": ("logs/translate-c000-1.json", "logs/translate-c001-1.json"),
    "review": ("build/sandbox/review/chunks/c000.tex", "logs/review.jsonl"),
    "compile": ("logs/compile-fix.jsonl",),
}


def write_outputs(root: Path, *stages: str) -> Workdir:
    workdir = Workdir(root / "paper")
    for name in stages:
        for relative in (*OUTPUT_FILES[name], *SIDE_FILES[name]):
            path = workdir.path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding="utf-8")
        manifest_path = workdir.manifest_path(name)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text('{"status": "ok", "warnings": [], "message": ""}\n', encoding="utf-8")
    return workdir


def test_stage_order_matches_proposal() -> None:
    assert STAGES == ("fetch", "precompile", "mask", "survey", "translate", "review", "compile")


def test_downstream_starts_at_the_given_stage() -> None:
    assert downstream("review") == ("review", "compile")
    assert downstream("fetch") == STAGES


def test_first_pending_without_any_outputs(tmp_path: Path) -> None:
    assert first_pending(Workdir(tmp_path / "paper")) == "fetch"


def test_first_pending_resumes_after_existing_outputs(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    assert first_pending(workdir) == "mask"


def test_first_pending_with_all_outputs(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    assert first_pending(workdir) is None


def test_empty_directory_counts_as_absent(tmp_path: Path) -> None:
    workdir = Workdir(tmp_path / "paper")
    workdir.src.mkdir(parents=True)
    assert not outputs_present(workdir, "fetch")
    assert first_pending(workdir) == "fetch"


def test_clean_from_removes_stage_and_downstream(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    clean_from(workdir, "survey")
    for name in ("fetch", "precompile", "mask"):
        assert outputs_present(workdir, name)
        assert workdir.manifest_path(name).is_file()
    for relative in (*SIDE_FILES["fetch"], *SIDE_FILES["precompile"]):
        assert (workdir.path / relative).is_file()
    for name in ("survey", "translate", "review", "compile"):
        assert not outputs_present(workdir, name)
        assert not workdir.manifest_path(name).exists()
        for relative in (*OUTPUT_FILES[name], *SIDE_FILES[name]):
            assert not (workdir.path / relative).exists()
    assert first_pending(workdir) == "survey"


def test_clean_from_fetch_removes_all_outputs(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    clean_from(workdir, "fetch")
    for name in STAGES:
        assert not outputs_present(workdir, name)
        assert not workdir.manifest_path(name).exists()
    assert first_pending(workdir) == "fetch"


def test_clean_from_tolerates_a_missing_workdir(tmp_path: Path) -> None:
    workdir = Workdir(tmp_path / "absent")
    clean_from(workdir, "fetch")
    assert not workdir.path.exists()
