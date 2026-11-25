from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError

from .artifacts.common import Note, NoteSession, NotesFile
from .manifests import describe_error, load_manifest, write_manifest
from .workdir import ENCODING

SESSION_NOTES_FILENAME = "notes.json"


class WrittenNote(BaseModel):
    where: str
    problem: str
    resolution: str
    suggestion: str


class WrittenNotes(BaseModel):
    notes: list[WrittenNote]


def collect(session_dir: Path, session: NoteSession, notes_path: Path) -> list[str]:
    written_path = session_dir / SESSION_NOTES_FILENAME
    existing = load_manifest(notes_path, NotesFile) or NotesFile()
    warnings: list[str] = []
    try:
        written = WrittenNotes.model_validate_json(written_path.read_text(encoding=ENCODING))
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        written = WrittenNotes(notes=[])
        warnings.append(
            f"cannot read {SESSION_NOTES_FILENAME} written by the {session} session ({describe_error(error)}); "
            "its notes are not collected"
        )
    collected = [Note(session=session, **note.model_dump()) for note in written.notes]
    write_manifest(notes_path, NotesFile(notes=[*existing.notes, *collected]))
    written_path.unlink(missing_ok=True)
    return warnings


def remove(notes_path: Path, session: NoteSession) -> None:
    existing = load_manifest(notes_path, NotesFile)
    if existing is None:
        return
    write_manifest(notes_path, NotesFile(notes=[note for note in existing.notes if note.session != session]))
