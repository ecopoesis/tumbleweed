"""The scalar types every model in a spec is built from: text, numbers, cells.

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

Numbers are IEEE-754 doubles, as in I-JSON and in RFC 8785 (JCS), the canonical JSON that the
tool store hashes (S23), and one rule covers them everywhere, so that a spec still validates
after canonicalisation, which writes `1e16` as `10000000000000000` and `2**60` as
`1152921504606847000`:

- a number in an `int` position (an `int` entry or literal, a table `int` cell, an item of a list
  of ints) is a whole number of magnitude at most 2**53 - 1, and is never written as `5.0`;
- a number in a `float` position (a `float` entry or literal, a `float` column, an item of a
  list of floats) is a finite double. It may be written as an integer of any size that stands for
  a double, and is then stored as a float: `5`, `5.0` and `5e0` are one value;
- a number in a `json` value is a finite double, an integer being accepted by the same test (and
  stored as a float if it is beyond 2**53 - 1).

An integer stands for a double if it is exactly that double (`2**70`) or if it is how
canonical JSON writes the nearest one (`1152921504606847000`, which is not exactly `2**60`:
`tumbleweed.core.jcs.double_of_int`). No type is inferred from how a number is written. An
integer that is neither, such as 2**53 + 1 or 10**400, is refused in a float or JSON position,
not silently rounded.
"""

import math
import unicodedata
from typing import Annotated, Final

from pydantic import AfterValidator, BeforeValidator, Field, PlainValidator, WithJsonSchema

from tumbleweed.core.errors import InvalidValueError, excerpt
from tumbleweed.core.jcs import double_of_int
from tumbleweed.ui.spec.limits import (
    MAX_CELL_LENGTH,
    MAX_LINE_LENGTH,
    MAX_OPTION_LENGTH,
    MAX_SAFE_INT,
    MAX_STRING_LENGTH,
    MAX_TEXT_LENGTH,
    MAX_TITLE_LENGTH,
)

type Scalar = bool | int | float | str

_BLOCK_CONTROLS: Final = ("\n", "\t")
_ALLOWED_FORMAT: Final = frozenset({"\u200c", "\u200d"})  # zero-width non-joiner and joiner
_FORBIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
_DOUBLE_BITS: Final = 1024  # no double is as wide as 2**1024


def _check_authored(value: str, *, allowed_controls: tuple[str, ...]) -> str:
    # A fast path in C: what `str.isprintable` accepts (letters, marks, numbers, punctuation,
    # symbols, the ASCII space) is all allowed. Anything else, such as the joiners, is
    # looked at one character at a time.
    plain = value
    for control in allowed_controls:
        plain = plain.replace(control, "")
    if plain.isprintable():
        return value
    for char in value:
        if char in allowed_controls or char in _ALLOWED_FORMAT:
            continue
        if unicodedata.category(char) in _FORBIDDEN_CATEGORIES:
            raise InvalidValueError(
                f"text must not contain control, format, separator or surrogate characters "
                f"(found U+{ord(char):04X} in {excerpt(value)})"
            )
    return value


def check_line(value: str) -> str:
    """Authored one-line text: no control character at all."""
    return _check_authored(value, allowed_controls=())


def check_block(value: str) -> str:
    """Authored block text: like a line, but newline and tab are allowed."""
    return _check_authored(value, allowed_controls=_BLOCK_CONTROLS)


def check_data_text(value: str) -> str:
    """Refuse NUL and lone surrogates, the only characters data text cannot carry.

    Both tests run in C (`in` and `encode`), so a 65,536-character string costs microseconds.
    """
    if "\0" in value:
        raise InvalidValueError(
            f"text must not contain NUL or a lone surrogate (found U+0000 in {excerpt(value)})"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidValueError(
            f"text must not contain NUL or a lone surrogate "
            f"(found U+{ord(value[error.start]):04X} in {excerpt(value)})"
        ) from None
    return value


def text_bytes(value: str) -> int:
    """The number of bytes of `value` in UTF-8 (a lone surrogate counts as three)."""
    return len(value) if value.isascii() else len(value.encode("utf-8", "surrogatepass"))


def canonical_text(value: str) -> str:
    """`value` as the tool store writes text (S23): NFC, and a line feed for any line ending.

    Two texts that are equal after this are one text once saved, so names that must be distinct
    (JSON keys, column names) are compared in this form.
    """
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def double_of(value: int) -> float:
    """The float an integer stands for, or `InvalidValueError` if it stands for none.

    An integer stands for a double if it is exactly that double, or if canonical JSON writes the
    nearest double as that integer (`tumbleweed.core.jcs.double_of_int`): what canonicalisation
    makes of a double above 2**53 must still be accepted when it is read back.
    """
    number = double_of_int(value)
    if number is not None:
        return number
    if value.bit_length() > _DOUBLE_BITS:
        raise InvalidValueError("an integer too large for a double")
    raise InvalidValueError(
        "an integer that no double is equal to and that canonical JSON does not write for one "
        "(it is not rounded)"
    )


def as_double(value: object) -> object:
    """For a `float` position: an exact integer becomes a float, anything else is unchanged."""
    return double_of(value) if type(value) is int else value


def check_scalar(value: object) -> Scalar:
    """One cell of a table or item of a list: a bool, a number or a string, nothing else.

    A single check with a single message, so that a wrong value is one clear error and not one
    per alternative of a union. An integer is accepted if it stands for a double (the column or
    the list then decides whether it is an `int`, within 2**53 - 1, or a `float`), a float must
    be finite, a string is data text of at most `MAX_CELL_LENGTH` characters.
    """
    kind = type(value)
    if kind is bool:
        return bool(value)
    if isinstance(value, int) and kind is int:
        double_of(value)
        return value
    if isinstance(value, float) and kind is float:
        if not math.isfinite(value):
            raise InvalidValueError("a number must be finite")
        return value
    if isinstance(value, str) and kind is str:
        if len(value) > MAX_CELL_LENGTH:
            raise InvalidValueError(f"text is longer than {MAX_CELL_LENGTH} characters")
        return check_data_text(value)
    raise InvalidValueError(f"expected a string, a number or a bool, got {excerpt(value)}")


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
DataString = Annotated[str, Field(max_length=MAX_STRING_LENGTH), AfterValidator(check_data_text)]
ColumnName = Annotated[
    str, Field(min_length=1, max_length=MAX_CELL_LENGTH), AfterValidator(check_data_text)
]

# A cell or list item. One validator, so one message; the schema says what it accepts.
Cell = Annotated[
    bool | int | float | str,
    PlainValidator(check_scalar),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "boolean"},
                {"type": "number"},
                {"type": "string", "maxLength": MAX_CELL_LENGTH},
            ]
        }
    ),
]

SafeInt = Annotated[int, Field(ge=-MAX_SAFE_INT, le=MAX_SAFE_INT)]
PositiveSafeInt = Annotated[int, Field(ge=1, le=MAX_SAFE_INT)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False), BeforeValidator(as_double)]
PositiveFiniteFloat = Annotated[float, Field(gt=0, allow_inf_nan=False), BeforeValidator(as_double)]

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
    "Scalar",
    "Title",
    "as_double",
    "as_tuple",
    "canonical_text",
    "check_block",
    "check_data_text",
    "check_line",
    "check_scalar",
    "double_of",
    "text_bytes",
]
