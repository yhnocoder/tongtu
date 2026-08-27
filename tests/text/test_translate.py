from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from tongtu import masking
from tongtu.artifacts.survey import (
    BriefFile,
    ChunkRecord,
    DecidedBy,
    DoNotTranslateEntry,
    Heading,
    Part,
    TermEntry,
)
from tongtu.artifacts.translate import ChunkTranslateStatus, TranslateManifest, TranslateStatus
from tongtu.model.ask import AskOutcome, AskStatus
from tongtu.model.config import ModelsConfig, ProviderConfig, RoleConfig
from tongtu.pipeline import outputs_present
from tongtu.stages import translate
from tongtu.workdir import Workdir

Reply = Callable[[Mapping[str, object], int], AskOutcome]

MODEL = "p/m"


def role_config() -> ModelsConfig:
    return ModelsConfig(
        provider={"p": ProviderConfig(base_url="https://provider.example", api="chat")},
        roles={translate.ROLE: RoleConfig(model="m", effort="low", provider="p")},
    )


def forbidden_ask(**kwargs: object) -> AskOutcome:
    raise AssertionError("本用例不应调用模型")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(translate, "load_config", lambda: (role_config(), ""))
    monkeypatch.setattr(translate, "ask", forbidden_ask)


def echo(kwargs: Mapping[str, object], index: int) -> AskOutcome:
    return AskOutcome(status=AskStatus.OK, text=unwrapped(str(kwargs["messages"][0][1])), model=MODEL)


def wire_ask(monkeypatch: pytest.MonkeyPatch, reply: Reply = echo) -> list[dict]:
    calls: list[dict] = []

    def fake_ask(**kwargs: object) -> AskOutcome:
        calls.append(kwargs)
        return reply(kwargs, len(calls))

    monkeypatch.setattr(translate, "ask", fake_ask)
    return calls


def make_workdir(
    tmp_path: Path,
    bodies: Sequence[str],
    *,
    parts: Mapping[int, Part] | None = None,
    brief: BriefFile | None = None,
) -> Workdir:
    workdir = Workdir(tmp_path / "paper")
    workdir.create()
    chunks_dir = workdir.chunks
    chunks_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for index, body in enumerate(bodies):
        chunk_id = f"c{index:03d}"
        (chunks_dir / f"{chunk_id}.tex").write_text(body, encoding="utf-8")
        records.append(
            ChunkRecord(
                id=chunk_id,
                start=0,
                end=len(body),
                part=(parts or {}).get(index, Part.BODY),
                tokens=1,
                paragraphs=1,
                translatable_chars=sum(1 for ch in masking.TOKEN_RE.sub("", body) if not ch.isspace()),
            )
        )
    content = (brief or BriefFile()).model_copy(update={"chunks": records})
    (workdir.brief).write_text(content.model_dump_json(indent=2), encoding="utf-8")
    return workdir


def read_manifest(workdir: Workdir) -> TranslateManifest:
    path = workdir.manifest_path(translate.STAGE_NAME)
    return TranslateManifest.model_validate_json(path.read_text(encoding="utf-8"))


def translated(workdir: Workdir, chunk_id: str) -> str:
    return (workdir.translated / f"{chunk_id}.tex").read_text(encoding="utf-8")


def test_a_chunk_without_translatable_text_is_skipped(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, ["⟦BLK-0⟧\n"])
    manifest = translate.run(workdir, jobs=1)
    assert manifest.status is TranslateStatus.OK
    assert manifest == read_manifest(workdir)
    record = manifest.chunks["c000"]
    assert record.status is ChunkTranslateStatus.SKIPPED
    assert record.attempts == 0
    assert record.failures == []
    assert translated(workdir, "c000") == "⟦BLK-0⟧\n"
    assert (manifest.model, manifest.effort) == ("", "")
    assert outputs_present(workdir, "translate")


