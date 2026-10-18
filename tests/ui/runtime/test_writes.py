"""What a value becomes when it is written to an entry (`docs/ui-spec.md` section 7)."""

import pytest
from pydantic import TypeAdapter

from tests.ui.runtime.support import choice, flag, number, real, text
from tests.ui.spec.support import Json
from tumbleweed.ui.runtime import coerce_for_write
from tumbleweed.ui.runtime.values import MAX_FLOAT, Value
from tumbleweed.ui.spec.limits import MAX_SAFE_INT
from tumbleweed.ui.spec.state import StateDecl, StateValue

DECL = TypeAdapter(StateDecl)


def coerced(decl: Json, value: Value) -> StateValue | None:
    return coerce_for_write(DECL.validate_python(decl), value)


class TestNumbers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(5, 5), (-3, 0), (150, 100), (0, 0), (100, 100), (2.5, 3), (-2.5, 0), (99.5, 100)],
    )
    def test_an_int_entry_clamps_into_its_bounds(self, value: float, expected: int) -> None:
        assert coerced(number("n", 1, minimum=0, maximum=100), value) == expected

    def test_a_float_written_to_an_int_entry_is_rounded_half_away_from_zero_first(self) -> None:
        assert coerced(number("n", 1, minimum=-10, maximum=10), -2.5) == -3
        assert coerced(number("n", 1, minimum=-10, maximum=10), 0.49999999999999994) == 0

    @pytest.mark.parametrize(
        ("value", "expected"), [(5, 5.0), (-3, 0.0), (150, 100.0), (0.25, 0.25)]
    )
    def test_a_float_entry_clamps_and_an_int_becomes_a_float(
        self, value: float, expected: float
    ) -> None:
        result = coerced(real("f", 1.0, minimum=0.0, maximum=100.0), value)

        assert result == expected
        assert type(result) is float

    def test_an_int_written_to_a_float_entry_is_a_float_without_bounds_too(self) -> None:
        result = coerced(real("f", 1.0), 7)

        assert result == 7.0
        assert type(result) is float

    def test_an_entry_without_bounds_clamps_only_to_what_a_number_can_be(self) -> None:
        assert coerced(number("n", 0), 10**30) == MAX_SAFE_INT
        assert coerced(number("n", 0), -(10**30)) == -MAX_SAFE_INT
        assert coerced(real("f", 0.0), float("inf")) == MAX_FLOAT

    def test_one_bound_alone_applies(self) -> None:
        assert coerced(number("n", 5, minimum=0), -4) == 0
        assert coerced(number("n", 5, minimum=0), 40) == 40
        assert coerced(number("n", 5, maximum=10), 40) == 10

    @pytest.mark.parametrize("value", [True, "5", None])
    def test_something_that_is_not_a_number_is_not_written(self, value: object) -> None:
        assert coerced(number("n", 1), value) is None  # type: ignore[arg-type]  # the point
        assert coerced(real("f", 1.0), value) is None  # type: ignore[arg-type]  # the point


class TestStrings:
    def test_a_string_is_cut_to_max_length_characters(self) -> None:
        assert coerced(text("s", max_length=3), "abcdef") == "abc"
        assert coerced(text("s", max_length=3), "abc") == "abc"
        assert coerced(text("s", max_length=3), "") == ""

    def test_the_cut_counts_characters_not_bytes(self) -> None:
        assert coerced(text("s", max_length=2), "é€\U0001f600") == "é€"

    def test_a_number_is_not_written_to_a_string_entry(self) -> None:
        assert coerced(text("s"), 5) is None


class TestEnumsAndBools:
    def test_an_option_is_written(self) -> None:
        assert coerced(choice("e", ["a", "b"], "a"), "b") == "b"

    def test_a_string_that_is_no_option_keeps_the_old_value(self) -> None:
        assert coerced(choice("e", ["a", "b"], "a"), "c") is None
        assert coerced(choice("e", ["a", "b"], "a"), "A") is None
        assert coerced(choice("e", ["a", "b"], "a"), "") is None

    def test_a_bool_is_written_to_a_bool_entry_and_nothing_else(self) -> None:
        assert coerced(flag("b"), True) is True
        assert coerced(flag("b"), 1) is None
        assert coerced(flag("b"), "true") is None


@pytest.mark.parametrize(
    "decl",
    [
        {"kind": "image", "key": "x"},
        {"kind": "file", "key": "x"},
        {"kind": "table", "key": "x"},
        {"kind": "list", "key": "x", "item_type": "int"},
        {"kind": "json", "key": "x"},
    ],
)
def test_entries_that_only_a_patch_can_change_refuse_a_written_value(decl: Json) -> None:
    assert coerced(decl, "anything") is None
