from __future__ import annotations

import json
from pathlib import Path

import pytest

from tongtu import notes
from tongtu.artifacts.common import Note, NotesFile
from tongtu.manifests import load_manifest, write_manifest

WRITTEN = {"where": "zh.tex:12", "problem": "检查误报", "resolution": "保留原文", "suggestion": "改 validation.py"}


def write_session_notes(session_dir: Path, content: str) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "notes.json"
    path.write_text(content, encoding="utf-8")
    return path


def read_notes(path: Path) -> list[Note]:
    notes_file = load_manifest(path, NotesFile)
    assert notes_file is not None
    return notes_file.notes


def test_collect_tags_each_note_with_the_session_and_removes_the_session_file(tmp_path: Path) -> None:
    written = write_session_notes(tmp_path / "session", json.dumps({"notes": [WRITTEN, {**WRITTEN, "where": "b"}]}))
    out = tmp_path / "out" / "paper.notes.json"
    assert notes.collect(tmp_path / "session", "translate", out) == []
    assert read_notes(out) == [
        Note(session="translate", **WRITTEN),
        Note(session="translate", **{**WRITTEN, "where": "b"}),
    ]
    assert not written.exists()


def test_collect_appends_after_the_existing_notes(tmp_path: Path) -> None:
    out = tmp_path / "paper.notes.json"
    earlier = Note(session="precompile_fix", where="main.tex:3", problem="p", resolution="r", suggestion="")
    write_manifest(out, NotesFile(notes=[earlier]))
    write_session_notes(tmp_path / "session", json.dumps({"notes": [WRITTEN]}))
    notes.collect(tmp_path / "session", "compile_fix", out)
    assert read_notes(out) == [earlier, Note(session="compile_fix", **WRITTEN)]


def test_collect_of_an_empty_list_still_writes_the_notes_file(tmp_path: Path) -> None:
    write_session_notes(tmp_path / "session", '{"notes": []}')
    out = tmp_path / "paper.notes.json"
    assert notes.collect(tmp_path / "session", "translate", out) == []
    assert read_notes(out) == []


@pytest.mark.parametrize(
    "content",
    [
        None,
        "{",
        '{"notes": [{"where": "x"}]}',
        '{"items": []}',
        '{"notes": "none"}',
    ],
)
def test_collect_warns_on_a_missing_or_malformed_file_and_keeps_the_notes_file(
    tmp_path: Path, content: str | None
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    if content is not None:
        write_session_notes(session_dir, content)
    out = tmp_path / "paper.notes.json"
    warnings = notes.collect(session_dir, "precompile_fix", out)
    assert len(warnings) == 1
    assert "notes.json" in warnings[0] and "precompile_fix" in warnings[0]
    assert read_notes(out) == []
    assert not (session_dir / "notes.json").exists()


def test_collect_after_a_malformed_file_keeps_the_existing_notes(tmp_path: Path) -> None:
    out = tmp_path / "paper.notes.json"
    earlier = Note(session="translate", **WRITTEN)
    write_manifest(out, NotesFile(notes=[earlier]))
    write_session_notes(tmp_path / "session", "not json")
    assert len(notes.collect(tmp_path / "session", "translate", out)) == 1
    assert read_notes(out) == [earlier]


def test_remove_drops_only_the_named_session(tmp_path: Path) -> None:
    out = tmp_path / "paper.notes.json"
    entries = [Note(session=session, **WRITTEN) for session in ("precompile_fix", "translate", "compile_fix")]
    write_manifest(out, NotesFile(notes=entries))
    notes.remove(out, "translate")
    assert [entry.session for entry in read_notes(out)] == ["precompile_fix", "compile_fix"]


def test_remove_without_a_notes_file_does_nothing(tmp_path: Path) -> None:
    notes.remove(tmp_path / "paper.notes.json", "translate")
    assert not (tmp_path / "paper.notes.json").exists()