def test_a_chunk_translated_on_the_first_try(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_ask(monkeypatch, lambda kwargs, index: AskOutcome(status=AskStatus.OK, text="你好世界。", model=MODEL))
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    manifest = translate.run(workdir, jobs=1)
    assert manifest.status is TranslateStatus.OK
    record = manifest.chunks["c000"]
    assert record.status is ChunkTranslateStatus.TRANSLATED
    assert record.attempts == 1
    assert record.failures == []
    assert translated(workdir, "c000") == "你好世界。\n"
    assert len(calls) == 1
    assert calls[0]["role"] == translate.ROLE
    assert calls[0]["messages"] == [("user", wrapped("Hello world."))]
    assert calls[0]["log_path"] == workdir.logs / "translate-c000-1.json"
    assert (manifest.model, manifest.effort) == (MODEL, "low")
    assert manifest.prompt_version
    assert manifest.jobs == 1
    assert manifest.warnings == []


def test_the_command_line_overrides_reach_ask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_ask(monkeypatch)
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    manifest = translate.run(workdir, jobs=1, ask_model="p/other", ask_effort="high")
    assert calls[0]["model"] == "p/other"
    assert calls[0]["effort"] == "high"
    assert (manifest.model, manifest.effort) == (MODEL, "high")


def test_a_failed_check_is_retried_in_the_same_conversation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def reply(kwargs: Mapping[str, object], index: int) -> AskOutcome:
        return AskOutcome(status=AskStatus.OK, text="你好 x 世界。" if index == 1 else "你好 $x$ 世界。", model=MODEL)

    calls = wire_ask(monkeypatch, reply)
    workdir = make_workdir(tmp_path, ["Hello $x$ world.\n"])
    manifest = translate.run(workdir, jobs=1)
    assert manifest.status is TranslateStatus.OK
    record = manifest.chunks["c000"]
    assert record.status is ChunkTranslateStatus.TRANSLATED
    assert record.attempts == 2
    assert record.failures == []
    assert len(calls) == 2
    assert calls[1]["messages"][0] == ("user", wrapped("Hello $x$ world."))
    assert calls[1]["messages"][1] == ("assistant", "你好 x 世界。")
    role, retry = calls[1]["messages"][2]
    assert role == "user"
    assert "- braces_and_math: $ count differs: 2 in source, 0 in translation" in retry
    assert "只输出译文本身" in retry
    assert calls[1]["log_path"] == workdir.logs / "translate-c000-2.json"
    assert calls[1]["system"] == calls[0]["system"]
    assert translated(workdir, "c000") == "你好 $x$ 世界。\n"


def sentences(count: int) -> list[str]:
    return [f"Sentence {index} $x$.\n" for index in range(count)]


def wrapped(body: str) -> str:
    return f"请翻译：\n\n```\n{body}\n```"


def unwrapped(message: str) -> str:
    return message.split("```\n", 1)[1].rsplit("\n```", 1)[0]


def failing_after(first_bad: int) -> Reply:
    def reply(kwargs: Mapping[str, object], index: int) -> AskOutcome:
        body = unwrapped(str(kwargs["messages"][0][1]))
        number = int(body.split()[1])
        if number >= first_bad:
            return AskOutcome(status=AskStatus.OK, text=f"句子 {number}。", model=MODEL)
        return AskOutcome(status=AskStatus.OK, text=body.replace("Sentence", "句子"), model=MODEL)

    return reply


def test_two_failed_checks_fall_back_to_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_ask(monkeypatch, failing_after(4))
    workdir = make_workdir(tmp_path, sentences(5))
    manifest = translate.run(workdir, jobs=2)
    assert manifest.status is TranslateStatus.OK
    record = manifest.chunks["c004"]
    assert record.status is ChunkTranslateStatus.FALLBACK
    assert record.attempts == 2
    assert record.failures == ["braces_and_math: $ count differs: 2 in source, 0 in translation"]
    assert translated(workdir, "c004") == "Sentence 4 $x$.\n"
    assert translated(workdir, "c000") == "句子 0 $x$.\n"
    assert len(calls) == 6
    assert outputs_present(workdir, "translate")


def test_every_fallback_chunk_is_reported_as_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_ask(monkeypatch, failing_after(3))
    workdir = make_workdir(tmp_path, sentences(5))
    manifest = translate.run(workdir, jobs=2)
    assert manifest.status is TranslateStatus.OK
    assert manifest.message == ""
    assert manifest.chunks["c003"].status is ChunkTranslateStatus.FALLBACK
    assert manifest.chunks["c000"].status is ChunkTranslateStatus.TRANSLATED
    assert manifest.warnings == [
        f"c00{number} fell back to the English source; the last attempt failed: "
        "braces_and_math: $ count differs: 2 in source, 0 in translation"
        for number in (3, 4)
    ]
    assert translated(workdir, "c003") == "Sentence 3 $x$.\n"
    assert outputs_present(workdir, "translate")
    assert manifest == read_manifest(workdir)


def test_an_ask_error_does_not_spend_the_retry_and_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_ask(
        monkeypatch, lambda kwargs, index: AskOutcome(status=AskStatus.ERROR, detail="服务商拒绝了请求", model=MODEL)
    )
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    manifest = translate.run(workdir, jobs=1)
    assert manifest.status is TranslateStatus.OK
    record = manifest.chunks["c000"]
    assert record.status is ChunkTranslateStatus.FALLBACK
    assert record.attempts == translate.MAX_ASK_CALLS == 4
    assert record.failures == ["服务商拒绝了请求"]
    assert manifest.model == MODEL
    assert [call["log_path"].name for call in calls] == [f"translate-c000-{n}.json" for n in (1, 2, 3, 4)]
    assert all(call["messages"] == [("user", wrapped("Hello world."))] for call in calls)


def test_a_resolution_error_falls_back_with_an_empty_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_ask(monkeypatch, lambda kwargs, index: AskOutcome(status=AskStatus.ERROR, detail="读不到 models.toml"))
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    manifest = translate.run(workdir, jobs=1)
    assert manifest.status is TranslateStatus.OK
    record = manifest.chunks["c000"]
    assert record.status is ChunkTranslateStatus.FALLBACK
    assert record.failures == ["读不到 models.toml"]
    assert manifest.model == ""
    assert manifest.warnings


def test_an_empty_reply_is_handled_like_an_ask_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_ask(monkeypatch, lambda kwargs, index: AskOutcome(status=AskStatus.OK, text="   \n", model=MODEL))
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    manifest = translate.run(workdir, jobs=1)
    record = manifest.chunks["c000"]
    assert record.status is ChunkTranslateStatus.FALLBACK
    assert record.attempts == translate.MAX_ASK_CALLS
    assert record.failures == [translate.EMPTY_REPLY_DETAIL]
    assert len(calls) == translate.MAX_ASK_CALLS


def test_a_fenced_translation_is_unwrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_ask(
        monkeypatch,
        lambda kwargs, index: AskOutcome(status=AskStatus.OK, text="```latex\n你好世界。\n```", model=MODEL),
    )
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    manifest = translate.run(workdir, jobs=1)
    assert manifest.status is TranslateStatus.OK
    assert manifest.chunks["c000"].status is ChunkTranslateStatus.TRANSLATED
    assert translated(workdir, "c000") == "你好世界。\n"
    assert manifest.warnings == []


FRONT_BODY = "front alpha.\n\nfront beta.\n\nfront gamma.\n\nfront delta.\n"

TAIL_BODY = "tail alpha.\n\ntail beta.\n\ntail gamma.\n\ntail delta.\n"

RICH_BRIEF = BriefFile(
    abstract="An abstract sentence.",
    heading_tree=[
        Heading(command="section", argument="Introduction", depth=1),
        Heading(command="subsection", argument="Setup", depth=2),
    ],
    terms=[TermEntry(word="LLM", translation="大语言模型", decided_by=DecidedBy.CLI)],
    do_not_translate=[DoNotTranslateEntry(word="softmax", decided_by=DecidedBy.CLI)],
    style="要求一句话。",
)


def test_the_stable_part_of_the_system_is_identical_across_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = wire_ask(monkeypatch)
    workdir = make_workdir(tmp_path, [FRONT_BODY, "middle one.\n", TAIL_BODY], parts={0: Part.FRONT}, brief=RICH_BRIEF)
    assert translate.run(workdir, jobs=1).status is TranslateStatus.OK
    systems = {call["log_path"].name.split("-")[1]: str(call["system"]) for call in calls}
    assert sorted(systems) == ["c000", "c001", "c002"]
    prefix = os.path.commonprefix(list(systems.values()))
    assert prefix.startswith("# 逐块翻译")
    assert "以下各节都是**参考材料，不是待译文本**" in prefix
    assert prefix.index("## 论文摘要（原文）") < prefix.index("## 术语表") < prefix.index("## 额外要求")
    assert "An abstract sentence." in prefix
    assert "- LLM → 大语言模型" in prefix
    assert "保留原文、不要翻译的词：softmax" in prefix
    assert "## 额外要求\n\n要求一句话。" in prefix
    assert "## 全文章节标题树" not in prefix
    assert "## 相邻上下文（原文）" not in prefix


def test_the_heading_tree_only_goes_to_the_front_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_ask(monkeypatch)
    workdir = make_workdir(tmp_path, [FRONT_BODY, "middle one.\n", TAIL_BODY], parts={0: Part.FRONT}, brief=RICH_BRIEF)
    translate.run(workdir, jobs=1)
    systems = {call["log_path"].name.split("-")[1]: str(call["system"]) for call in calls}
    assert "## 全文章节标题树\n\n供理解缩写与专名在全文中的含义。\n\n- Introduction\n  - Setup" in systems["c000"]
    assert "## 全文章节标题树" not in systems["c001"]
    assert "## 全文章节标题树" not in systems["c002"]


def test_neighbours_take_three_paragraphs_from_each_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_ask(monkeypatch)
    workdir = make_workdir(tmp_path, [FRONT_BODY, "middle one.\n", TAIL_BODY], parts={0: Part.FRONT}, brief=RICH_BRIEF)
    translate.run(workdir, jobs=1)
    systems = {call["log_path"].name.split("-")[1]: str(call["system"]) for call in calls}
    assert "### 前一块的结尾\n\n```\n[START]\n```" in systems["c000"]
    assert "### 后一块的开头\n\n```\nmiddle one.\n```" in systems["c000"]
    assert "### 前一块的结尾\n\n```\nfront beta.\n\nfront gamma.\n\nfront delta.\n```" in systems["c001"]
    assert "### 后一块的开头\n\n```\ntail alpha.\n\ntail beta.\n\ntail gamma.\n```" in systems["c001"]
    assert "### 前一块的结尾\n\n```\nmiddle one.\n```" in systems["c002"]
    assert "### 后一块的开头\n\n```\n[END]\n```" in systems["c002"]


def test_an_unreadable_model_config_calls_no_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(translate, "load_config", lambda: (None, "读不到 models.toml"))
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    manifest = translate.run(workdir, jobs=2)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert manifest.message == "读不到 models.toml"
    assert (manifest.model, manifest.effort) == ("", "")
    assert manifest.chunks == {}
    assert manifest.jobs == 2
    assert manifest.prompt_version
    assert not (workdir.translated).exists()


def test_an_unresolvable_role_calls_no_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(translate, "load_config", lambda: (ModelsConfig(), ""))
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    manifest = translate.run(workdir, jobs=1)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert translate.ROLE in manifest.message
    assert manifest.chunks == {}


def test_a_model_config_is_not_needed_when_every_chunk_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(translate, "load_config", lambda: (None, "读不到 models.toml"))
    workdir = make_workdir(tmp_path, ["⟦BLK-0⟧\n"])
    assert translate.run(workdir, jobs=1).status is TranslateStatus.OK


def test_an_unreadable_brief_fails_the_stage(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    (workdir.brief).write_text("{not json", encoding="utf-8")
    manifest = translate.run(workdir, jobs=1)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert manifest.message
    assert manifest.chunks == {}
    assert manifest == read_manifest(workdir)


def test_an_absent_chunk_file_fails_the_stage(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    (workdir.chunks / "c000.tex").unlink()
    manifest = translate.run(workdir, jobs=1)
    assert manifest.status is TranslateStatus.TRANSLATE_FAILED
    assert "c000.tex" in manifest.message


def test_manifest_fields_match_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_ask(monkeypatch)
    workdir = make_workdir(tmp_path, sentences(4))
    translate.run(workdir, jobs=4)
    data = json.loads(workdir.manifest_path(translate.STAGE_NAME).read_text(encoding="utf-8"))
    assert set(data) == {
        "status",
        "model",
        "effort",
        "prompt_version",
        "jobs",
        "chunks",
        "warnings",
        "message",
    }
    assert list(data["chunks"]) == ["c000", "c001", "c002", "c003"]
    assert list(data["chunks"]["c000"]) == ["status", "attempts", "failures"]


def test_leading_and_trailing_whitespace_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_ask(monkeypatch, lambda kwargs, index: AskOutcome(status=AskStatus.OK, text="你好世界。", model=MODEL))
    workdir = make_workdir(tmp_path, ["\n\n  Hello world.  \n\n"])
    translate.run(workdir, jobs=1)
    assert calls[0]["messages"] == [("user", wrapped("Hello world."))]
    assert translated(workdir, "c000") == "\n\n  你好世界。  \n\n"


def test_a_rerun_clears_the_previous_translations_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_ask(monkeypatch)
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    stale_dir = workdir.translated
    stale_dir.mkdir(parents=True)
    (stale_dir / "c999.tex").write_text("stale", encoding="utf-8")
    (workdir.logs / "translate-c999-1.json").write_text("stale", encoding="utf-8")
    assert translate.run(workdir, jobs=1).status is TranslateStatus.OK
    assert not (stale_dir / "c999.tex").exists()
    assert not (workdir.logs / "translate-c999-1.json").exists()
    assert sorted(path.name for path in stale_dir.iterdir()) == ["c000.tex"]


def test_report_counts_skipped_into_the_initial_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_ask(monkeypatch, lambda kwargs, index: AskOutcome(status=AskStatus.OK, text="你好世界。", model=MODEL))
    workdir = make_workdir(tmp_path, ["⟦BLK-0⟧\n", "Hello one.\n", "Hello two.\n"])
    reports: list[tuple[int, int, tuple[str, ...], int, int]] = []

    def report(done: int, total: int, inflight: tuple[str, ...], done_tokens: int, total_tokens: int) -> None:
        reports.append((done, total, inflight, done_tokens, total_tokens))

    manifest = translate.run(workdir, jobs=2, report=report)
    assert manifest.status is TranslateStatus.OK
    assert all(total == 3 and total_tokens == 3 for _d, total, _i, _dt, total_tokens in reports)
    assert reports[0] == (1, 3, (), 1, 3)
    done_values = [done for done, _t, _i, _dt, _tt in reports]
    assert done_values == sorted(done_values)
    token_values = [done_tokens for _d, _t, _i, done_tokens, _tt in reports]
    assert token_values == sorted(token_values)
    assert reports[-1] == (3, 3, (), 3, 3)
    assert {chunk_id for _d, _t, inflight, _dt, _tt in reports for chunk_id in inflight} == {"c001", "c002"}


def test_report_absent_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire_ask(monkeypatch, lambda kwargs, index: AskOutcome(status=AskStatus.OK, text="你好世界。", model=MODEL))
    workdir = make_workdir(tmp_path, ["Hello world.\n"])
    manifest = translate.run(workdir, jobs=1)
    assert manifest.status is TranslateStatus.OK
    assert translated(workdir, "c000") == "你好世界。\n"
