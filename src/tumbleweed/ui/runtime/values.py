"""The scalar values an expression works with, and the numeric rules shared by its writers.

An expression sees four types: `bool`, `int`, `float` and `str`. These helpers give the numeric
rules of `docs/ui-spec.md` section 7 one home: an `int` is held to +-(2**53 - 1), a `float` is
always finite (infinity becomes the largest finite float of its sign, not-a-number becomes 0.0),
and `round` rounds halves away from zero.
"""

import math
import sys
from typing import Final

from tumbleweed.ui.spec.limits import MAX_SAFE_INT

type Value = bool | int | float | str

MAX_FLOAT: Final = sys.float_info.max


def clamp_int(number: int) -> int:
    """`number` held to +-(2**53 - 1)."""
    return max(-MAX_SAFE_INT, min(MAX_SAFE_INT, number))


def finite(number: float) -> float:
    """`number`, with infinity as the largest finite float of its sign and NaN as 0.0."""
    if math.isnan(number):
        return 0.0
    if math.isinf(number):
        return MAX_FLOAT if number > 0 else -MAX_FLOAT
    return number


def to_float(number: int) -> float:
    """An int as a float. Exact for an int within +-(2**53 - 1); saturates beyond a double."""
    try:
        return float(number)
    except OverflowError:
        return MAX_FLOAT if number > 0 else -MAX_FLOAT


def round_half_away(number: float) -> int:
    """The nearest whole number, halves away from zero, held to +-(2**53 - 1).

    Exact: `abs(number) - floor(abs(number))` is computed without error for a double, so
    0.49999999999999994 rounds down (adding 0.5 first would round it up).
    """
    number = finite(number)
    magnitude = abs(number)
    whole = math.floor(magnitude)
    if magnitude - whole >= 0.5:
        whole += 1
    return clamp_int(-whole if number < 0 else whole)


def number_of(value: object) -> int | float | None:
    """`value` if it is a number (an `int` or a `float`, never a `bool`), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value
