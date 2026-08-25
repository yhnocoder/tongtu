from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from tongtu import validation
from tongtu.artifacts.review import ReviewManifest, ReviewStatus
from tongtu.model.config import ModelsConfig, RoleConfig, RuntimeConfig
from tongtu.model.work import StopReason, WorkOutcome
from tongtu.pipeline import outputs_present
from tongtu.stages import review
from tongtu.workdir import Workdir

SKILL_PATH = ".agent/skills/review"

BRIEF = '{"chunks": []}\n'

SOURCE = "\\section{Introduction}\n\nWe train a model with $n$ layers.\n\n⟦BLK-0⟧\n"

TRANSLATION = "\\section{引言}\n\n我们训练了一个 $n$ 层的模型。\n\n⟦BLK-0⟧\n"

REVISED = "\\section{引言}\n\n我们训练了一个 $n$ 层的网络。\n\n⟦BLK-0⟧\n"

BROKEN = "\\section{引言}\n\n我们训练了一个 $n$ 层的网络。\n\n⟦BLK-0⟧⟦BLK-0⟧\n"


def models_config() -> ModelsConfig:
    return ModelsConfig(
        runtime={"demo": RuntimeConfig(skill_path=".agent/skills/{role}", command=["runner"])},
        roles={
            review.ROLE: RoleConfig(
                model="m1",
                effort="high",
                runtime="demo",
                max_turns=4,
                timeout_seconds=60,
            )
        },
    )


def forbidden_work(*args: object, **kwargs: object) -> WorkOutcome:
    raise AssertionError("本用例不应拉起会话")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review, "load_config", lambda: (models_config(), ""))
    monkeypatch.setattr(review, "work", forbidden_work)


def wire_work(
    monkeypatch: pytest.MonkeyPatch,
    edit: Callable[[Path], None] | None = None,
    *,
    stop_reason: StopReason = StopReason.FINISHED,
    detail: str = "",
) -> list[dict]:
    calls: list[dict] = []

    def fake_work(
        role: str,
        workdir: Path,
        *,
        trace_path: Path,
        model: str | None = None,
        effort: str | None = None,
    ) -> WorkOutcome:
        calls.append({"role": role, "workdir": workdir, "trace_path": trace_path, "model": model, "effort": effort})
        if edit is not None:
            edit(workdir)
        return WorkOutcome(stop_reason=stop_reason, detail=detail)

    monkeypatch.setattr(review, "work", fake_work)
    return calls


