"""The scalar types every model in a spec is built from: text, safe integers, finite floats.

Two kinds of text, because they have different owners:

- **Authored text** (titles, labels, placeholders, option names, string literals, notes) is
  written by the model into the spec and is part of what a tool package hashes. It may hold no
  control character (a block may hold `\\n` and `\\t`), no Unicode format character (bidi
  overrides, zero-width space, byte-order mark; the zero-width joiner and non-joiner that real
  scripts and emoji need are allowed), no line or paragraph separator and no lone surrogate:
  the same rules as `WorkspaceRelPath`, for the same reason, that text must not look like
  something else or end a line in a log or the UI.
- **Data text** (the value of a string state, a cell of a table, an item of a list, strings
  inside a JSON value) comes from the user and from sandboxes, and a CSV can hold anything. It
  may hold any character except NUL, which GTK and C strings cannot carry, and a lone surrogate,
  which cannot be encoded as UTF-8 and so cannot be written as JSON.

Numbers are restricted so that the canonical JSON of a spec (RFC 8785, hashed by S23) is exact:
integers stay within +-(2**53 - 1) and floats are finite. Integers and floats are separate types
and the type of a value is never inferred from how a number is written (`5` or `5.0`), because
canonical JSON writes both as `5`; a float field accepts an integer, an integer field does not
accept `5.0`.
"""

import unicodedata
from typing import Annotated, Final

from pydantic import AfterValidator, Field

from tumbleweed.core.errors import InvalidValueError, excerpt
from tumbleweed.ui.spec.limits import (
    MAX_CELL_LENGTH,
    MAX_LINE_LENGTH,
    MAX_OPTION_LENGTH,
    MAX_SAFE_INT,
    MAX_STRING_LENGTH,
    MAX_TEXT_LENGTH,
    MAX_TITLE_LENGTH,
)

_BLOCK_CONTROLS: Final = frozenset({"\n", "\t"})
_ALLOWED_FORMAT: Final = frozenset({"‌", "‍"})  # zero-width non-joiner and joiner
_FORBIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})


def _check_authored(value: str, *, allowed_controls: frozenset[str]) -> str:
    for char in value:
        if char in allowed_controls or char in _ALLOWED_FORMAT:
            continue
        category = unicodedata.category(char)
        if category in _FORBIDDEN_CATEGORIES:
            raise InvalidValueError(
                f"text must not contain control, format, separator or surrogate characters "
                f"(found U+{ord(char):04X} in {excerpt(value)})"
            )
    return value


def check_line(value: str) -> str:
    return _check_authored(value, allowed_controls=frozenset())


def check_block(value: str) -> str:
    return _check_authored(value, allowed_controls=_BLOCK_CONTROLS)


def check_data_text(value: str) -> str:
    """Refuse NUL and lone surrogates, the only characters data text cannot carry."""
    for char in value:
        if char == "\0" or unicodedata.category(char) == "Cs":
            raise InvalidValueError(
                f"text must not contain NUL or a lone surrogate (found U+{ord(char):04X} "
                f"in {excerpt(value)})"
            )
    return value


def as_tuple(value: object) -> object:
    """Accept a `list` where a tuple is declared, so that parsed JSON validates in Python mode.

    The models hold tuples so that a validated spec cannot be edited afterwards, but the Python
    form of a JSON array is a list. Only an exact `list` is converted; anything else is left for
    the tuple validator to refuse.
    """
    return tuple(value) if type(value) is list else value


Title = Annotated[str, Field(min_length=1, max_length=MAX_TITLE_LENGTH), AfterValidator(check_line)]
Line = Annotated[str, Field(max_length=MAX_LINE_LENGTH), AfterValidator(check_line)]
Block = Annotated[str, Field(max_length=MAX_TEXT_LENGTH), AfterValidator(check_block)]
Option = Annotated[
    str, Field(min_length=1, max_length=MAX_OPTION_LENGTH), AfterValidator(check_line)
]
Cell = Annotated[str, Field(max_length=MAX_CELL_LENGTH), AfterValidator(check_data_text)]
DataString = Annotated[str, Field(max_length=MAX_STRING_LENGTH), AfterValidator(check_data_text)]
ColumnName = Annotated[
    str, Field(min_length=1, max_length=MAX_CELL_LENGTH), AfterValidator(check_data_text)
]

SafeInt = Annotated[int, Field(ge=-MAX_SAFE_INT, le=MAX_SAFE_INT)]
PositiveSafeInt = Annotated[int, Field(ge=1, le=MAX_SAFE_INT)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
PositiveFiniteFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]

__all__ = [
    "Block",
    "Cell",
    "ColumnName",
    "DataString",
    "FiniteFloat",
    "Line",
    "Option",
    "PositiveFiniteFloat",
    "PositiveSafeInt",
    "SafeInt",
    "Title",
    "as_tuple",
    "check_block",
    "check_data_text",
    "check_line",
]
