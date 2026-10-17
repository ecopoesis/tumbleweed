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
