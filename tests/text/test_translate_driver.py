"""translate 驱动器里不调 `ask` 的四组逻辑，用例取自 stages/translate.md 验收与试跑对象一节。

前置条件分流、跳过判定、上下文组装与出口判定都在调模型之前或之后，可以在文本层机械覆盖；
真正调 `ask` 的翻译循环属 LLM 层，由真实论文人工核对，不在这里跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tongtu import manifests, workdir
from tongtu.agent.base import ASK_STATUS_OK, AskOutcome
from tongtu.artifacts.chunk import ChunkManifest, ChunkRecord, ChunkStatus
from tongtu.artifacts.survey import BriefFile, BriefHeading, GlossaryFile, SurveyManifest, SurveyStatus, TermEntry
from tongtu.artifacts.translate import ChunkTranslateStatus, TranslateManifest, TranslateStatus
from tongtu.chunking import Part
from tongtu.stages import chunk as chunk_stage
from tongtu.stages import survey as survey_stage
from tongtu.stages import translate as translate_stage

#: 三个 chunk：front、body、纯 placeholder 的 appendix，覆盖上下文组装的两种分派与跳过翻译。
CHUNK_BODIES: dict[str, str] = {
    "c000": "\\section{Abstract}\n\nWe present a toy model.\n",
    "c001": "\\section{Method}\n\nThe method has two parts.\n\nEach part is simple.\n",
    "c002": "⟦BLK-0⟧\n",
}

CHUNK_PARTS: dict[str, Part] = {"c000": Part.FRONT, "c001": Part.BODY, "c002": Part.APPENDIX}


def build_workdir(
    tmp_path: Path,
    *,
    chunk_status: ChunkStatus = ChunkStatus.OK,
    survey_status: SurveyStatus = SurveyStatus.OK,
    write_survey_manifest: bool = True,
    write_brief: bool = True,
) -> workdir.Workdir:
    """造一个前置条件齐备的论文工作目录，各参数用来逐条拆掉某一项。"""
    paper_workdir = workdir.Workdir(tmp_path / "fixture-paper")
    paper_workdir.create()

    (paper_workdir.build / "chunks").mkdir(parents=True, exist_ok=True)
    records = []
    for index, (chunk_id, body) in enumerate(CHUNK_BODIES.items()):
        chunk_stage.chunk_path(paper_workdir, chunk_id).write_text(body, encoding="utf-8")
        records.append(
            ChunkRecord(
                id=chunk_id,
                start=index,
                end=index + 1,
                sha256="",
                token_estimate=10,
                paragraphs=2,
                part=CHUNK_PARTS[chunk_id],
                translatable_chars=0 if chunk_id == "c002" else len(body),
            )
        )
    manifests.write_manifest(
        paper_workdir.manifest_path(chunk_stage.STAGE_NAME),
        ChunkManifest(status=chunk_status, chunks=records, chunks_total=len(records), chunks_sha256="chunkhash"),
    )

    manifests.write_manifest(
        survey_stage.glossary_file_path(paper_workdir),
        GlossaryFile(terms=[TermEntry(word="model", translation="模型", decided_by="paper")], style="平实直述。"),
    )
    if write_brief:
        manifests.write_manifest(
            survey_stage.brief_path(paper_workdir),
            BriefFile(
                abstract="A toy abstract.",
                heading_tree=[
                    BriefHeading(depth=1, level="section", argument="Abstract"),
                    BriefHeading(depth=2, level="subsection", argument="Method"),
                ],
            ),
        )
    if write_survey_manifest:
        manifests.write_manifest(
            paper_workdir.manifest_path(survey_stage.STAGE_NAME),
            SurveyManifest(status=survey_status, glossary_sha256="glossaryhash", brief_sha256="briefhash"),
        )
    return paper_workdir


def write_skippable_result(paper_workdir: workdir.Workdir, **overrides: object) -> TranslateManifest:
    """写一份「六个判定值都对得上」的 translate manifest 与配套译文文件。"""
    fields: dict[str, object] = {
        "status": TranslateStatus.OK,
        "chunks_sha256": "chunkhash",
        "glossary_sha256": "glossaryhash",
        "brief_sha256": "briefhash",
        "model_id": translate_stage.DEFAULT_MODEL,
        "prompt_version": translate_stage._prompt_asset()[1],
        "fallback_ratio": 0.0,
        "chunks": [
            {"id": chunk_id, "status": ChunkTranslateStatus.TRANSLATED, "sha256": "", "attempts": 1}
            for chunk_id in CHUNK_BODIES
        ],
    }
    fields.update(overrides)
    manifest = TranslateManifest(**fields)
    translate_stage.translated_dir(paper_workdir).mkdir(parents=True, exist_ok=True)
    for chunk_id in CHUNK_BODIES:
        translate_stage.translated_path(paper_workdir, chunk_id).write_text("译文\n", encoding="utf-8")
    manifests.write_manifest(paper_workdir.manifest_path(translate_stage.STAGE_NAME), manifest)
    return manifest


def run(paper_workdir: workdir.Workdir, **kwargs: object) -> TranslateManifest:
    """跑 translate 并返回它落盘的 manifest。"""
    return translate_stage.translate(workdir_path=paper_workdir.path, **kwargs).manifest


# ------------------------------------------------------------------ 前置条件分流


def test_missing_chunk_manifest_reports_chunk_missing(tmp_path: Path) -> None:
    """chunk manifest 不在 → chunk_missing，不抛栈，结论照常落盘。"""
    paper_workdir = build_workdir(tmp_path)
    paper_workdir.manifest_path(chunk_stage.STAGE_NAME).unlink()
    assert run(paper_workdir).status is TranslateStatus.CHUNK_MISSING


def test_absent_chunk_file_reports_chunk_missing(tmp_path: Path) -> None:
    """chunk 状态是 ok 但 chunk 文件缺 → chunk_missing，message 指名到具体 id。"""
    paper_workdir = build_workdir(tmp_path)
    chunk_stage.chunk_path(paper_workdir, "c001").unlink()
    manifest = run(paper_workdir)
    assert manifest.status is TranslateStatus.CHUNK_MISSING
    assert "c001" in manifest.message


def test_chunk_status_not_ok_reports_chunk_not_ok(tmp_path: Path) -> None:
    """chunk 状态不是 ok → chunk_not_ok。"""
    paper_workdir = build_workdir(tmp_path, chunk_status=ChunkStatus.MASK_NOT_OK)
    assert run(paper_workdir).status is TranslateStatus.CHUNK_NOT_OK


def test_chunk_status_is_reported_before_a_missing_survey_manifest(tmp_path: Path) -> None:
    """chunk 与 survey 同时不满足时报 chunk 那条。

    设计稿的前置条件把 chunk 两条排在 survey 两条之前。顺序反了的话，pdf_only 沿链的
    `chunk_not_ok` 会变成 `survey_missing`，退出码从 3 掉成 1。
    """
    paper_workdir = build_workdir(tmp_path, chunk_status=ChunkStatus.MASK_NOT_OK, write_survey_manifest=False)
    assert run(paper_workdir).status is TranslateStatus.CHUNK_NOT_OK


def test_missing_survey_manifest_reports_survey_missing(tmp_path: Path) -> None:
    """survey manifest 不在 → survey_missing。"""
    paper_workdir = build_workdir(tmp_path, write_survey_manifest=False)
    assert run(paper_workdir).status is TranslateStatus.SURVEY_MISSING


def test_missing_brief_reports_survey_missing(tmp_path: Path) -> None:
    """survey 状态是 ok 但 build/brief.json 不在 → survey_missing。"""
    paper_workdir = build_workdir(tmp_path, write_brief=False)
    manifest = run(paper_workdir)
    assert manifest.status is TranslateStatus.SURVEY_MISSING
    assert "brief.json" in manifest.message


def test_survey_status_not_ok_reports_survey_not_ok(tmp_path: Path) -> None:
    """survey 状态不是 ok → survey_not_ok。"""
    paper_workdir = build_workdir(tmp_path, survey_status=SurveyStatus.MASK_NOT_OK)
    assert run(paper_workdir).status is TranslateStatus.SURVEY_NOT_OK


# ------------------------------------------------------------------ 跳过判定


def test_all_six_values_matching_skips_the_stage(tmp_path: Path) -> None:
    """六个值全对得上 → 跳过，不调 ask。"""
    paper_workdir = build_workdir(tmp_path)
    write_skippable_result(paper_workdir)
    assert translate_stage.translate(workdir_path=paper_workdir.path).skipped


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"status": TranslateStatus.TRANSLATE_FAILED}, id="状态不是 ok"),
        pytest.param({"chunks_sha256": "changed"}, id="chunks_sha256 变了"),
        pytest.param({"glossary_sha256": "changed"}, id="glossary_sha256 变了"),
        pytest.param({"brief_sha256": "changed"}, id="brief_sha256 变了"),
        pytest.param({"model_id": "other-model"}, id="换了模型"),
        pytest.param({"prompt_version": "0"}, id="prompt 资产升级"),
        pytest.param({"fallback_ratio": 0.5}, id="上次的回退比例超出本次阈值"),
    ],
)
def test_any_single_value_changing_defeats_the_skip(tmp_path: Path, overrides: dict[str, object]) -> None:
    """六个判定值任一不符即不跳过；阈值那条用上次的比例超出本次阈值来触发。"""
    paper_workdir = build_workdir(tmp_path)
    manifest = write_skippable_result(paper_workdir, **overrides)
    assert (
        translate_stage._load_skippable_manifest(
            paper_workdir,
            ChunkManifest(status=ChunkStatus.OK, chunks_sha256="chunkhash"),
            SurveyManifest(status=SurveyStatus.OK, glossary_sha256="glossaryhash", brief_sha256="briefhash"),
            translate_stage.DEFAULT_MODEL,
            translate_stage._prompt_asset()[1],
            translate_stage.DEFAULT_MAX_FALLBACK_RATIO,
        )
        is None
    ), f"manifest {manifest.status} 不该被判为可跳过"


def test_a_missing_translation_file_defeats_the_skip(tmp_path: Path) -> None:
    """manifest 说 ok 但译文文件被删（`--force` 中途被打断即是此形）→ 不跳过。"""
    paper_workdir = build_workdir(tmp_path)
    write_skippable_result(paper_workdir)
    translate_stage.translated_path(paper_workdir, "c001").unlink()
    assert (
        translate_stage._load_skippable_manifest(
            paper_workdir,
            ChunkManifest(status=ChunkStatus.OK, chunks_sha256="chunkhash"),
            SurveyManifest(status=SurveyStatus.OK, glossary_sha256="glossaryhash", brief_sha256="briefhash"),
            translate_stage.DEFAULT_MODEL,
            translate_stage._prompt_asset()[1],
            translate_stage.DEFAULT_MAX_FALLBACK_RATIO,
        )
        is None
    )


# ------------------------------------------------------------------ 上下文组装


def contexts_by_id(tmp_path: Path) -> dict[str, translate_stage.ChunkContext]:
    paper_workdir = build_workdir(tmp_path)
    chunk_manifest = manifests.load_manifest(paper_workdir.manifest_path(chunk_stage.STAGE_NAME), ChunkManifest)
    assert chunk_manifest is not None
    resolved_glossary = manifests.load_manifest(survey_stage.glossary_file_path(paper_workdir), GlossaryFile)
    brief = manifests.load_manifest(survey_stage.brief_path(paper_workdir), BriefFile)
    assert resolved_glossary is not None and brief is not None
    built = translate_stage._contexts(chunk_manifest.chunks, dict(CHUNK_BODIES), resolved_glossary, brief)
    return {context.id: context for context in built}


def test_front_chunk_takes_the_heading_tree_and_neither_abstract_nor_neighbors(tmp_path: Path) -> None:
    """front chunk 自身即摘要、且是首个 chunk：带标题树，不带摘要与邻段。"""
    front = contexts_by_id(tmp_path)["c000"]
    assert front.heading_tree == ("- Abstract", "  - Method")
    assert front.abstract is None
    assert (front.previous, front.following) == ("", "")


def test_body_chunk_takes_the_abstract_and_neighbors_but_not_the_heading_tree(tmp_path: Path) -> None:
    """body 与 appendix chunk 反过来：带摘要与邻段，不带标题树。"""
    body = contexts_by_id(tmp_path)["c001"]
    assert body.heading_tree == ()
    assert body.abstract == "A toy abstract."
    assert "toy model" in body.previous
    assert "⟦BLK-0⟧" in body.following


def test_glossary_hits_are_filtered_per_chunk(tmp_path: Path) -> None:
    """术语按 chunk 命中过滤：`model` 只出现在 front chunk 的正文里。"""
    built = contexts_by_id(tmp_path)
    assert built["c000"].terms == ("model → 模型",)
    assert built["c001"].terms == ()
    assert built["c000"].style == "平实直述。"


# ------------------------------------------------------------------ 出口判定


def finish(tmp_path: Path, statuses: dict[str, ChunkTranslateStatus], max_fallback_ratio: float) -> TranslateManifest:
    paper_workdir = build_workdir(tmp_path)
    built = contexts_by_id(tmp_path)
    contexts = [built[chunk_id] for chunk_id in CHUNK_BODIES]
    outcomes = {
        chunk_id: translate_stage.ChunkOutcome(id=chunk_id, status=status, body=CHUNK_BODIES[chunk_id].strip())
        for chunk_id, status in statuses.items()
    }
    return translate_stage._finish(
        paper_workdir,
        ChunkManifest(status=ChunkStatus.OK),
        SurveyManifest(status=SurveyStatus.OK),
        contexts,
        outcomes,
        model_id=translate_stage.DEFAULT_MODEL,
        prompt_version="1",
        jobs=1,
        max_fallback_ratio=max_fallback_ratio,
    )


def test_skipped_chunks_are_not_in_the_fallback_ratio_denominator(tmp_path: Path) -> None:
    """分母是参与翻译的 chunk 数：三个 chunk 里一个跳过、一个回退，比例是 1/2 而不是 1/3。"""
    manifest = finish(
        tmp_path,
        {
            "c000": ChunkTranslateStatus.TRANSLATED,
            "c001": ChunkTranslateStatus.FALLBACK,
            "c002": ChunkTranslateStatus.SKIPPED,
        },
        max_fallback_ratio=0.6,
    )
    assert manifest.fallback_ratio == pytest.approx(0.5)
    assert manifest.skipped_chunks == ["c002"]
    assert manifest.status is TranslateStatus.OK


def test_exceeding_the_threshold_fails_the_stage_but_still_writes_the_translations(tmp_path: Path) -> None:
    """回退比例超阈值整体判失败，译文与 manifest 照常落盘——判失败是给退出码看的。"""
    paper_workdir = workdir.Workdir(tmp_path / "fixture-paper")
    manifest = finish(
        tmp_path,
        {
            "c000": ChunkTranslateStatus.TRANSLATED,
            "c001": ChunkTranslateStatus.FALLBACK,
            "c002": ChunkTranslateStatus.SKIPPED,
        },
        max_fallback_ratio=0.2,
    )
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert manifest.fallback_chunks == ["c001"]
    assert all(translate_stage.translated_path(paper_workdir, chunk_id).is_file() for chunk_id in CHUNK_BODIES)


def test_all_chunks_skipped_gives_a_zero_ratio_instead_of_dividing_by_zero(tmp_path: Path) -> None:
    """一个 chunk 都没参与翻译时比例记 0，不除零。"""
    manifest = finish(
        tmp_path,
        dict.fromkeys(CHUNK_BODIES, ChunkTranslateStatus.SKIPPED),
        max_fallback_ratio=0.2,
    )
    assert manifest.fallback_ratio == 0.0
    assert manifest.status is TranslateStatus.OK


# ------------------------------------------------------------------ 重试额度


class FakeAsk:
    """按剧本返回的 `ask` 替身：每次调用取剧本的下一项，记下实际被调用了几次。"""

    def __init__(self, script: list[str]) -> None:
        self.script = script
        self.calls = 0

    def __call__(self, **kwargs: object) -> object:
        self.calls += 1
        text = self.script[min(self.calls - 1, len(self.script) - 1)]
        return AskOutcome(status=ASK_STATUS_OK, text=text)


def translate_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: list[str]) -> tuple[object, int]:
    """拿 c001 走一遍 `_translate_chunk`，返回（结论、实际 ask 调用次数）。"""
    paper_workdir = build_workdir(tmp_path)
    fake = FakeAsk(script)
    monkeypatch.setattr(translate_stage.opencode, "ask", fake)
    context = contexts_by_id(tmp_path)["c001"]
    return translate_stage._translate_chunk(context, "skill", "fake-model", paper_workdir), fake.calls


def test_an_empty_response_does_not_consume_the_retry_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """前两次返回空译文，第三次给出合格译文：仍然判 translated，空返回没有吃掉重试额度。"""
    outcome, calls = translate_one(monkeypatch, tmp_path, ["", "   \n", CHUNK_BODIES["c001"].strip()])
    assert outcome.status is ChunkTranslateStatus.TRANSLATED
    assert (calls, outcome.attempts) == (3, 3)


def test_persistent_empty_responses_stop_at_the_hard_ceiling(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """模型一直返回空译文时由 MAX_ASK_CALLS 兜住，不会无限循环。"""
    outcome, calls = translate_one(monkeypatch, tmp_path, [""])
    assert outcome.status is ChunkTranslateStatus.FALLBACK
    assert calls == translate_stage.MAX_ASK_CALLS


def test_a_judgeable_but_wrong_translation_consumes_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """译文能评判但四层不过时照常消耗额度：MAX_RETRIES=2 即三次可评判尝试后回退。"""
    outcome, calls = translate_one(monkeypatch, tmp_path, ["少了命令的译文"])
    assert outcome.status is ChunkTranslateStatus.FALLBACK
    assert calls == translate_stage.MAX_RETRIES + 1
