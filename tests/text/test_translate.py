from __future__ import annotations

import json
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from tongtu import model, validation
from tongtu.artifacts.common import Note, NotesFile, Session, StopReason
from tongtu.artifacts.translate import KeptParagraph, PaperGlossary, TranslateManifest, TranslateStatus
from tongtu.manifests import load_manifest, write_manifest
from tongtu.model.work import WorkOutcome
from tongtu.stages import translate
from tongtu.workdir import Workdir

MASKED = """\
\\section{Training Setup}

We train the language model with the KV cache on long sequences of text.
"""

GOOD = """\
\\section{Training Setup}

我们在长文本序列上使用 KV cache 训练语言模型。
"""

WITHOUT_KEPT_WORD = """\
\\section{Training Setup}

我们在长文本序列上使用键值缓存训练语言模型。
"""

KEEP_REASON = "论文分析的英文例句"

KEPT = MASKED.replace("\n\nWe train", f"\n\n% tongtu: keep-english: {KEEP_REASON}\nWe train")

KEPT_WARNING = f"paragraph starting at line 3 of zh.tex is kept in English: {KEEP_REASON}"

SESSION_GLOSSARY = {"terms": {"language model": "语言模型"}, "do_not_translate": []}

NOTE = {"where": "zh.tex:3", "problem": "拿不准的译法", "resolution": "沿用术语表", "suggestion": ""}

Writer = Callable[[Path], None]


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workdir:
    home = tmp_path / "home"
    monkeypatch.setenv("TONGTU_HOME", str(home))
    write_json(home / "glossary.json", {"do_not_translate": ["KV cache"]})
    paper = Workdir(tmp_path / "paper")
    paper.create()
    paper.masked.write_text(MASKED, encoding="utf-8")
    return paper