def make_workdir(tmp_path: Path, pairs: Sequence[tuple[str, str]]) -> Workdir:
    workdir = Workdir(tmp_path / "paper")
    workdir.create()
    (workdir.build / review.CHUNKS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (workdir.build / review.TRANSLATED_DIRNAME).mkdir(parents=True, exist_ok=True)
    for index, (source, translation) in enumerate(pairs):
        chunk_id = f"c{index:03d}"
        (workdir.build / review.CHUNKS_DIRNAME / f"{chunk_id}.tex").write_text(source, encoding="utf-8")
        (workdir.build / review.TRANSLATED_DIRNAME / f"{chunk_id}.tex").write_text(translation, encoding="utf-8")
    (workdir.build / review.BRIEF_FILENAME).write_text(BRIEF, encoding="utf-8")
    return workdir


def read_manifest(workdir: Workdir) -> ReviewManifest:
    return ReviewManifest.model_validate_json(workdir.manifest_path(review.STAGE_NAME).read_text(encoding="utf-8"))


def reviewed(workdir: Workdir, chunk_id: str) -> Path:
    return workdir.build / review.REVIEWED_DIRNAME / f"{chunk_id}.tex"


def site_path(workdir: Workdir) -> Path:
    return workdir.build / review.SANDBOX_DIRNAME / review.STAGE_NAME


def in_site(workdir: Workdir, chunk_id: str) -> Path:
    return site_path(workdir) / review.REVIEWED_DIRNAME / f"{chunk_id}.tex"


def write_site(chunk_id: str, body: str) -> Callable[[Path], None]:
    def edit(site: Path) -> None:
        (site / review.REVIEWED_DIRNAME / f"{chunk_id}.tex").write_text(body, encoding="utf-8")

    return edit


def test_a_session_that_changes_nothing_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_work(monkeypatch)
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    manifest = review.run(workdir)
    assert manifest.status is ReviewStatus.OK
    assert manifest == read_manifest(workdir)
    assert manifest.changed == []
    assert manifest.reverted == []
    assert manifest.warnings == []
    assert manifest.session.stop_reason == "finished"
    assert manifest.session.model == "demo/m1"
    assert reviewed(workdir, "c000").read_text(encoding="utf-8") == TRANSLATION
    assert outputs_present(workdir, "review")
    assert calls[0]["role"] == review.ROLE
    assert calls[0]["workdir"] == site_path(workdir)
    assert calls[0]["trace_path"] == workdir.logs / review.TRACE_FILENAME


def test_the_site_holds_only_the_isolated_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_work(monkeypatch)
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    review.run(workdir)
    site = site_path(workdir)
    assert (site / review.CHUNKS_DIRNAME / "c000.tex").read_text(encoding="utf-8") == SOURCE
    assert in_site(workdir, "c000").read_text(encoding="utf-8") == TRANSLATION
    assert (site / review.BRIEF_FILENAME).read_text(encoding="utf-8") == BRIEF
    assert sorted(path.name for path in site.iterdir()) == [".agent", "brief.json", "chunks", "reviewed"]


def test_a_valid_revision_is_recorded_and_copied_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_work(monkeypatch, write_site("c000", REVISED))
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION), (SOURCE, TRANSLATION)])
    manifest = review.run(workdir)
    assert manifest.status is ReviewStatus.OK
    assert manifest.changed == ["c000"]
    assert manifest.reverted == []
    assert reviewed(workdir, "c000").read_text(encoding="utf-8") == REVISED
    assert reviewed(workdir, "c001").read_text(encoding="utf-8") == TRANSLATION


def test_a_revision_that_breaks_placeholders_is_reverted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_work(monkeypatch, write_site("c000", BROKEN))
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    manifest = review.run(workdir)
    assert manifest.status is ReviewStatus.OK
    assert manifest.changed == ["c000"]
    assert manifest.reverted == ["c000"]
    assert reviewed(workdir, "c000").read_text(encoding="utf-8") == TRANSLATION


def test_a_deleted_file_is_reverted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def remove(site: Path) -> None:
        (site / review.REVIEWED_DIRNAME / "c000.tex").unlink()

    wire_work(monkeypatch, remove)
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    manifest = review.run(workdir)
    assert manifest.status is ReviewStatus.OK
    assert manifest.changed == ["c000"]
    assert manifest.reverted == ["c000"]
    assert reviewed(workdir, "c000").read_text(encoding="utf-8") == TRANSLATION


def test_an_added_file_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_work(monkeypatch, write_site("c009", REVISED))
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    manifest = review.run(workdir)
    assert manifest.status is ReviewStatus.OK
    assert manifest.changed == []
    assert manifest.reverted == []
    assert sorted(path.name for path in (workdir.build / review.REVIEWED_DIRNAME).iterdir()) == ["c000.tex"]


def test_a_session_error_fails_the_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_work(monkeypatch, write_site("c000", REVISED), stop_reason=StopReason.ERROR, detail="运行时不在 PATH 里")
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    manifest = review.run(workdir)
    assert manifest.status is ReviewStatus.REVIEW_FAILED
    assert manifest == read_manifest(workdir)
    assert manifest.message == "运行时不在 PATH 里"
    assert manifest.session.stop_reason == "error"
    assert manifest.changed == []
    assert not (workdir.build / review.REVIEWED_DIRNAME).exists()
    assert not outputs_present(workdir, "review")


def test_a_session_timeout_keeps_the_revisions_and_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_work(monkeypatch, write_site("c000", REVISED), stop_reason=StopReason.TIMEOUT)
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    manifest = review.run(workdir)
    assert manifest.status is ReviewStatus.OK
    assert manifest.session.stop_reason == "timeout"
    assert manifest.warnings == [review.TIMEOUT_WARNING]
    assert manifest.changed == ["c000"]
    assert reviewed(workdir, "c000").read_text(encoding="utf-8") == REVISED


