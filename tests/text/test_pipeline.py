from __future__ import annotations

from pathlib import Path

import pytest

from tongtu.artifacts.common import Note, NotesFile
from tongtu.manifests import load_manifest, write_manifest
from tongtu.pipeline import STAGES, clean, clean_from, downstream, first_pending, outputs_present
from tongtu.workdir import Workdir, raw_log_path

OUTPUT_FILES: dict[str, tuple[str, ...]] = {
    "fetch": ("src/main.tex",),
    "precompile": ("build/precompile.tex", "build/precompile.pdf"),
    "mask": ("build/masked.tex", "build/blocks.json"),
    "translate": ("build/translated.tex",),
    "compile": ("build/zh.tex", "out/paper.zh.pdf"),
}

SIDE_FILES: dict[str, tuple[str, ...]] = {
    "fetch": ("build/e-print.bin", "out/paper.meta.json"),
    "precompile": (
        "build/sandbox/tex/main.tex",
        "logs/precompile-fix.jsonl",
        "logs/precompile-fix.raw.jsonl",
    ),
    "mask": (),
    "translate": (
        "build/sandbox/translate/zh.tex",
        "out/paper.glossary.json",
        "logs/translate.jsonl",
        "logs/translate.raw.jsonl",
    ),
    "compile": (
        "out/paper.zh.anchors.json",
        "build/zh.synctex.gz",
        "logs/compile-fix.jsonl",
        "logs/compile-fix.raw.jsonl",
    ),
}

NOTE_SESSIONS = ("precompile_fix", "translate", "compile_fix")


def note(session: str) -> Note:
    return Note(session=session, where="main.tex:1", problem="p", resolution="r", suggestion="")


def write_manifest_status(workdir: Workdir, name: str, status: str = "ok") -> None:
    manifest_path = workdir.manifest_path(name)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(f'{{"status": "{status}", "warnings": [], "message": ""}}\n', encoding="utf-8")


def write_outputs(root: Path, *stages: str) -> Workdir:
    workdir = Workdir(root / "paper")
    for name in stages:
        for relative in (*OUTPUT_FILES[name], *SIDE_FILES[name]):
            path = workdir.path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding="utf-8")
        write_manifest_status(workdir, name)
    write_manifest(workdir.notes, NotesFile(notes=[note(session) for session in NOTE_SESSIONS]))
    return workdir


def noted_sessions(workdir: Workdir) -> list[str]:
    notes_file = load_manifest(workdir.notes, NotesFile)
    assert notes_file is not None
    return [entry.session for entry in notes_file.notes]


def test_stage_order() -> None:
    assert STAGES == ("fetch", "precompile", "mask", "translate", "compile")


def test_downstream_starts_at_the_given_stage() -> None:
    assert downstream("translate") == ("translate", "compile")
    assert downstream("fetch") == STAGES


def test_raw_log_sits_next_to_the_event_log(tmp_path: Path) -> None:
    assert raw_log_path(tmp_path / "logs" / "translate.jsonl") == tmp_path / "logs" / "translate.raw.jsonl"


def test_first_pending_without_any_manifest(tmp_path: Path) -> None:
    assert first_pending(Workdir(tmp_path / "paper")) == "fetch"


def test_first_pending_resumes_after_the_stages_that_finished_ok(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, "fetch", "precompile")
    assert first_pending(workdir) == "mask"


def test_first_pending_stops_at_a_failed_manifest_even_with_outputs(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    write_manifest_status(workdir, "translate", "translate_failed")
    assert first_pending(workdir) == "translate"


def test_first_pending_ignores_outputs_without_a_manifest(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    workdir.manifest_path("mask").unlink()
    assert outputs_present(workdir, "mask")
    assert first_pending(workdir) == "mask"


def test_first_pending_with_every_stage_ok(tmp_path: Path) -> None:
    assert first_pending(write_outputs(tmp_path, *STAGES)) is None


def test_empty_directory_counts_as_absent(tmp_path: Path) -> None:
    workdir = Workdir(tmp_path / "paper")
    workdir.src.mkdir(parents=True)
    assert not outputs_present(workdir, "fetch")


def test_clean_from_removes_stage_and_downstream(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    clean_from(workdir, "translate")
    for name in ("fetch", "precompile", "mask"):
        assert outputs_present(workdir, name)
        assert workdir.manifest_path(name).is_file()
        for relative in SIDE_FILES[name]:
            assert (workdir.path / relative).is_file()
    for name in ("translate", "compile"):
        assert not outputs_present(workdir, name)
        assert not workdir.manifest_path(name).exists()
        for relative in (*OUTPUT_FILES[name], *SIDE_FILES[name]):
            assert not (workdir.path / relative).exists()
    assert not workdir.sandbox("translate").exists()
    assert noted_sessions(workdir) == ["precompile_fix"]
    assert first_pending(workdir) == "translate"


@pytest.mark.parametrize(
    ("stage", "kept"),
    [
        ("precompile", ["translate", "compile_fix"]),
        ("translate", ["precompile_fix", "compile_fix"]),
        ("compile", ["precompile_fix", "translate"]),
        ("mask", list(NOTE_SESSIONS)),
    ],
)
def test_clean_removes_only_the_notes_of_its_own_session(tmp_path: Path, stage: str, kept: list[str]) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    clean(workdir, stage)
    assert noted_sessions(workdir) == kept
    for relative in (*OUTPUT_FILES[stage], *SIDE_FILES[stage]):
        assert not (workdir.path / relative).exists()


def test_clean_from_fetch_removes_all_outputs(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    clean_from(workdir, "fetch")
    for name in STAGES:
        assert not outputs_present(workdir, name)
        assert not workdir.manifest_path(name).exists()
    assert noted_sessions(workdir) == []
    assert first_pending(workdir) == "fetch"


def test_clean_from_fetch_keeps_the_run_record(tmp_path: Path) -> None:
    workdir = write_outputs(tmp_path, *STAGES)
    workdir.last_run.write_text("{}", encoding="utf-8")
    clean_from(workdir, "fetch")
    assert workdir.last_run.read_text(encoding="utf-8") == "{}"


def test_clean_from_tolerates_a_missing_workdir(tmp_path: Path) -> None:
    workdir = Workdir(tmp_path / "absent")
    clean_from(workdir, "fetch")
    assert not workdir.path.exists()
