"""Binding resolution, and the numeric range a slider, spin button or progress bar works over."""

import math
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter

from tests.ui.runtime.support import choice, flag, number, real, spec_with, text
from tests.ui.spec.support import BOUND, UNBOUND, Json
from tumbleweed.ui.runtime import NumericRange, UnresolvedBindingError, bind_of
from tumbleweed.ui.spec.components import Component
from tumbleweed.ui.spec.limits import MAX_SAFE_INT
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import StateDecl

COMPONENT = TypeAdapter(Component)
DECL = TypeAdapter(StateDecl)

# the kind of entry each component binds, and a declaration of it called `x`
DECLARATIONS: dict[str, Json] = {
    "string": text("x"),
    "enum": choice("x", ["a", "b"], "a"),
    "int": number("x", 5, minimum=0, maximum=10),
    "float": real("x", 0.5, minimum=0.0, maximum=1.0),
    "bool": flag("x"),
    "image": {"kind": "image", "key": "x"},
    "table": {"kind": "table", "key": "x"},
}


class TestBindOf:
    @pytest.mark.parametrize("name", sorted(BOUND))
    def test_a_bound_component_resolves_to_its_entry(self, name: str) -> None:
        component, kinds, writes, _bounded = BOUND[name]
        kind = sorted(kinds)[0]
        spec = spec_with([DECLARATIONS[kind]], root=component)
        decls: dict[StateKey, StateDecl] = spec.declarations()

        binding = bind_of(COMPONENT.validate_python(component), decls)

        assert binding is not None
        assert binding.key == "x"
        assert binding.decl == decls[StateKey("x")]
        if name == "text_view":
            return  # two-way only if editable: tested below
        assert binding.two_way is writes

    def test_a_text_view_is_two_way_only_when_editable(self) -> None:
        decls = spec_with([text("x")]).declarations()

        read_only = bind_of(COMPONENT.validate_python({"kind": "text_view", "bind": "x"}), decls)
        editable = bind_of(
            COMPONENT.validate_python({"kind": "text_view", "bind": "x", "editable": True}), decls
        )

        assert read_only is not None
        assert editable is not None
        assert (read_only.two_way, editable.two_way) == (False, True)

    @pytest.mark.parametrize("name", sorted(UNBOUND))
    def test_a_component_with_nothing_bound_has_no_binding(self, name: str) -> None:
        assert bind_of(COMPONENT.validate_python(UNBOUND[name]), {}) is None

    @pytest.mark.parametrize("kind", ["label", "heading"])
    def test_a_static_label_has_no_binding(self, kind: str) -> None:
        assert bind_of(COMPONENT.validate_python({"kind": kind, "text": "hi"}), {}) is None

    def test_a_binding_to_an_entry_the_spec_does_not_have_is_an_error_for_the_renderer(
        self,
    ) -> None:
        component = COMPONENT.validate_python({"kind": "label", "bind": "ghost"})

        with pytest.raises(UnresolvedBindingError):
            bind_of(component, {})


def range_of(decl: Json) -> NumericRange:
    return NumericRange.of(DECL.validate_python(decl))


