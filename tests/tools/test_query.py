"""A search is reduced to words before SQLite sees it: no FTS5 syntax, bounded cost (S23)."""

import sqlite3

import pytest
import regex
from hypothesis import given
from hypothesis import strategies as st

from tumbleweed.tools import fts_match
from tumbleweed.tools.limits import MAX_QUERY_CHARS, MAX_QUERY_TERMS, MAX_TERM_CHARS

WELL_FORMED = regex.compile(r'"[\w\p{M}]{1,64}"\*( "[\w\p{M}]{1,64}"\*){0,7}')


def test_words_become_quoted_prefix_terms() -> None:
    assert fts_match("unit converter") == '"unit"* "converter"*'
    assert fts_match("  Unit   Convert!  ") == '"Unit"* "Convert"*'


@pytest.mark.parametrize("text", ["", "   ", "!!!", '""', "()", "*", "-", "\n\t", "...--"])
def test_text_without_a_word_is_no_query(text: str) -> None:
    assert fts_match(text) is None


@pytest.mark.parametrize(
    "text",
    [
        'foo" OR "bar',
        "foo OR bar",
        "NOT foo",
        "foo NEAR bar",
        "NEAR(a b, 2)",
        "name:foo",
        "{name tags}: foo",
        "^foo",
        "foo*",
        "-foo",
        "foo AND (bar OR baz",
        '"unterminated',
        "a'; DROP TABLE tools; --",
        "\x00foo",
    ],
)
def test_the_syntax_of_fts5_does_not_survive(text: str) -> None:
    match = fts_match(text)

    assert match is not None
    assert WELL_FORMED.fullmatch(match), match
    # Every operator is just a word to look for, quoted: none is an operator any more.
    assert "(" not in match
    assert ":" not in match
    assert " OR " not in match.replace('"OR"*', "")


def test_at_most_eight_terms_count() -> None:
    match = fts_match(" ".join(f"w{n}" for n in range(50)))

    assert match is not None
    assert match.count('"*') == MAX_QUERY_TERMS


def test_a_term_is_cut_to_its_limit() -> None:
    match = fts_match("x" * 5000)

    assert match == f'"{"x" * MAX_TERM_CHARS}"*'


def test_the_text_is_cut_before_it_is_looked_at() -> None:
    text = "a " * MAX_QUERY_CHARS + "tail"

    match = fts_match(text)

    assert match is not None
    assert "tail" not in match


def test_letters_of_any_script_are_words() -> None:
    assert fts_match("caf\N{LATIN SMALL LETTER E WITH ACUTE} 東京") == '"café"* "東京"*'


NFD_WORDS = [
    "cr\N{LATIN SMALL LETTER E}\N{COMBINING GRAVE ACCENT}me",
    "br\N{LATIN SMALL LETTER U}\N{COMBINING CIRCUMFLEX ACCENT}l\N{LATIN SMALL LETTER E}e",
    "\N{LATIN CAPITAL LETTER E}\N{COMBINING ACUTE ACCENT}cole",
]


@pytest.mark.parametrize("word", NFD_WORDS)
def test_an_accent_typed_as_a_second_character_is_the_stored_word(word: str) -> None:
    import unicodedata

    match = fts_match(word)

    assert match == f'"{unicodedata.normalize("NFC", word)}"*'
    assert "\N{COMBINING GRAVE ACCENT}" not in match


def test_a_letter_with_no_precomposed_form_keeps_its_mark_inside_one_word() -> None:
    assert fts_match("x\N{COMBINING ACUTE ACCENT}y") == '"x\N{COMBINING ACUTE ACCENT}y"*'


def test_a_text_far_too_long_is_cut_before_it_is_normalised() -> None:
    match = fts_match("a" * 10_000_000)

    assert match == f'"{"a" * MAX_TERM_CHARS}"*'


@given(st.text(max_size=400))
def test_whatever_is_typed_sqlite_accepts_the_result(text: str) -> None:
    match = fts_match(text)
    if match is None:
        return
    assert WELL_FORMED.fullmatch(match)
    database = sqlite3.connect(":memory:")
    try:
        database.execute("CREATE VIRTUAL TABLE t USING fts5(a, b, c)")
        database.execute("INSERT INTO t VALUES ('hello world', 'x', 'y')")
        database.execute("SELECT rowid FROM t WHERE t MATCH ?", (match,)).fetchall()
    finally:
        database.close()
