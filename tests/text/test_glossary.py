from __future__ import annotations

import json
from pathlib import Path

import pytest

from tongtu import glossary
from tongtu.artifacts.translate import PaperGlossary, PaperKeptWord, PaperTerm
from tongtu.validation import Glossary

MASKED = """\
\\section{Experimental Setup}
Large language models (LLMs) are trained with mixed RL training on long contexts.

We serve the model with vLLM and use the KV cache for decoding~\\cite{tokenizer}.
"""


def write(path: Path, content: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def paper(tmp_path: Path) -> Path:
    path = tmp_path / "paper"
    path.mkdir()
    return path


def env(home: Path) -> dict[str, str]:
    return {"TONGTU_HOME": str(home)}


def selection(home: Path, paper: Path, *cli: Path) -> glossary.Selection:
    return glossary.load_selection(MASKED, paper, cli, env(home))


def test_a_glossary_file_accepts_the_three_optional_fields(home: Path, paper: Path) -> None:
    write(home / "glossary.json", {"terms": {"KV cache": "键值缓存"}, "do_not_translate": ["LLM"], "style": "简洁"})
    layers = glossary.read_layers(paper, (), env(home))
    assert layers.learned is None
    assert [layer.glossary for layer in layers.user] == [
        Glossary(terms=(("KV cache", "键值缓存"),), do_not_translate=("LLM",), style="简洁")
    ]
    write(home / "glossary.json", {})
    assert glossary.read_layers(paper, (), env(home)).user[0].glossary == Glossary()


@pytest.mark.parametrize(
    "content",
    [
        {"terms": {"KV cache": "键值缓存"}, "notes": "x"},
        {"terms": {" ": "空"}},
        {"terms": {"KV cache": " "}},
        {"do_not_translate": [""]},
        {"terms": {"LLM": "大模型"}, "do_not_translate": ["LLMs"]},
        {"terms": {"long context": "长上下文", "Long-Contexts": "长语境"}},
        ["LLM"],
    ],
)
def test_a_malformed_glossary_file_raises_with_its_path(home: Path, paper: Path, content: object) -> None:
    path = write(paper / "glossary.json", content)
    with pytest.raises(glossary.GlossaryError, match=str(path)):
        glossary.read_layers(paper, (), env(home))


def test_duplicate_json_keys_with_different_translations_raise(home: Path, paper: Path) -> None:
    path = paper / "glossary.json"
    path.write_text('{"terms": {"LLM": "大模型", "LLM": "大语言模型"}}', encoding="utf-8")
    with pytest.raises(glossary.GlossaryError, match=str(path)):
        glossary.read_layers(paper, (), env(home))


def test_a_missing_command_line_glossary_is_an_error(home: Path, paper: Path, tmp_path: Path) -> None:
    with pytest.raises(glossary.GlossaryError, match="absent.json"):
        glossary.read_layers(paper, (tmp_path / "absent.json",), env(home))


def test_layers_are_read_in_order_and_absent_ones_are_skipped(home: Path, paper: Path, tmp_path: Path) -> None:
    learned = write(home / "glossary.learned.json", {"terms": {"KV cache": "KV 缓存"}})
    paper_file = write(paper / "glossary.json", {"terms": {"KV cache": "论文级"}})
    first = write(tmp_path / "first.json", {"terms": {"KV cache": "第一"}})
    second = write(tmp_path / "second.json", {"terms": {"KV cache": "第二"}})
    layers = glossary.read_layers(paper, (first, second), env(home))
    assert layers.learned is not None and layers.learned.path == learned
    assert [layer.path for layer in layers.user] == [paper_file, first, second]


def test_the_highest_layer_wins_for_the_same_normalized_word(home: Path, paper: Path, tmp_path: Path) -> None:
    write(home / "glossary.json", {"terms": {"long context": "全局译法", "KV cache": "键值缓存"}, "style": "全局风格"})
    write(paper / "glossary.json", {"terms": {"Long-Contexts": "论文级译法"}, "style": "论文级风格"})
    cli = write(tmp_path / "cli.json", {"do_not_translate": ["long contexts"]})
    chosen = selection(home, paper, cli)
    assert chosen.fixed.terms == (("KV cache", "键值缓存"),)
    assert chosen.fixed.do_not_translate == ("long contexts",)
    assert chosen.fixed.style == "论文级风格"


def test_a_lower_layer_style_survives_when_higher_layers_write_none(home: Path, paper: Path) -> None:
    write(home / "glossary.json", {"style": "全局风格"})
    write(paper / "glossary.json", {"terms": {"KV cache": "键值缓存"}})
    assert selection(home, paper).fixed.style == "全局风格"


def test_only_entries_found_in_the_paper_are_selected(home: Path, paper: Path) -> None:
    write(
        home / "glossary.json",
        {
            "terms": {"large language model": "大语言模型", "tokenizer": "分词器", "Experimental Setup": "实验设置"},
            "do_not_translate": ["LLM", "vLLM", "transformer"],
        },
    )
    chosen = selection(home, paper)
    assert chosen.fixed.terms == (("large language model", "大语言模型"),)
    assert chosen.fixed.do_not_translate == ("LLM", "vLLM")
    assert [warning.split(": ", 1)[1] for warning in chosen.unmatched] == [
        "'transformer' does not occur in this paper",
        "'tokenizer' does not occur in this paper",
        "'Experimental Setup' does not occur in this paper",
    ]
    assert all(warning.startswith(str(home / "glossary.json")) for warning in chosen.unmatched)


def test_learned_entries_go_to_the_session_glossary_unless_a_user_layer_covers_them(home: Path, paper: Path) -> None:
    write(
        home / "glossary.learned.json",
        {"terms": {"KV cache": "KV 缓存", "long context": "长上下文", "pruning": "剪枝"}, "do_not_translate": ["LLM"]},
    )
    write(paper / "glossary.json", {"terms": {"long contexts": "长语境"}})
    chosen = selection(home, paper)
    assert chosen.fixed.terms == (("long contexts", "长语境"),)
    assert chosen.learned == Glossary(terms=(("KV cache", "KV 缓存"),), do_not_translate=("LLM",))
    assert chosen.unmatched == ()
    assert (chosen.user_count, chosen.learned_count) == (1, 2)


def test_session_files_are_written_in_the_glossary_format(home: Path, paper: Path, tmp_path: Path) -> None:
    write(home / "glossary.learned.json", {"terms": {"KV cache": "KV 缓存"}, "style": "已学风格"})
    write(home / "glossary.json", {"do_not_translate": ["LLM"], "style": "全局风格"})
    session = tmp_path / "session"
    glossary.write_session_files(session, selection(home, paper))
    fixed = json.loads((session / "glossary.fixed.json").read_text(encoding="utf-8"))
    assert fixed == {"terms": {}, "do_not_translate": ["LLM"], "style": "全局风格"}
    initial = json.loads((session / "glossary.json").read_text(encoding="utf-8"))
    assert initial == {"terms": {"KV cache": "KV 缓存"}, "do_not_translate": []}


def test_a_fixed_term_that_drops_a_kept_word_is_invalid(home: Path, paper: Path) -> None:
    write(home / "glossary.json", {"do_not_translate": ["RL"]})
    cli = write(paper / "cli.json", {"terms": {"mixed RL training": "混合强化学习训练"}})
    with pytest.raises(glossary.GlossaryError) as raised:
        selection(home, paper, cli)
    message = str(raised.value)
    assert "'mixed RL training'" in message and "'RL'" in message
    assert str(cli) in message and str(home / "glossary.json") in message


def test_a_fixed_term_that_keeps_the_kept_word_is_valid(home: Path, paper: Path) -> None:
    write(home / "glossary.json", {"do_not_translate": ["RL"], "terms": {"mixed RL training": "混合 RL 训练"}})
    assert selection(home, paper).fixed.terms == (("mixed RL training", "混合 RL 训练"),)


def test_validation_uses_the_session_glossary_under_the_fixed_entries(home: Path, paper: Path) -> None:
    write(home / "glossary.json", {"terms": {"KV cache": "键值缓存"}})
    session = Glossary(terms=(("KV Cache", "KV 缓存"), ("vLLM", "某框架")), do_not_translate=("LLM",))
    merged = glossary.validation_glossary(selection(home, paper), session)
    assert dict(merged.terms) == {"vLLM": "某框架", "KV cache": "键值缓存"}
    assert merged.do_not_translate == ("LLM",)


def test_a_missing_session_glossary_reads_as_empty(tmp_path: Path) -> None:
    assert glossary.read_session_glossary(tmp_path) == Glossary()
    write(tmp_path / "glossary.json", {"terms": {"KV cache": ""}})
    with pytest.raises(glossary.GlossaryError):
        glossary.read_session_glossary(tmp_path)


def test_the_paper_glossary_labels_each_entry_with_its_source(home: Path, paper: Path) -> None:
    write(home / "glossary.learned.json", {"terms": {"KV cache": "KV 缓存", "long context": "长上下文"}})
    write(home / "glossary.json", {"do_not_translate": ["LLM"], "terms": {"large language model": "大语言模型"}})
    chosen = selection(home, paper)
    session = Glossary(
        terms=(("KV cache", "KV 缓存"), ("long context", "长语境"), ("decoding", "解码"), ("LLMs", "大模型")),
        do_not_translate=("vLLM",),
    )
    assert glossary.paper_glossary(chosen, session) == PaperGlossary(
        terms=[
            PaperTerm(word="large language model", translation="大语言模型", source="user"),
            PaperTerm(word="KV cache", translation="KV 缓存", source="learned"),
            PaperTerm(word="long context", translation="长语境", source="new"),
            PaperTerm(word="decoding", translation="解码", source="new"),
        ],
        do_not_translate=[
            PaperKeptWord(word="LLM", source="user"),
            PaperKeptWord(word="vLLM", source="new"),
        ],
    )


def test_growing_appends_only_words_the_learned_glossary_lacks() -> None:
    learned = Glossary(terms=(("KV cache", "KV 缓存"),), do_not_translate=("vLLM",))
    fixed = glossary.Selection(fixed=Glossary(do_not_translate=("LLM",)), learned=Glossary(), unmatched=())
    session = Glossary(
        terms=(("kv-cache", "键值缓存"), ("decoding", "解码"), ("LLMs", "大模型")),
        do_not_translate=("token", "vLLM"),
    )
    grown, added = glossary.grow(learned, fixed, session)
    assert added == 2
    assert grown == Glossary(terms=(("KV cache", "KV 缓存"), ("decoding", "解码")), do_not_translate=("vLLM", "token"))


def test_publish_writes_the_paper_glossary_and_the_learned_glossary(home: Path, paper: Path, tmp_path: Path) -> None:
    write(home / "glossary.learned.json", {"terms": {"KV cache": "KV 缓存"}})
    write(home / "glossary.json", {"do_not_translate": ["LLM"]})
    chosen = selection(home, paper)
    session_dir = tmp_path / "session"
    glossary.write_session_files(session_dir, chosen)
    write(
        session_dir / "glossary.json",
        {"terms": {"KV cache": "键值缓存", "decoding": "解码"}, "do_not_translate": ["LLM"]},
    )
    out = paper / "out" / "paper.glossary.json"
    assert glossary.publish(session_dir, chosen, out, env(home)) == 1
    written = PaperGlossary.model_validate_json(out.read_text(encoding="utf-8"))
    assert [(term.word, term.source) for term in written.terms] == [("KV cache", "new"), ("decoding", "new")]
    assert [(word.word, word.source) for word in written.do_not_translate] == [("LLM", "user")]
    learned = json.loads((home / "glossary.learned.json").read_text(encoding="utf-8"))
    assert learned == {"terms": {"KV cache": "KV 缓存", "decoding": "解码"}, "do_not_translate": []}


def test_publish_leaves_the_learned_glossary_alone_when_nothing_is_new(home: Path, paper: Path, tmp_path: Path) -> None:
    chosen = selection(home, paper)
    session_dir = tmp_path / "session"
    glossary.write_session_files(session_dir, chosen)
    assert glossary.publish(session_dir, chosen, paper / "out" / "paper.glossary.json", env(home)) == 0
    assert not glossary.learned_path(env(home)).exists()