class TestNumericRange:
    def test_an_int_range_has_whole_digits_and_its_own_step(self) -> None:
        numeric = range_of(number("n", 5, minimum=0, maximum=100, step=5))

        assert (numeric.minimum, numeric.maximum, numeric.step) == (0.0, 100.0, 5.0)
        assert numeric.digits == 0
        assert numeric.integer is True
        assert 5.0 <= numeric.page <= 100.0

    @pytest.mark.parametrize(
        ("minimum", "maximum", "step", "page"),
        [
            (0, 100, 1, 10.0),  # a tenth of the range
            (0, 100, 25, 25.0),  # the step, when it is wider than a tenth
            (0, 100, 100, 100.0),  # never wider than the range
            (0, 7, 1, 1.0),  # a tenth is narrower than the step
            (-50, 50, 3, 10.0),
        ],
    )
    def test_the_page_is_a_tenth_of_the_range_but_never_below_the_step_or_above_the_range(
        self, minimum: int, maximum: int, step: int, page: float
    ) -> None:
        numeric = range_of(number("n", minimum, minimum=minimum, maximum=maximum, step=step))

        assert numeric.page == page
        assert numeric.step == float(step)

    def test_a_float_range_shows_the_decimals_of_its_step(self) -> None:
        assert range_of(real("f", 0.5, minimum=0.0, maximum=10.0, step=0.01)).digits == 2
        assert range_of(real("f", 0.5, minimum=0.0, maximum=10.0, step=0.25)).digits == 2
        assert range_of(real("f", 0.5, minimum=0.0, maximum=10.0, step=2.0)).digits == 0
        assert range_of(real("f", 0.5, minimum=0.0, maximum=10.0, step=1e-12)).digits == 10

    def test_a_float_without_a_step_gets_a_power_of_ten_a_hundredth_of_the_range_or_so(
        self,
    ) -> None:
        steps = {
            width: range_of(real("f", 0.0, minimum=0.0, maximum=float(width))).step
            for width in (1, 5, 100, 12345)
        }

        assert steps == {1: 0.01, 5: 0.01, 100: 1.0, 12345: 100.0}

    def test_a_range_narrower_than_the_smallest_normal_float_has_no_default_step(self) -> None:
        # The spec allows it (a float entry without a step leaves the step to the renderer), and
        # a slider or spin button cannot do arithmetic on a subnormal step: it is refused here,
        # for the renderer to show a placeholder; a progress bar has no step and is fine.
        decl = real("f", 0.0, minimum=0.0, maximum=5e-324)

        with pytest.raises(ValueError, match="subnormal"):
            range_of(decl)
        numeric = NumericRange.of(DECL.validate_python(decl), stepped=False)
        assert numeric.fraction(0.0) == 0.0
        assert numeric.fraction(5e-324) == 1.0

    def test_the_narrowest_range_that_has_a_default_step(self) -> None:
        numeric = range_of(real("f", 0.0, minimum=0.0, maximum=sys.float_info.min))

        assert numeric.step == sys.float_info.min == numeric.width

    @pytest.mark.parametrize(
        ("minimum", "maximum"),
        [
            (-MAX_SAFE_INT // 2, MAX_SAFE_INT // 2),  # the widest range
            (0, MAX_SAFE_INT),
            (-MAX_SAFE_INT, -1),
            (MAX_SAFE_INT - 1, MAX_SAFE_INT),
            (0, 1),
        ],
    )
    def test_the_extremes_of_an_int_range_make_finite_widget_numbers(
        self, minimum: int, maximum: int
    ) -> None:
        numeric = range_of(number("n", minimum, minimum=minimum, maximum=maximum))

        for field in (numeric.minimum, numeric.maximum, numeric.step, numeric.page, numeric.width):
            assert math.isfinite(field)
        assert 0 < numeric.step <= numeric.width
        assert 0 < numeric.page <= numeric.width

    def test_the_smallest_step_a_spec_may_have_is_accepted(self) -> None:
        numeric = range_of(real("f", 0.0, minimum=0.0, maximum=1.0, step=sys.float_info.min))

        assert numeric.step == sys.float_info.min
        assert numeric.digits == 10  # capped: no widget shows 308 decimals

    def test_a_step_as_wide_as_the_range_is_accepted(self) -> None:
        numeric = range_of(real("f", 0.0, minimum=0.0, maximum=4.0, step=4.0))

        assert numeric.step == numeric.width == numeric.page == 4.0

    @pytest.mark.parametrize(
        "decl",
        [
            number("n", 0),
            number("n", 0, minimum=0),
            number("n", 0, maximum=5),
            real("f", 0.0),
            text("s"),
            flag("b"),
        ],
    )
    def test_an_entry_without_a_usable_range_is_a_value_error(self, decl: Json) -> None:
        with pytest.raises(ValueError, match=r"range|minimum|numeric"):
            range_of(decl)

    def test_a_fraction_is_where_a_value_lies_from_zero_to_one(self) -> None:
        numeric = range_of(number("n", 0, minimum=-10, maximum=10))

        assert numeric.fraction(-10) == 0.0
        assert numeric.fraction(0) == 0.5
        assert numeric.fraction(10) == 1.0
        assert numeric.fraction(1e300) == 1.0
        assert numeric.fraction(-1e300) == 0.0
        assert numeric.fraction(float("nan")) == 0.0

    def test_a_fraction_across_the_widest_range_does_not_lose_the_ends(self) -> None:
        numeric = range_of(number("n", 0, minimum=-MAX_SAFE_INT // 2, maximum=MAX_SAFE_INT // 2))

        assert numeric.fraction(-MAX_SAFE_INT // 2) == 0.0
        assert numeric.fraction(MAX_SAFE_INT // 2) == 1.0
        assert 0.49 < numeric.fraction(0) < 0.51

    def test_a_position_becomes_an_int_rounded_half_away_or_stays_a_float(self) -> None:
        whole = range_of(number("n", 0, minimum=-10, maximum=10))
        fractional = range_of(real("f", 0.0, minimum=-10.0, maximum=10.0))

        assert whole.to_state(2.5) == 3
        assert whole.to_state(-2.5) == -3
        assert type(whole.to_state(2.4)) is int
        assert fractional.to_state(2.5) == 2.5

    @given(
        low=st.integers(-MAX_SAFE_INT, MAX_SAFE_INT - 1),
        data=st.data(),
    )
    def test_every_range_the_spec_accepts_makes_usable_numbers(
        self, low: int, data: st.DataObject
    ) -> None:
        high = data.draw(st.integers(low + 1, min(MAX_SAFE_INT, low + MAX_SAFE_INT)))
        step = data.draw(st.one_of(st.none(), st.integers(1, high - low)))
        fields: dict[str, int] = {"minimum": low, "maximum": high}
        if step is not None:
            fields["step"] = step

        numeric = range_of(number("n", low, **fields))

        assert 0 < numeric.step <= numeric.width
        assert 0 < numeric.page <= numeric.width
        assert numeric.fraction(float(low)) == 0.0
        assert numeric.fraction(float(high)) == 1.0

    @given(
        low=st.floats(-1e15, 1e15, allow_nan=False),
        width=st.floats(min_value=5e-324, max_value=1e15, allow_nan=False),
        data=st.data(),
    )
    def test_every_float_range_the_spec_accepts_makes_usable_numbers(
        self, low: float, width: float, data: st.DataObject
    ) -> None:
        high = low + width
        if not low < high:
            return  # the width vanished into the rounding of the sum: not a range
        span = high - low
        step = data.draw(
            st.one_of(st.none(), st.floats(sys.float_info.min, max(span, sys.float_info.min)))
        )
        fields: dict[str, float] = {"minimum": low, "maximum": high}
        if step is not None and step <= span:
            fields["step"] = step
        elif span < sys.float_info.min:
            return  # no step to default to: `test_a_range_narrower...` is about this

        numeric = range_of(real("f", low, **fields))

        assert math.isfinite(numeric.step)
        assert 0 < numeric.step <= numeric.width
        assert 0 < numeric.page <= numeric.width
        assert 0 <= numeric.digits <= 10
        assert 0.0 <= numeric.fraction(low) <= numeric.fraction(high) <= 1.0


class TestSnapStep:
    """Only what a step or page increment produced is cleaned of floating-point noise."""

    def numeric(self, step: float | None, maximum: float = 100.0) -> NumericRange:
        fields: dict[str, float] = {"minimum": 0.0, "maximum": maximum}
        if step is not None:
            fields["step"] = step
        return range_of(real("x", 0.0, **fields))

    def test_a_noisy_step_is_the_held_value_plus_the_step_as_decimals(self) -> None:
        numeric = self.numeric(0.01)
        held = 50.0
        for _ in range(9):
            held = numeric.snap_step(held + 0.01, held)

        assert held == 50.09  # not 50.08999999999998

    def test_stepping_down_and_by_several_steps_at_once(self) -> None:
        numeric = self.numeric(0.1)

        assert numeric.snap_step(0.30000000000000004, 0.2) == 0.3
        assert numeric.snap_step(0.2 - 0.1, 0.2) == 0.1
        assert numeric.snap_step(0.1 * 3, 0.0) == 0.3  # a held arrow steps three at a time

    def test_a_page_increment_is_recognised_too(self) -> None:
        numeric = self.numeric(0.01, maximum=100.0)  # a page is a tenth of the range: 10.0

        assert numeric.snap_step(0.07 + 10.0, 0.07) == 10.07

    def test_the_digits_of_the_held_value_are_kept_and_none_is_invented(self) -> None:
        numeric = self.numeric(0.1)

        assert numeric.snap_step(0.123 + 0.1, 0.123) == 0.223

    @pytest.mark.parametrize(
        ("value", "held", "step"),
        [
            (4e-10, 0.0, 0.01),  # typed "0.0000000004"
            (3000000.678, 3000000.0, 0.01),  # typed, at a large magnitude
            (2.0000000001, 2.0, 0.1),  # typed, a hair from the held value
            (0.123456789, 0.5, 0.01),  # dragged to a position of its own
            (5e-324, 0.5, 0.01),  # a tiny value
            (1e-22, 0.0, 1e-22 * 3),  # not a whole step of this step
        ],
    )
    def test_a_value_that_is_not_a_stepped_one_is_returned_exactly(
        self, value: float, held: float, step: float
    ) -> None:
        numeric = self.numeric(step, maximum=max(100.0, held * 2, step * 10))

        assert numeric.snap_step(value, held) == value

    def test_a_tiny_range_and_step_are_stepped_exactly(self) -> None:
        numeric = range_of(real("x", 0.0, minimum=0.0, maximum=1e-20, step=1e-22))

        assert numeric.snap_step(1e-22, 0.0) == 1e-22
        assert numeric.snap_step(3e-22, 2e-22) == 3e-22
        assert numeric.snap_step(0.0, 1e-22) == 0.0  # back to the start, a whole step

    def test_a_huge_magnitude_is_stepped_without_losing_the_step(self) -> None:
        numeric = range_of(real("x", 0.0, minimum=0.0, maximum=4e15, step=0.5))

        assert numeric.snap_step(3000000.5 + 0.5, 3000000.5) == 3000001.0

    def test_the_tolerance_is_a_millionth_of_a_step(self) -> None:
        numeric = self.numeric(1.0)

        assert numeric.snap_step(1.0 + 0.9e-6, 0.0) == 1.0  # within: it was a step
        assert numeric.snap_step(1.0 + 1.1e-6, 0.0) == 1.0 + 1.1e-6  # outside: it was not
        assert numeric.snap_step(5.0 - 0.9e-6, 0.0) == 5.0

    def test_at_most_twenty_steps_in_one_move(self) -> None:
        numeric = self.numeric(0.1)

        assert numeric.snap_step(2.0 + 1e-12, 0.0) == 2.0
        assert numeric.snap_step(2.1 + 1e-12, 0.0) == 2.1 + 1e-12

    def test_an_int_entry_is_never_snapped(self) -> None:
        numeric = range_of(number("n", 0, minimum=0, maximum=100))

        assert numeric.snap_step(5.0000001, 5) == 5.0000001


class TestParse:
    """What a user may type into a spin button, strictly."""

    def float_range(self) -> NumericRange:
        return range_of(real("x", 0.5, minimum=-10.0, maximum=10.0, step=0.01))

    def int_range(self) -> NumericRange:
        return range_of(number("n", 5, minimum=0, maximum=100))

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("0.3", 0.3),
            ("-2", -2.0),
            ("+2.5", 2.5),
            (" 0.5 ", 0.5),
            (".25", 0.25),
            ("3.", 3.0),
            ("1e-1", 0.1),
            ("1E1", 10.0),
            ("0.0000000004", 4e-10),
            ("1e-300", 1e-300),
            ("10", 10.0),
            ("-10", -10.0),
        ],
    )
    def test_numbers(self, text: str, expected: float) -> None:
        assert self.float_range().parse(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "", " ", "abc", "1,5", "0x10", "inf", "-inf", "nan", "1_0", "1__0", "--1", "+-1",
            "1.2.3", "1e", "e1", "1e+", ".", "-", "10.0001", "-10.0001", "1e999",
            "\u0661", "\u0663.\u0665",
            "\uff11", "1\u0663", "1 2", "\u00bd", "1e1_0", "True", "None",
        ],
    )  # fmt: skip
    def test_what_is_no_number_or_is_outside_the_range(self, text: str) -> None:
        assert self.float_range().parse(text) is None

    @pytest.mark.parametrize(
        ("text", "expected"), [("12", 12), ("12.0", 12), ("1e1", 10), ("0", 0)]
    )
    def test_an_int_takes_whole_numbers(self, text: str, expected: int) -> None:
        result = self.int_range().parse(text)

        assert result == expected
        assert type(result) is int

    @pytest.mark.parametrize(
        "text", ["2.5", "100.5", "-1", "101", "1e3", "1_0", "\u0661\u0662", "0x1"]
    )
    def test_an_int_refuses_the_rest(self, text: str) -> None:
        assert self.int_range().parse(text) is None