def test_leading_and_trailing_whitespace_comes_from_the_source_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_work(monkeypatch, write_site("c000", REVISED.strip()))
    workdir = make_workdir(tmp_path, [("\n\n" + SOURCE.strip() + "\n\n", TRANSLATION)])
    manifest = review.run(workdir)
    assert manifest.changed == ["c000"]
    assert reviewed(workdir, "c000").read_text(encoding="utf-8") == "\n\n" + REVISED.strip() + "\n\n"


def test_a_rerun_clears_the_previous_site_and_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_work(monkeypatch)
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    site = site_path(workdir)
    site.mkdir(parents=True, exist_ok=True)
    (site / "leftover.txt").write_text("上一轮的文件", encoding="utf-8")
    (workdir.build / review.REVIEWED_DIRNAME).mkdir(parents=True, exist_ok=True)
    (workdir.build / review.REVIEWED_DIRNAME / "c009.tex").write_text("上一轮的译文", encoding="utf-8")
    (workdir.logs / review.TRACE_FILENAME).write_text("上一轮的 trace", encoding="utf-8")
    review.run(workdir)
    assert not (site / "leftover.txt").exists()
    assert not reviewed(workdir, "c009").exists()
    assert not (workdir.logs / review.TRACE_FILENAME).exists()


def test_without_chunks_the_stage_fails(tmp_path: Path) -> None:
    workdir = Workdir(tmp_path / "paper")
    workdir.create()
    manifest = review.run(workdir)
    assert manifest.status is ReviewStatus.REVIEW_FAILED
    assert review.CHUNKS_DIRNAME in manifest.message
    assert manifest.session is None


def test_without_a_translation_the_stage_fails(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    (workdir.build / review.TRANSLATED_DIRNAME / "c000.tex").unlink()
    manifest = review.run(workdir)
    assert manifest.status is ReviewStatus.REVIEW_FAILED
    assert "FileNotFoundError" in manifest.message


def test_skip_copies_the_translation_without_a_session(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    manifest = review.run(workdir, skip=True)
    assert manifest.status is ReviewStatus.OK
    assert manifest == read_manifest(workdir)
    assert manifest.session is None
    assert manifest.changed == []
    assert manifest.reverted == []
    assert manifest.message == review.SKIPPED_MESSAGE
    assert reviewed(workdir, "c000").read_text(encoding="utf-8") == TRANSLATION
    assert not site_path(workdir).exists()
    assert outputs_present(workdir, "review")


def test_a_role_pointing_at_an_unknown_runtime_fails_the_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = ModelsConfig(roles={review.ROLE: RoleConfig(model="m1", effort="high", runtime="nowhere")})
    monkeypatch.setattr(review, "load_config", lambda: (config, ""))
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    manifest = review.run(workdir)
    assert manifest.status is ReviewStatus.REVIEW_FAILED
    assert manifest.session is None
    assert "nowhere" in manifest.message


def run_validate_script(site: Path, source: Path, translation: Path) -> int:
    script = site / SKILL_PATH / review.VALIDATE_FILENAME
    done = subprocess.run(
        [sys.executable, "-I", str(script), str(source), str(translation)],
        capture_output=True,
        text=True,
        cwd=site,
    )
    return done.returncode


def test_the_site_carries_a_validate_script_that_judges_as_the_library_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_work(monkeypatch)
    workdir = make_workdir(tmp_path, [(SOURCE, TRANSLATION)])
    review.run(workdir)
    site = site_path(workdir)
    assert (site / SKILL_PATH / review.VALIDATE_FILENAME).is_file()
    source_path = site / review.CHUNKS_DIRNAME / "c000.tex"
    for body in (TRANSLATION, REVISED, BROKEN, SOURCE.replace("⟦BLK-0⟧", ""), "\\section{引言}\n"):
        translation_path = tmp_path / "candidate.tex"
        translation_path.write_text(body, encoding="utf-8")
        expected = validation.validate(SOURCE.strip(), body.strip()).ok
        assert run_validate_script(site, source_path, translation_path) == (0 if expected else 1)
