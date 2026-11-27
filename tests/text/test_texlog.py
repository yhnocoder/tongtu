from __future__ import annotations

from tongtu import texlog

LOG = (
    'Missing character: There is no н ("43D) in font larm1000!\n'
    "Missing character: There is no о (U+043E) in font larm1000!\n"
    "Some other line\n"
    'Missing character: There is no о ("43E) in font larm1000!\n'
    'Missing character: There is no ( ("28) in font nullfont!\n'
)


def test_missing_characters_are_sorted_by_count() -> None:
    assert texlog.missing_characters(LOG) == [("о", 2), ("н", 1), ("(", 1)]


def test_missing_characters_in_a_clean_log() -> None:
    assert texlog.missing_characters("Output written on main.xdv (1 page, 10 bytes).\n") == []


def test_parse_counts_counts_every_missing_character_line() -> None:
    assert texlog.parse_counts(LOG)["missing_characters"] == 4
