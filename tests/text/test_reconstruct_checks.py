from __future__ import annotations

import collections
from pathlib import Path

import pytest

from tongtu.artifacts.reconstruct import CheckReport
from tongtu.reconstruct import checks
from tongtu.reconstruct.crops import CropError

WORDS = [f"word{chr(97 + index // 26)}{chr(97 + index % 26)}" for index in range(40)]


def passing_report(**changes: object) -> CheckReport:
    fields: dict[str, object] = {
        "source_pages": 10,
        "pages": 10,
        "recall": 1.0,
        "precision": 1.0,
        "missing_characters": 0,
        "order": 1.0,
        "passed": True,
    }
    fields.update(changes)
    return CheckReport.model_validate(fields)


def test_text_words_joins_line_end_hyphens_and_drops_short_words() -> None:
    assert checks.text_words("An opti-\nmizer of SGD, ok?") == collections.Counter({"optimizer": 1, "sgd": 1})


def test_recall_precision_counts_the_shared_multiset() -> None:
    original = collections.Counter({"adam": 3, "step": 1})
    rebuilt = collections.Counter({"adam": 2, "step": 1, "extra": 1})
    assert checks.recall_precision(original, rebuilt) == (0.75, 0.75)
    assert checks.recall_precision(collections.Counter(), collections.Counter()) == (0.0, 0.0)


def test_unique_grams_keeps_fragments_that_occur_once() -> None:
    words = ["a", "b", "c", "d", "e", "f", "a", "b", "c", "d", "e", "f"]
    grams = checks.unique_grams(words)
    assert ("a", "b", "c", "d", "e", "f") not in grams
    assert grams[("b", "c", "d", "e", "f", "a")] == 1


def test_longest_increasing_returns_member_indices() -> None:
    assert checks.longest_increasing([0, 5, 1, 2, 3]) == {0, 2, 3, 4}
    assert checks.longest_increasing([]) == set()


def test_order_score_of_identical_sequences_is_one() -> None:
    order = checks.order_score(WORDS, WORDS, WORDS)
    assert order.score == 1.0
    assert order.grams == len(WORDS) - checks.ORDER_GRAM_WORDS + 1
    assert order.out_of_order == []


def test_order_score_marks_a_moved_block() -> None:
    rebuilt = WORDS[20:] + WORDS[:20]
    order = checks.order_score(WORDS, WORDS, rebuilt)
    assert order.score < checks.ORDER_MIN
    assert order.out_of_order
    assert all(position < 20 for position in order.out_of_order)


def test_order_score_without_shared_fragments_is_one() -> None:
    order = checks.order_score(WORDS, WORDS, ["other"] * 10)
    assert (order.score, order.grams, order.out_of_order) == (1.0, 0, [])


def test_order_score_ignores_fragments_repeated_in_the_unmasked_original() -> None:
    masked = WORDS[:10]
    unmasked = WORDS[:10] + WORDS[:10]
    order = checks.order_score(masked, unmasked, WORDS[:10])
    assert order.grams == 0


def test_hint_text_strips_commands_and_keeps_escaped_characters() -> None:
    assert checks.hint_text(r"\mathbf{x}\_1 \& 50\% \textbackslash{}") == "x_1 & 50% \\"


def test_glyph_counts_normalizes_and_drops_spaces() -> None:
    assert checks.glyph_counts("ﬁ x") == collections.Counter({"f": 1, "i": 1, "x": 1})


def test_failed_items_of_a_passing_report_is_empty() -> None:
    assert checks.failed_items(passing_report()) == []


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"pages": 0}, "no compiled PDF with pages"),
        ({"recall": 0.97}, "recall 0.9700 < 0.98"),
        ({"precision": 0.9}, "precision 0.9000 < 0.98"),
        ({"missing_characters": 3}, "3 missing characters"),
        ({"order": 0.95}, "reading order 0.9500 < 0.99"),
        ({"pages": 13}, "page ratio 13/10 above 1.2 and more than 2 pages over the source"),
        ({"pages": 8}, "page ratio 8/10 below 0.85"),
        ({"source_pages": 2, "pages": 5}, "page ratio 5/2 above 1.2 and more than 2 pages over the source"),
        ({"source_pages": 30, "pages": 37}, "page ratio 37/30 above 1.2 and more than 2 pages over the source"),
    ],
)
def test_failed_items_names_each_failed_threshold(changes: dict[str, object], expected: str) -> None:
    assert expected in checks.failed_items(passing_report(**changes, passed=False))


def test_draft_source_switches_graphicx_to_draft() -> None:
    tex = "\\usepackage{graphicx}\n\\begin{document}\nbody\n\\end{document}\n"
    draft = checks.draft_source(tex)
    assert "\\usepackage[draft]{graphicx}" in draft
    assert draft.index(checks.FLOATING_REMOVED) < draft.index("\\begin{document}")


def test_lost_glyphs_of_an_unreadable_crop_names_the_crop(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text("text \\im{m-0002}{1.5bp}{x}\n", encoding="utf-8")
    (tmp_path / "crops").mkdir()
    (tmp_path / "crops" / "m-0002.pdf").write_bytes(b"not a pdf")
    with pytest.raises(CropError, match="cannot open the crop m-0002.pdf"):
        checks.lost_glyphs(tmp_path)


@pytest.mark.parametrize(
    ("source_pages", "pages", "passed"), [(2, 4, True), (2, 5, False), (30, 36, True), (30, 37, False)]
)
def test_page_count_allows_the_larger_of_the_ratio_and_two_extra_pages(
    source_pages: int, pages: int, passed: bool
) -> None:
    assert (checks.page_count_problem(pages, source_pages) == "") is passed
