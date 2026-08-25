from __future__ import annotations

import json
from pathlib import Path

import pytest

from tongtu.artifacts.mask import BlocksFile, CaptionRecord
from tongtu.artifacts.survey import BriefFile, DecidedBy, Part, SurveyManifest, SurveyStatus
from tongtu.masking import CaptionKind
from tongtu.model.ask import AskOutcome, AskStatus
from tongtu.model.config import ModelsConfig, RoleConfig
from tongtu.pipeline import outputs_present
from tongtu.stages import mask, survey
from tongtu.workdir import Workdir

from ...conftest import FIXTURE_PAPERS, paper_dir

SAMPLE = """\
\\begin{abstract}
We study LLM scaling with attention heads.
\\end{abstract}

\\maketitle

\\section{Introduction}
\\label{sec:intro}

Large Language Models are everywhere.  The Transformer uses softmax.

\\subsection{Setup}

We serve with vLLM.

\\section{Method}

Our method is simple.

\\appendix

\\section{Details}

Extra details here.
"""

BULK = "lorem ipsum dolor sit amet consectetur " * 1200


def make_workdir(tmp_path: Path, masked: str | None = SAMPLE, blocks: BlocksFile | None = None) -> Workdir:
    workdir = Workdir(tmp_path / "paper")
    workdir.create()
    if masked is not None:
        (workdir.build / survey.MASKED_FILENAME).write_text(masked, encoding="utf-8")
    (workdir.build / survey.BLOCKS_FILENAME).write_text((blocks or BlocksFile()).model_dump_json(), encoding="utf-8")
    return workdir


def read_manifest(workdir: Workdir) -> SurveyManifest:
    return SurveyManifest.model_validate_json(workdir.manifest_path(survey.STAGE_NAME).read_text(encoding="utf-8"))


def read_brief(workdir: Workdir) -> BriefFile:
    return BriefFile.model_validate_json((workdir.build / survey.BRIEF_FILENAME).read_text(encoding="utf-8"))


def write_glossary(path: Path, content: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False) if not isinstance(content, str) else content, "utf-8")
    return path


def role_config() -> ModelsConfig:
    return ModelsConfig(roles={survey.ROLE: RoleConfig(model="m", effort="low", provider="p")})


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(survey, "load_config", lambda: (ModelsConfig(), ""))
    monkeypatch.setattr(survey, "ask", _forbidden_ask)


def _forbidden_ask(**kwargs: object) -> AskOutcome:
    raise AssertionError("本用例不应调用模型")


