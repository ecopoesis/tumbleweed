"""A search made safe for SQLite FTS5.

`MATCH` takes a query language (`AND`, `NOT`, `NEAR`, column filters, `*`, quotes, parentheses),
and a syntax error in it is an exception, while some well-formed queries are costly. A user's or
a model's text is therefore never given to `MATCH`: `fts_match` takes the words out of it, drops
all of its punctuation (so none of the syntax survives), bounds how many words and how long, and
quotes each one as an FTS5 string with a trailing `*` (a prefix match), so the result is always a
well-formed conjunction of at most `MAX_QUERY_TERMS` prefix terms. The text is also passed to
SQLite as a bound parameter, never spliced into SQL.
"""

import re
from typing import Final

from tumbleweed.tools.limits import MAX_QUERY_CHARS, MAX_QUERY_TERMS, MAX_TERM_CHARS

_WORD: Final = re.compile(r"\w+")


def fts_match(text: str) -> str | None:
    """The `MATCH` string for what the user typed, or None if there is no word in it."""
    terms = _WORD.findall(text[:MAX_QUERY_CHARS])[:MAX_QUERY_TERMS]
    if not terms:
        return None
    # `\w` never matches a double quote, so a term cannot end the FTS5 string it is put in.
    return " ".join(f'"{term[:MAX_TERM_CHARS]}"*' for term in terms)
