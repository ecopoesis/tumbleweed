"""A search made safe for SQLite FTS5.

`MATCH` takes a query language (`AND`, `NOT`, `NEAR`, column filters, `*`, quotes, parentheses),
and a syntax error in it is an exception, while some well-formed queries are costly. A user's or
a model's text is therefore never given to `MATCH`: `fts_match` takes the words out of it, drops
all of its punctuation (so none of the syntax survives), bounds how many words and how long, and
quotes each one as an FTS5 string with a trailing `*` (a prefix match), so the result is always a
well-formed conjunction of at most `MAX_QUERY_TERMS` prefix terms. The text is also passed to
SQLite as a bound parameter, never spliced into SQL.
"""

from typing import Final

import regex

from tumbleweed.core.canonical import normalize_text
from tumbleweed.tools.limits import MAX_QUERY_CHARS, MAX_QUERY_TERMS, MAX_TERM_CHARS

# Letters, digits and the combining marks that belong to them: a letter with no precomposed form
# (`x` and U+0301) is one word, not two.
_WORD: Final = regex.compile(r"[\w\p{M}]+")


def fts_match(text: str) -> str | None:
    """The `MATCH` string for what the user typed, or None if there is no word in it."""
    # NFC first, as the stored text is: an accent typed as a second character (NFD) is the same
    # word as the stored one. The cut comes after, so the limit is on what is searched; the first
    # cut only bounds the work of a text that is far too long.
    searched = normalize_text(text[: MAX_QUERY_CHARS * 4])[:MAX_QUERY_CHARS]
    terms = _WORD.findall(searched)[:MAX_QUERY_TERMS]
    if not terms:
        return None
    # Neither `\w` nor a mark is a double quote, so a term cannot end the FTS5 string it is in.
    return " ".join(f'"{term[:MAX_TERM_CHARS]}"*' for term in terms)