def test_run_ok_writes_outputs_and_manifest(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK
    assert manifest == read_manifest(workdir)
    assert outputs_present(workdir, "survey")
    brief = read_brief(workdir)
    assert manifest.chunks_total == len(brief.chunks) == 3
    assert [record.part for record in brief.chunks] == [Part.FRONT, Part.BODY, Part.APPENDIX]
    assert [record.id for record in brief.chunks] == ["c000", "c001", "c002"]
    assert brief.abstract == "We study LLM scaling with attention heads."
    assert manifest.warnings == []


def test_chunk_files_concatenate_back_to_masked(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    survey.run(workdir)
    brief = read_brief(workdir)
    bodies = [
        (workdir.build / survey.CHUNKS_DIRNAME / f"{record.id}.tex").read_text(encoding="utf-8")
        for record in brief.chunks
    ]
    assert "".join(bodies) == SAMPLE
    for record, body in zip(brief.chunks, bodies, strict=True):
        assert body == SAMPLE[record.start : record.end]
        assert record.paragraphs >= 1
        assert record.tokens > 0
    assert sorted(path.name for path in (workdir.build / survey.CHUNKS_DIRNAME).iterdir()) == [
        f"{record.id}.tex" for record in brief.chunks
    ]


def test_manifest_field_order_matches_card(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    manifest = survey.run(workdir)
    assert list(manifest.model_dump()) == [
        "status",
        "chunks_total",
        "transparent_environments",
        "terms_total",
        "do_not_translate_total",
        "filtered",
        "warnings",
        "message",
    ]


def test_brief_field_order_matches_card(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    survey.run(workdir)
    brief = json.loads((workdir.build / survey.BRIEF_FILENAME).read_text(encoding="utf-8"))
    assert list(brief) == ["abstract", "heading_tree", "terms", "do_not_translate", "style", "chunks"]
    assert list(brief["chunks"][0]) == [
        "id",
        "start",
        "end",
        "part",
        "tokens",
        "paragraphs",
        "headings",
        "translatable_chars",
    ]
    assert list(brief["heading_tree"][0]) == ["command", "argument", "depth"]


def test_heading_tree_depth_is_relative_to_the_shallowest_command(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    survey.run(workdir)
    tree = read_brief(workdir).heading_tree
    assert [(heading.command, heading.argument, heading.depth) for heading in tree] == [
        ("section", "Introduction", 1),
        ("subsection", "Setup", 2),
        ("section", "Method", 1),
        ("section", "Details", 1),
    ]


def test_chunk_headings_follow_the_document_tree(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    survey.run(workdir)
    chunks = read_brief(workdir).chunks
    assert [heading.argument for heading in chunks[0].headings] == []
    assert [heading.argument for heading in chunks[1].headings] == ["Introduction", "Setup", "Method"]
    assert [heading.depth for heading in chunks[1].headings] == [1, 2, 1]
    assert [heading.argument for heading in chunks[2].headings] == ["Details"]


def test_document_without_any_heading_falls_back_to_one_body(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, "just a paragraph\n\nand another one\n")
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK
    brief = read_brief(workdir)
    assert brief.heading_tree == []
    assert [record.part for record in brief.chunks] == [Part.BODY]


def test_wrapping_environment_with_arguments_is_transparent(tmp_path: Path) -> None:
    masked = (
        "\\begin{multicols}{2}\n\n\\section{One}\n\ntext one here\n\n"
        "\\section{Two}\n\ntext two here\n\n\\end{multicols}\n"
    )
    workdir = make_workdir(tmp_path, masked)
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK
    assert manifest.transparent_environments == ["multicols"]
    assert [heading.argument for heading in read_brief(workdir).heading_tree] == ["One", "Two"]


def test_units_over_split_above_are_subdivided(tmp_path: Path) -> None:
    masked = f"\\section{{One}}\n\n\\subsection{{A}}\n\n{BULK}\n\n\\subsection{{B}}\n\n{BULK}\n"
    workdir = make_workdir(tmp_path, masked)
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK
    assert manifest.chunks_total > 1
    assert (
        "".join(
            (workdir.build / survey.CHUNKS_DIRNAME / f"{record.id}.tex").read_text(encoding="utf-8")
            for record in read_brief(workdir).chunks
        )
        == masked
    )


def test_a_single_paragraph_over_split_above_gets_a_warning(tmp_path: Path) -> None:
    masked = f"\\section{{One}}\n{BULK}\n"
    workdir = make_workdir(tmp_path, masked)
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK
    assert manifest.chunks_total == 1
    assert any(str(survey.SPLIT_ABOVE) in warning for warning in manifest.warnings)


def test_small_chunks_do_not_merge_across_parts(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    survey.run(workdir)
    chunks = read_brief(workdir).chunks
    assert len({record.part for record in chunks}) == 3
    assert all(record.tokens < survey.MERGE_BELOW for record in chunks)


ABSTRACT_BLOCKS = BlocksFile(
    captions=[CaptionRecord(id="CAP-1", block_id="", kind=CaptionKind.ABSTRACT, tex="  前导区摘要  ", masked_text="")]
)


def test_abstract_comes_from_the_blocks_slot_first(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, SAMPLE, ABSTRACT_BLOCKS)
    survey.run(workdir)
    assert read_brief(workdir).abstract == "前导区摘要"


def test_abstract_falls_back_to_the_masked_environment(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    survey.run(workdir)
    assert read_brief(workdir).abstract == "We study LLM scaling with attention heads."


def test_abstract_absent_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, "\\section{One}\n\nplain text only\n")
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK
    assert read_brief(workdir).abstract is None
    assert any("abstract not found" in warning for warning in manifest.warnings)


CHUNK_FAILURES = {
    "masked_missing": None,
    "unbalanced_environment": "\\section{One}\n\n\\begin{quote}\n\nunclosed\n",
    "empty_document": "",
}


@pytest.mark.parametrize("case", sorted(CHUNK_FAILURES))
def test_chunk_failed_leaves_no_outputs(tmp_path: Path, case: str) -> None:
    workdir = make_workdir(tmp_path, CHUNK_FAILURES[case])
    (workdir.build / survey.BRIEF_FILENAME).write_text("stale", encoding="utf-8")
    (workdir.build / survey.CHUNKS_DIRNAME).mkdir()
    (workdir.build / survey.CHUNKS_DIRNAME / "c000.tex").write_text("stale", encoding="utf-8")
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.CHUNK_FAILED
    assert manifest.message
    assert manifest == read_manifest(workdir)
    assert not (workdir.build / survey.BRIEF_FILENAME).exists()
    assert not (workdir.build / survey.CHUNKS_DIRNAME).exists()
    assert not outputs_present(workdir, "survey")


def test_unreadable_blocks_file_is_chunk_failed(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    (workdir.build / survey.BLOCKS_FILENAME).write_text("{not json", encoding="utf-8")
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.CHUNK_FAILED
    assert not outputs_present(workdir, "survey")


GLOSSARY_FAILURES = {
    "not_json": "{not json",
    "not_an_object": "[]",
    "unknown_field": {"terms": {}, "trms": {}},
    "same_word_in_both_sections": {"terms": {"LLM": "大语言模型"}, "do_not_translate": ["llm"]},
    "empty_translation": {"terms": {"LLM": "  "}},
    "empty_word": {"terms": {"  ": "大语言模型"}},
    "terms_not_an_object": {"terms": []},
    "do_not_translate_not_a_list": {"do_not_translate": "Transformer"},
    "style_not_a_string": {"style": 3},
}


@pytest.mark.parametrize("case", sorted(GLOSSARY_FAILURES))
def test_glossary_invalid_leaves_no_outputs(tmp_path: Path, case: str) -> None:
    workdir = make_workdir(tmp_path)
    path = write_glossary(tmp_path / "cli.json", GLOSSARY_FAILURES[case])
    manifest = survey.run(workdir, glossary=(path,))
    assert manifest.status is SurveyStatus.GLOSSARY_INVALID
    assert str(path) in manifest.message
    assert manifest == read_manifest(workdir)
    assert not (workdir.build / survey.BRIEF_FILENAME).exists()
    assert not (workdir.build / survey.CHUNKS_DIRNAME).exists()


def test_a_missing_command_line_glossary_is_a_user_error(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    manifest = survey.run(workdir, glossary=(tmp_path / "absent.json",))
    assert manifest.status is SurveyStatus.GLOSSARY_INVALID
    assert "absent.json" in manifest.message
    assert not outputs_present(workdir, "survey")


def test_absent_global_and_paper_layers_are_skipped(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    assert survey.run(workdir).status is SurveyStatus.OK


def test_four_layers_override_by_word(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(survey, "load_config", lambda: (role_config(), ""))
    monkeypatch.setattr(
        survey,
        "ask",
        lambda **kwargs: AskOutcome(
            status=AskStatus.OK,
            text=json.dumps(
                {
                    "terms": [
                        {"word": "LLM", "translation": "模型提议"},
                        {"word": "Transformer", "translation": "变换器"},
                        {"word": "softmax", "translation": "软最大"},
                        {"word": "attention head", "translation": "注意头"},
                    ],
                    "do_not_translate": [],
                }
            ),
        ),
    )
    write_glossary(
        Path(str(tmp_path / "config")) / "tongtu" / survey.GLOSSARY_FILENAME,
        {"terms": {"LLM": "全局译法", "Transformer": "全局变换器", "softmax": "全局软最大"}, "style": "全局风格"},
    )
    write_glossary(
        workdir_glossary := (tmp_path / "paper" / survey.GLOSSARY_FILENAME),
        {"terms": {"LLM": "论文译法", "Transformer": "论文变换器"}, "style": "论文风格"},
    )
    assert workdir_glossary.is_file()
    cli_path = write_glossary(tmp_path / "cli.json", {"terms": {"LLM": "命令行译法"}, "style": "命令行风格"})
    workdir = make_workdir(tmp_path)
    manifest = survey.run(workdir, glossary=(cli_path,))
    assert manifest.status is SurveyStatus.OK
    brief = read_brief(workdir)
    decided = {entry.word: (entry.translation, entry.decided_by) for entry in brief.terms}
    assert decided["LLM"] == ("命令行译法", DecidedBy.CLI)
    assert decided["Transformer"] == ("论文变换器", DecidedBy.PAPER)
    assert decided["softmax"] == ("全局软最大", DecidedBy.GLOBAL)
    assert decided["attention head"] == ("注意头", DecidedBy.SURVEY)
    assert brief.style == "命令行风格"
    assert manifest.terms_total == len(brief.terms) == 4


def test_do_not_translate_overrides_across_sections(tmp_path: Path) -> None:
    write_glossary(
        Path(str(tmp_path / "config")) / "tongtu" / survey.GLOSSARY_FILENAME, {"terms": {"LLM": "大语言模型"}}
    )
    cli_path = write_glossary(tmp_path / "cli.json", {"do_not_translate": ["llm"]})
    workdir = make_workdir(tmp_path)
    survey.run(workdir, glossary=(cli_path,))
    brief = read_brief(workdir)
    assert brief.terms == []
    assert [(entry.word, entry.decided_by) for entry in brief.do_not_translate] == [("llm", DecidedBy.CLI)]


def test_blank_style_at_the_highest_layer_clears_it(tmp_path: Path) -> None:
    write_glossary(Path(str(tmp_path / "config")) / "tongtu" / survey.GLOSSARY_FILENAME, {"style": "全局风格"})
    cli_path = write_glossary(tmp_path / "cli.json", {"style": "   "})
    workdir = make_workdir(tmp_path)
    survey.run(workdir, glossary=(cli_path,))
    assert read_brief(workdir).style is None


def test_style_absent_everywhere_is_null(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    survey.run(workdir)
    assert read_brief(workdir).style is None


HIT_CASES = [
    ("LLM", "We compare LLM outputs today.\n", True),
    ("LLM", "We compare LLMs outputs today.\n", True),
    ("LLM", "We serve models with vLLM today.\n", False),
    ("LLM", "We compare llm outputs today.\n", False),
    ("large language model", "Large Language Models are common.\n", True),
    ("large language model", "We study large language\nmodel behavior here.\n", True),
    ("box", "Several boxes appear in figures.\n", True),
    ("attention head", "Nothing relevant appears here.\n", False),
]


@pytest.mark.parametrize(("word", "masked", "hit"), HIT_CASES, ids=range(len(HIT_CASES)))
def test_hit_rule_decides_brief_or_filtered(tmp_path: Path, word: str, masked: str, hit: bool) -> None:
    cli_path = write_glossary(tmp_path / "cli.json", {"terms": {word: "译法"}})
    workdir = make_workdir(tmp_path, masked)
    manifest = survey.run(workdir, glossary=(cli_path,))
    assert manifest.status is SurveyStatus.OK
    brief = read_brief(workdir)
    assert [entry.word for entry in brief.terms] == ([word] if hit else [])
    assert [(entry.word, entry.decided_by) for entry in manifest.filtered] == ([] if hit else [(word, DecidedBy.CLI)])
    assert manifest.terms_total == len(brief.terms)


def test_filtered_counts_do_not_translate_too(tmp_path: Path) -> None:
    cli_path = write_glossary(tmp_path / "cli.json", {"do_not_translate": ["Transformer", "Mamba"]})
    workdir = make_workdir(tmp_path)
    manifest = survey.run(workdir, glossary=(cli_path,))
    assert manifest.do_not_translate_total == 1
    assert [entry.word for entry in manifest.filtered] == ["Mamba"]


def test_model_proposal_lands_in_brief_with_survey_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_ask(**kwargs: object) -> AskOutcome:
        seen.update(kwargs)
        return AskOutcome(
            status=AskStatus.OK,
            text=json.dumps(
                {"terms": [{"word": "attention head", "translation": "注意力头"}], "do_not_translate": ["softmax"]}
            ),
        )

    monkeypatch.setattr(survey, "load_config", lambda: (role_config(), ""))
    monkeypatch.setattr(survey, "ask", fake_ask)
    workdir = make_workdir(tmp_path)
    manifest = survey.run(workdir, ask_model="p/m", ask_effort="high")
    assert manifest.status is SurveyStatus.OK
    assert manifest.warnings == []
    brief = read_brief(workdir)
    assert [(entry.word, entry.translation, entry.decided_by) for entry in brief.terms] == [
        ("attention head", "注意力头", DecidedBy.SURVEY)
    ]
    assert [(entry.word, entry.decided_by) for entry in brief.do_not_translate] == [("softmax", DecidedBy.SURVEY)]
    assert seen["role"] == survey.ROLE
    assert seen["log_path"] == workdir.logs / survey.TERMS_LOG_FILENAME
    assert seen["model"] == "p/m"
    assert seen["effort"] == "high"
    assert seen["schema"] == survey.TERMS_SCHEMA
    assert SAMPLE in seen["messages"][0][1]


def test_model_error_degrades_to_an_empty_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(survey, "load_config", lambda: (role_config(), ""))
    monkeypatch.setattr(survey, "ask", lambda **kwargs: AskOutcome(status=AskStatus.ERROR, detail="服务商拒绝了请求"))
    workdir = make_workdir(tmp_path)
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK
    assert any("服务商拒绝了请求" in warning for warning in manifest.warnings)
    assert read_brief(workdir).terms == []


def test_a_reply_off_schema_degrades_to_an_empty_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(survey, "load_config", lambda: (role_config(), ""))
    monkeypatch.setattr(survey, "ask", lambda **kwargs: AskOutcome(status=AskStatus.OK, text="not json at all"))
    workdir = make_workdir(tmp_path)
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK
    assert any("does not match the schema" in warning for warning in manifest.warnings)
    assert read_brief(workdir).terms == []


def test_unreadable_model_config_degrades_to_an_empty_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(survey, "load_config", lambda: (None, "读不到 models.toml"))
    workdir = make_workdir(tmp_path)
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK
    assert any("读不到 models.toml" in warning for warning in manifest.warnings)


def test_no_terms_skips_the_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(survey, "load_config", lambda: (role_config(), ""))
    workdir = make_workdir(tmp_path)
    workdir.logs.mkdir(parents=True, exist_ok=True)
    (workdir.logs / survey.TERMS_LOG_FILENAME).write_text("stale", encoding="utf-8")
    manifest = survey.run(workdir, no_terms=True)
    assert manifest.status is SurveyStatus.OK
    assert manifest.warnings == []
    assert not (workdir.logs / survey.TERMS_LOG_FILENAME).exists()


def test_an_unconfigured_role_skips_the_model_without_a_warning(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK
    assert manifest.warnings == []


@pytest.mark.parametrize("paper", FIXTURE_PAPERS)
def test_fixture_papers_survey_after_mask(tmp_path: Path, paper: str) -> None:
    workdir = Workdir(tmp_path / paper)
    workdir.create()
    (workdir.build / mask.PRECOMPILE_FILENAME).write_text(
        (paper_dir(paper) / "main.tex").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert mask.run(workdir).status.value == "ok"
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK, manifest.message
    masked = (workdir.build / survey.MASKED_FILENAME).read_text(encoding="utf-8")
    brief = read_brief(workdir)
    assert manifest.chunks_total == len(brief.chunks) >= 1
    assert (
        "".join(
            (workdir.build / survey.CHUNKS_DIRNAME / f"{record.id}.tex").read_text(encoding="utf-8")
            for record in brief.chunks
        )
        == masked
    )


COMPOUND = """\
\\section{Method}

We use mixed RL training and plain RL for the LLM and several LLMs.
"""


def proposal(monkeypatch: pytest.MonkeyPatch, terms: dict[str, str], do_not_translate: list[str]) -> None:
    monkeypatch.setattr(survey, "load_config", lambda: (role_config(), ""))
    monkeypatch.setattr(
        survey,
        "ask",
        lambda **kwargs: AskOutcome(
            status=AskStatus.OK,
            text=json.dumps(
                {
                    "terms": [{"word": word, "translation": value} for word, value in terms.items()],
                    "do_not_translate": do_not_translate,
                }
            ),
        ),
    )


def term_warnings(manifest: SurveyManifest) -> list[str]:
    return [line for line in manifest.warnings if line.startswith("term")]


def test_a_plural_and_its_singular_from_the_user_are_both_kept(tmp_path: Path) -> None:
    cli_path = write_glossary(tmp_path / "cli.json", {"do_not_translate": ["LLM"], "terms": {"LLMs": "大语言模型"}})
    workdir = make_workdir(tmp_path, COMPOUND)
    manifest = survey.run(workdir, glossary=(cli_path,))
    brief = read_brief(workdir)
    assert [entry.word for entry in brief.do_not_translate] == ["LLM"]
    assert [entry.word for entry in brief.terms] == ["LLMs"]
    assert len(term_warnings(manifest)) == 1
    assert all(part in term_warnings(manifest)[0] for part in ("'LLM'", "'LLMs'", "cli", "both are kept"))


def test_a_plural_and_its_singular_from_the_model_keep_do_not_translate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal(monkeypatch, {"LLMs": "大语言模型"}, ["LLM"])
    workdir = make_workdir(tmp_path, COMPOUND)
    manifest = survey.run(workdir)
    brief = read_brief(workdir)
    assert [entry.word for entry in brief.do_not_translate] == ["LLM"]
    assert brief.terms == []
    assert len(term_warnings(manifest)) == 1
    assert all(part in term_warnings(manifest)[0] for part in ("'LLM'", "'LLMs'", "survey", "dropping"))


def test_a_higher_layer_overrides_a_lower_layer_plural(tmp_path: Path) -> None:
    write_glossary(
        Path(str(tmp_path / "config")) / "tongtu" / survey.GLOSSARY_FILENAME, {"terms": {"LLMs": "全局译法"}}
    )
    cli_path = write_glossary(tmp_path / "cli.json", {"terms": {"LLM": "命令行译法"}})
    workdir = make_workdir(tmp_path, COMPOUND)
    manifest = survey.run(workdir, glossary=(cli_path,))
    brief = read_brief(workdir)
    assert [(entry.word, entry.translation, entry.decided_by) for entry in brief.terms] == [
        ("LLM", "命令行译法", DecidedBy.CLI)
    ]
    assert term_warnings(manifest) == []


def test_a_compound_translation_keeping_the_untranslated_word_survives(tmp_path: Path) -> None:
    cli_path = write_glossary(
        tmp_path / "cli.json", {"do_not_translate": ["RL"], "terms": {"mixed RL training": "混合 RL 训练"}}
    )
    workdir = make_workdir(tmp_path, COMPOUND)
    manifest = survey.run(workdir, glossary=(cli_path,))
    brief = read_brief(workdir)
    assert [entry.word for entry in brief.terms] == ["mixed RL training"]
    assert [entry.word for entry in brief.do_not_translate] == ["RL"]
    assert term_warnings(manifest) == []


def test_two_user_layers_in_conflict_keep_both_entries(tmp_path: Path) -> None:
    write_glossary(Path(str(tmp_path / "config")) / "tongtu" / survey.GLOSSARY_FILENAME, {"do_not_translate": ["RL"]})
    cli_path = write_glossary(tmp_path / "cli.json", {"terms": {"mixed RL training": "混合强化学习训练"}})
    workdir = make_workdir(tmp_path, COMPOUND)
    manifest = survey.run(workdir, glossary=(cli_path,))
    brief = read_brief(workdir)
    assert [entry.word for entry in brief.terms] == ["mixed RL training"]
    assert [entry.word for entry in brief.do_not_translate] == ["RL"]
    assert len(term_warnings(manifest)) == 1
    assert all(part in term_warnings(manifest)[0] for part in ("'RL'", "global", "cli", "both are kept"))


def test_a_user_compound_term_drops_the_proposed_word(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proposal(monkeypatch, {}, ["RL"])
    cli_path = write_glossary(tmp_path / "cli.json", {"terms": {"mixed RL training": "混合强化学习训练"}})
    workdir = make_workdir(tmp_path, COMPOUND)
    manifest = survey.run(workdir, glossary=(cli_path,))
    brief = read_brief(workdir)
    assert [(entry.word, entry.decided_by) for entry in brief.terms] == [("mixed RL training", DecidedBy.CLI)]
    assert brief.do_not_translate == []
    assert len(term_warnings(manifest)) == 1
    assert "dropping this do_not_translate word" in term_warnings(manifest)[0]


def test_a_user_word_drops_the_proposed_compound_term(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proposal(monkeypatch, {"mixed RL training": "混合强化学习训练"}, [])
    cli_path = write_glossary(tmp_path / "cli.json", {"do_not_translate": ["RL"]})
    workdir = make_workdir(tmp_path, COMPOUND)
    manifest = survey.run(workdir, glossary=(cli_path,))
    brief = read_brief(workdir)
    assert brief.terms == []
    assert [(entry.word, entry.decided_by) for entry in brief.do_not_translate] == [("RL", DecidedBy.CLI)]
    assert len(term_warnings(manifest)) == 1
    assert "dropping this term" in term_warnings(manifest)[0]


def test_two_proposed_entries_in_conflict_keep_the_untranslated_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal(monkeypatch, {"mixed RL training": "混合强化学习训练"}, ["RL"])
    workdir = make_workdir(tmp_path, COMPOUND)
    manifest = survey.run(workdir)
    brief = read_brief(workdir)
    assert brief.terms == []
    assert [(entry.word, entry.decided_by) for entry in brief.do_not_translate] == [("RL", DecidedBy.SURVEY)]
    assert len(term_warnings(manifest)) == 1
    assert "dropping this term" in term_warnings(manifest)[0]


def test_a_proposed_translation_gets_spaces_around_latin_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proposal(monkeypatch, {"mixed RL training": "混合RL训练", "LLM": "LLM 模型"}, [])
    workdir = make_workdir(tmp_path, COMPOUND)
    survey.run(workdir)
    brief = read_brief(workdir)
    assert [(entry.word, entry.translation) for entry in brief.terms] == [
        ("LLM", "LLM 模型"),
        ("mixed RL training", "混合 RL 训练"),
    ]


def test_a_user_translation_keeps_its_spacing(tmp_path: Path) -> None:
    cli_path = write_glossary(tmp_path / "cli.json", {"terms": {"mixed RL training": "混合RL训练"}})
    workdir = make_workdir(tmp_path, COMPOUND)
    survey.run(workdir, glossary=(cli_path,))
    brief = read_brief(workdir)
    assert [(entry.word, entry.translation) for entry in brief.terms] == [("mixed RL training", "混合RL训练")]