def write_json(path: Path, content: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")


def session(stop_reason: StopReason = StopReason.FINISHED) -> Session:
    return Session(stop_reason=stop_reason, model="claude-code/m", effort="low", duration_seconds=2.0)


def fake_session(
    monkeypatch: pytest.MonkeyPatch,
    write: Writer | None,
    stop_reason: StopReason = StopReason.FINISHED,
    detail: str = "",
) -> list[dict]:
    calls: list[dict] = []

    def fake_work(skill: str, session_dir: Path, **kwargs: object) -> WorkOutcome:
        calls.append({"skill": skill, "session_dir": session_dir, **kwargs})
        if write is not None:
            write(session_dir)
        return WorkOutcome(session=session(stop_reason), detail=detail)

    monkeypatch.setattr(model, "work", fake_work)
    return calls


def writes(translation: str, glossary: object = SESSION_GLOSSARY, notes: object = None) -> Writer:
    def write(session_dir: Path) -> None:
        (session_dir / "zh.tex").write_text(translation, encoding="utf-8")
        write_json(session_dir / "glossary.json", glossary)
        write_json(session_dir / "notes.json", {"notes": [NOTE]} if notes is None else notes)

    return write


def run(workdir: Workdir, **kwargs: object) -> TranslateManifest:
    manifest = translate.run(workdir, mode="fast", **kwargs)
    assert load_manifest(workdir.manifest_path("translate"), TranslateManifest) == manifest
    return manifest


def session_dir(workdir: Workdir) -> Path:
    return workdir.sandbox("translate")


def read_notes(workdir: Workdir) -> list[Note]:
    notes_file = load_manifest(workdir.notes, NotesFile)
    assert notes_file is not None
    return notes_file.notes


def test_a_session_that_passes_the_checks_is_ok(workdir: Workdir, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_session(monkeypatch, writes(GOOD))
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.OK
    assert manifest.mode == "fast"
    assert manifest.session == session()
    assert manifest.message == ""
    assert manifest.problems == []
    assert (manifest.paragraphs_total, manifest.paragraphs_translated) == (1, 1)
    assert manifest.glossary.model_dump() == {"user": 1, "learned": 0, "added": 1}
    assert workdir.translated.read_text(encoding="utf-8") == GOOD
    published = PaperGlossary.model_validate_json(workdir.paper_glossary.read_text(encoding="utf-8"))
    assert [(term.word, term.source) for term in published.terms] == [("language model", "new")]
    assert [(word.word, word.source) for word in published.do_not_translate] == [("KV cache", "user")]
    learned = json.loads((workdir.path.parent / "home" / "glossary.learned.json").read_text(encoding="utf-8"))
    assert learned["terms"] == {"language model": "语言模型"}
    assert read_notes(workdir) == [Note(session="translate", **NOTE)]
    assert not (session_dir(workdir) / "notes.json").exists()
    assert len(calls) == 1
    assert calls[0]["skill"] == "translate"
    assert calls[0]["session_dir"] == session_dir(workdir)
    assert calls[0]["mode"] == "fast"
    assert calls[0]["log_path"] == workdir.logs / "translate.jsonl"


def test_the_session_directory_is_prepared_before_the_session(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def inspect(directory: Path) -> None:
        source = directory / "en.tex"
        seen["en"] = source.read_text(encoding="utf-8")
        seen["en_writable"] = bool(source.stat().st_mode & stat.S_IWUSR)
        seen["fixed"] = json.loads((directory / "glossary.fixed.json").read_text(encoding="utf-8"))
        seen["initial"] = json.loads((directory / "glossary.json").read_text(encoding="utf-8"))
        seen["validate"] = (directory / ".skills" / "translate" / "validate.py").read_text(encoding="utf-8")
        writes(GOOD)(directory)

    write_json(workdir.path.parent / "home" / "glossary.learned.json", {"terms": {"sequence": "序列", "GPU": "显卡"}})
    fake_session(monkeypatch, inspect)
    manifest = run(workdir)
    assert seen["en"] == MASKED
    assert seen["en_writable"] is False
    assert seen["fixed"] == {"terms": {}, "do_not_translate": ["KV cache"]}
    assert seen["initial"] == {"terms": {"sequence": "序列"}, "do_not_translate": []}
    assert seen["validate"] == Path(validation.__file__).read_text(encoding="utf-8")
    assert manifest.glossary.model_dump() == {"user": 1, "learned": 1, "added": 1}


@pytest.mark.parametrize(
    ("masked", "expected"),
    [(MASKED.rstrip("\n"), MASKED.encode("utf-8")), (MASKED, MASKED.encode("utf-8"))],
    ids=["without_final_newline", "with_final_newline"],
)
def test_the_session_source_ends_with_exactly_one_added_newline(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch, masked: str, expected: bytes
) -> None:
    seen: dict = {}

    def inspect(directory: Path) -> None:
        seen["en"] = (directory / "en.tex").read_bytes()
        writes(GOOD)(directory)

    workdir.masked.write_text(masked, encoding="utf-8")
    fake_session(monkeypatch, inspect)
    manifest = run(workdir)
    assert seen["en"] == expected
    assert workdir.masked.read_text(encoding="utf-8") == masked
    assert manifest.status is TranslateStatus.OK


def test_a_translation_that_fails_the_checks_is_translate_failed(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session(monkeypatch, writes(MASKED))
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert manifest.problems and all(problem.startswith("[") for problem in manifest.problems)
    assert any(problem.startswith("[untranslated] ") for problem in manifest.problems)
    assert f"{len(manifest.problems)} problems" in manifest.message
    assert "finished" in manifest.message
    assert str(session_dir(workdir)) in manifest.message
    assert "tongtu run again" in manifest.message
    assert manifest.session == session()
    assert (manifest.paragraphs_total, manifest.paragraphs_translated) == (1, 0)
    assert not workdir.translated.exists()
    assert not workdir.paper_glossary.exists()
    assert (session_dir(workdir) / "zh.tex").read_text(encoding="utf-8") == MASKED
    assert read_notes(workdir) == [Note(session="translate", **NOTE)]


def test_a_paragraph_kept_in_english_is_ok_and_listed(workdir: Workdir, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session(monkeypatch, writes(KEPT))
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.OK
    assert manifest.problems == []
    preview = MASKED.splitlines()[2][: validation.PARAGRAPH_PREVIEW_LENGTH]
    assert manifest.kept == [KeptParagraph(line=3, preview=preview, reason=KEEP_REASON)]
    assert (manifest.paragraphs_total, manifest.paragraphs_translated) == (1, 0)
    assert KEPT_WARNING in manifest.warnings
    assert workdir.translated.read_text(encoding="utf-8") == KEPT


def test_a_failed_translation_still_lists_its_kept_paragraphs(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session(monkeypatch, writes(KEPT.replace("Training Setup", "训练设置")))
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert any(problem.startswith("[heading] ") for problem in manifest.problems)
    assert [(kept.line, kept.reason) for kept in manifest.kept] == [(3, KEEP_REASON)]
    assert KEPT_WARNING in manifest.warnings


def test_a_session_without_zh_tex_is_translate_failed(workdir: Workdir, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session(monkeypatch, None, StopReason.ERROR, "agentrun failed: boom")
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert manifest.problems == []
    assert "zh.tex was not written" in manifest.message
    assert "ended with error" in manifest.message
    assert (manifest.paragraphs_total, manifest.paragraphs_translated) == (1, 0)
    assert any("agentrun failed: boom" in warning for warning in manifest.warnings)
    assert any("notes.json" in warning for warning in manifest.warnings)
    assert read_notes(workdir) == []
    assert not workdir.translated.exists()


def test_a_timed_out_session_whose_translation_passes_is_ok_with_a_warning(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session(monkeypatch, writes(GOOD), StopReason.TIMEOUT)
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.OK
    assert manifest.session is not None and manifest.session.stop_reason is StopReason.TIMEOUT
    assert any("timeout" in warning for warning in manifest.warnings)
    assert workdir.translated.is_file()


def test_an_existing_translation_that_passes_is_reused_without_a_session(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = session_dir(workdir)
    directory.mkdir(parents=True)
    (directory / "zh.tex").write_text(GOOD, encoding="utf-8")
    write_json(directory / "glossary.json", SESSION_GLOSSARY)
    earlier = Note(session="translate", **NOTE)
    write_manifest(workdir.notes, NotesFile(notes=[earlier]))
    workdir.translated.write_text("stale", encoding="utf-8")
    calls = fake_session(monkeypatch, writes(MASKED))
    manifest = run(workdir)
    assert calls == []
    assert manifest.status is TranslateStatus.OK
    assert manifest.session is None
    assert manifest.glossary.added == 1
    assert workdir.translated.read_text(encoding="utf-8") == GOOD
    assert workdir.paper_glossary.is_file()
    assert read_notes(workdir) == [earlier]


def test_a_failing_existing_translation_is_continued_in_the_same_directory(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = session_dir(workdir)
    (directory / "parts").mkdir(parents=True)
    (directory / "parts" / "01.tex").write_text("第一节", encoding="utf-8")
    (directory / "zh.tex").write_text(MASKED, encoding="utf-8")
    (directory / "en.tex").write_text("changed by the last session", encoding="utf-8")
    write_json(directory / "glossary.json", {"terms": {"text": "文本"}})
    write_json(directory / "glossary.fixed.json", {"terms": {}})
    earlier = Note(session="translate", **NOTE)
    write_manifest(workdir.notes, NotesFile(notes=[earlier]))
    seen: dict = {}

    def inspect(current: Path) -> None:
        seen["en"] = (current / "en.tex").read_text(encoding="utf-8")
        seen["zh"] = (current / "zh.tex").read_text(encoding="utf-8")
        seen["parts"] = (current / "parts" / "01.tex").read_text(encoding="utf-8")
        seen["glossary"] = json.loads((current / "glossary.json").read_text(encoding="utf-8"))
        seen["fixed"] = json.loads((current / "glossary.fixed.json").read_text(encoding="utf-8"))
        (current / "zh.tex").write_text(GOOD, encoding="utf-8")
        write_json(current / "notes.json", {"notes": [{**NOTE, "where": "zh.tex:5"}]})

    calls = fake_session(monkeypatch, inspect)
    manifest = run(workdir)
    assert len(calls) == 1
    assert seen == {
        "en": MASKED,
        "zh": MASKED,
        "parts": "第一节",
        "glossary": {"terms": {"text": "文本"}},
        "fixed": {"terms": {}, "do_not_translate": ["KV cache"]},
    }
    assert manifest.status is TranslateStatus.OK
    assert manifest.glossary.added == 1
    assert [note.where for note in read_notes(workdir)] == ["zh.tex:3", "zh.tex:5"]


def test_a_continued_session_that_still_fails_starts_only_one_session(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = session_dir(workdir)
    directory.mkdir(parents=True)
    (directory / "zh.tex").write_text(MASKED, encoding="utf-8")
    calls = fake_session(monkeypatch, writes(MASKED))
    manifest = run(workdir)
    assert len(calls) == 1
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED


def test_an_unreadable_glossary_layer_is_glossary_invalid(workdir: Workdir, monkeypatch: pytest.MonkeyPatch) -> None:
    (workdir.path / "glossary.json").write_text("{", encoding="utf-8")
    calls = fake_session(monkeypatch, writes(GOOD))
    manifest = run(workdir)
    assert calls == []
    assert manifest.status is TranslateStatus.GLOSSARY_INVALID
    assert str(workdir.path / "glossary.json") in manifest.message
    assert manifest.session is None
    assert not session_dir(workdir).exists()


def test_a_missing_command_line_glossary_is_glossary_invalid(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_session(monkeypatch, writes(GOOD))
    manifest = run(workdir, glossary_files=[tmp_path / "absent.json"])
    assert calls == []
    assert manifest.status is TranslateStatus.GLOSSARY_INVALID


def test_a_command_line_glossary_is_used(workdir: Workdir, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    extra = tmp_path / "extra.json"
    write_json(extra, {"terms": {"sequence": "序列"}})
    fake_session(monkeypatch, writes(GOOD))
    manifest = run(workdir, glossary_files=[extra])
    assert manifest.status is TranslateStatus.OK
    assert manifest.glossary.user == 2


def test_unmatched_user_terms_become_warnings(workdir: Workdir, monkeypatch: pytest.MonkeyPatch) -> None:
    write_json(workdir.path / "glossary.json", {"terms": {"transformer": "变换器"}})
    fake_session(monkeypatch, writes(GOOD))
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.OK
    assert any("'transformer'" in warning for warning in manifest.warnings)


def test_unmatched_global_entries_become_one_warning(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    general = tmp_path / "home" / "glossary.json"
    write_json(general, {"do_not_translate": ["KV cache", "vLLM", "SGLang"]})
    fake_session(monkeypatch, writes(GOOD))
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.OK
    assert [warning for warning in manifest.warnings if str(general) in warning] == [
        f"{general}: 2 of its 3 entries do not occur in this paper"
    ]


def test_a_broken_session_glossary_is_a_problem(workdir: Workdir, monkeypatch: pytest.MonkeyPatch) -> None:
    def write(directory: Path) -> None:
        writes(GOOD)(directory)
        (directory / "glossary.json").write_text("{", encoding="utf-8")

    fake_session(monkeypatch, write)
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert len(manifest.problems) == 1
    assert manifest.problems[0].startswith("[glossary] ")
    assert "glossary.json" in manifest.problems[0]
    assert not workdir.translated.exists()


def test_session_edits_to_its_inputs_do_not_change_the_verdict(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch
) -> None:
    def write(directory: Path) -> None:
        for name in ("en.tex", "glossary.fixed.json"):
            (directory / name).chmod(0o644)
        (directory / "en.tex").write_text(WITHOUT_KEPT_WORD, encoding="utf-8")
        write_json(directory / "glossary.fixed.json", {})
        (directory / ".skills" / "translate").mkdir(parents=True, exist_ok=True)
        (directory / ".skills" / "translate" / "validate.py").write_text("def check(*a): ...", encoding="utf-8")
        writes(WITHOUT_KEPT_WORD)(directory)

    fake_session(monkeypatch, write)
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert any(problem.startswith("[glossary] ") and "KV cache" in problem for problem in manifest.problems)


def test_a_fixed_entry_wins_over_the_session_glossary(workdir: Workdir, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session(monkeypatch, writes(WITHOUT_KEPT_WORD, {"terms": {"KV cache": "键值缓存"}}))
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert any("KV cache" in problem for problem in manifest.problems)


def test_a_missing_masked_file_is_translate_failed(workdir: Workdir, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir.masked.unlink()
    calls = fake_session(monkeypatch, writes(GOOD))
    manifest = run(workdir)
    assert calls == []
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert "masked.tex" in manifest.message


def test_the_stage_removes_its_previous_outputs_but_keeps_the_session_directory(
    workdir: Workdir, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir.translated.write_text(GOOD, encoding="utf-8")
    workdir.paper_glossary.write_text("{}", encoding="utf-8")
    (session_dir(workdir) / "parts").mkdir(parents=True)
    fake_session(monkeypatch, None)
    manifest = run(workdir)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert not workdir.translated.exists()
    assert not workdir.paper_glossary.exists()
    assert (session_dir(workdir) / "parts").is_dir()


def test_the_report_names_the_session_action(workdir: Workdir, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_session(monkeypatch, writes(GOOD))
    reported: list[tuple[str, str]] = []
    run(workdir, report=lambda status, summary: reported.append((status, summary)))
    calls[0]["report"]("Write: zh.tex")
    assert reported == [("session", "running"), ("session", "Write: zh.tex")]
