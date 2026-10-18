"""The numeric helpers writers share: clamping, finite floats, rounding half away from zero."""

import math
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tumbleweed.ui.runtime.values import (
    MAX_FLOAT,
    clamp_int,
    finite,
    number_of,
    round_half_away,
    to_float,
)
from tumbleweed.ui.spec.limits import MAX_SAFE_INT


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (0, 0),
        (MAX_SAFE_INT, MAX_SAFE_INT),
        (MAX_SAFE_INT + 1, MAX_SAFE_INT),
        (-MAX_SAFE_INT - 1, -MAX_SAFE_INT),
        (10**400, MAX_SAFE_INT),
        (-(10**400), -MAX_SAFE_INT),
    ],
)
def test_clamp_int(number: int, expected: int) -> None:
    assert clamp_int(number) == expected


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (1.5, 1.5),
        (-0.0, -0.0),
        (MAX_FLOAT, MAX_FLOAT),
        (math.inf, MAX_FLOAT),
        (-math.inf, -MAX_FLOAT),
        (math.nan, 0.0),
    ],
)
def test_finite(number: float, expected: float) -> None:
    assert finite(number) == expected


def test_to_float_is_exact_in_the_safe_range_and_saturates_beyond_a_double() -> None:
    assert to_float(MAX_SAFE_INT) == float(MAX_SAFE_INT)
    assert to_float(10**400) == MAX_FLOAT
    assert to_float(-(10**400)) == -MAX_FLOAT


def test_number_of_is_an_int_or_a_float_and_never_a_bool() -> None:
    assert number_of(3) == 3
    assert number_of(2.5) == 2.5
    assert number_of(True) is None
    assert number_of("3") is None
    assert number_of(None) is None


@given(st.floats(allow_nan=False, allow_infinity=False, min_value=-9e15, max_value=9e15))
def test_round_half_away_is_exact(number: float) -> None:
    exact = Fraction(number)
    magnitude = abs(exact)
    expected = math.floor(magnitude + Fraction(1, 2))
    expected = -expected if exact < 0 else expected

    assert round_half_away(number) == expected


@given(st.floats(allow_nan=True, allow_infinity=True))
def test_round_half_away_is_total_and_within_the_safe_range(number: float) -> None:
    assert abs(round_half_away(number)) <= MAX_SAFE_INT
