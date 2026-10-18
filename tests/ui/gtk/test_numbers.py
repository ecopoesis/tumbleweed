"""Sliders, spin buttons and progress bars at the extremes of what a spec may say (#10, S09).

A spec number is a double, a range may be as wide as 2**53 - 1 and a step as small as the smallest
normal float. Each widget is built over such a range, driven to both ends from the widget and from
a patch, and must neither raise, nor show a value outside its range, nor write the state a value
outside the entry's bounds, nor take noticeable time.
"""

import logging
import math
import sys
import time
from typing import TYPE_CHECKING

import pytest

from tests.ui.gtk.support import of_type, spec_of
from tests.ui.runtime.support import number, real
from tests.ui.spec.support import Json
from tumbleweed.ui.spec.limits import MAX_SAFE_INT
from tumbleweed.ui.spec.names import StateKey

if TYPE_CHECKING:
    from tumbleweed.ui.spec import UiSpec

pytestmark = pytest.mark.gtk

HALF = MAX_SAFE_INT // 2
SMALLEST = sys.float_info.min

RANGES: dict[str, Json] = {
    "widest int range": number("x", 0, minimum=-HALF, maximum=HALF, step=1),
    "int range at the top": number("x", MAX_SAFE_INT, minimum=0, maximum=MAX_SAFE_INT),
    "int range at the bottom": number("x", -MAX_SAFE_INT, minimum=-MAX_SAFE_INT, maximum=0, step=7),
    "one step wide": number("x", 0, minimum=0, maximum=1),
    "huge step": number("x", 0, minimum=0, maximum=MAX_SAFE_INT, step=MAX_SAFE_INT),
    "negative range": number("x", -5, minimum=-10, maximum=-1),
    "float, smallest step": real("x", 0.0, minimum=0.0, maximum=1.0, step=SMALLEST),
    "float, 1e-20 wide, step 1e-22": real("x", 0.0, minimum=0.0, maximum=1e-20, step=1e-22),
    "float, 1e-20 wide, no step": real("x", 0.0, minimum=0.0, maximum=1e-20),
    "float, 10 wide, smallest step": real("x", 5.0, minimum=0.0, maximum=10.0, step=SMALLEST),
    "float, tiny range and step": real("x", 1e-300, minimum=0.0, maximum=1e-290, step=SMALLEST),
    "float, fine step": real("x", 0.5, minimum=-1.0, maximum=1.0, step=1e-9),
    "float, step is the range": real("x", 0.0, minimum=0.0, maximum=3.0, step=3.0),
    "float, no step, wide": real("x", 0.0, minimum=-4e15, maximum=4e15),
    "float, no step, narrow": real("x", 0.0, minimum=0.0, maximum=1e-300),
    "float, narrowest with a default step": real("x", 0.0, minimum=0.0, maximum=SMALLEST),
    "float, huge bounds": real("x", 0.0, minimum=-4.5e15, maximum=4.5e15, step=0.5),
}


SPIN_REFUSED = {  # a spin button needs a step above GTK's epsilon (1e-10); these are slider-only
    "float, 10 wide, smallest step",
    "float, 1e-20 wide, step 1e-22",
    "float, 1e-20 wide, no step",
    "float, tiny range and step",
    "float, smallest step",
    "float, no step, narrow",
    "float, narrowest with a default step",
}


def cases(kinds: tuple[str, ...]) -> list[tuple[str, str]]:
    return [
        (kind, name)
        for kind in kinds
        for name in RANGES
        if not (kind == "spin" and name in SPIN_REFUSED)
    ]


def numeric_step(decl: Json) -> float:
    """The step the widget works with: the declared one, a whole number's 1, or the default."""
    from tumbleweed.ui.spec import default_float_step

    if "step" in decl:
        return float(decl["step"])  # type: ignore[arg-type]  # JSON
    if decl["kind"] == "int":
        return 1.0
    low, high = bounds(decl)
    return default_float_step(high - low)


def widget_spec(kind: str, decl: Json) -> "UiSpec":
    return spec_of({"kind": kind, "bind": "x"}, [decl])


def bounds(decl: Json) -> tuple[float, float]:
    return float(decl["minimum"]), float(decl["maximum"])  # type: ignore[arg-type]  # JSON


class TestRendering:
    @pytest.mark.parametrize(("kind", "name"), cases(("slider", "spin", "progress")))
    def test_every_widget_is_built_over_every_range_and_shows_the_default(
        self, kind: str, name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        decl = RANGES[name]
        started = time.monotonic()

        with caplog.at_level(logging.ERROR):
            tool = RenderedTool(widget_spec(kind, decl))

        assert time.monotonic() - started < 2
        widget = tool.widget
        if kind == "progress":
            assert isinstance(widget, Gtk.ProgressBar)
            assert 0.0 <= widget.get_fraction() <= 1.0
        else:
            assert isinstance(widget, Gtk.Scale if kind == "slider" else Gtk.SpinButton)
            assert caplog.records == []
            low, high = bounds(decl)
            value = widget.get_value()
            assert math.isfinite(value)
            assert low <= value <= high

    def test_a_range_too_narrow_for_a_step_never_reaches_the_renderer(self) -> None:
        # The spec refuses a slider or spin button over a float range narrower than the smallest
        # normal float that gives no step; a progress bar needs none and is built.
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool
        from tumbleweed.ui.spec import SpecError

        decl = real("x", 0.0, minimum=0.0, maximum=5e-324)

        for kind in ("slider", "spin"):
            with pytest.raises(SpecError, match="smallest normal float"):
                widget_spec(kind, decl)
        assert isinstance(RenderedTool(widget_spec("progress", decl)).widget, Gtk.ProgressBar)


class TestDrivenToTheEnds:
    @pytest.mark.parametrize(("kind", "name"), cases(("slider", "spin")))
    def test_the_widget_driven_to_both_ends_writes_values_inside_the_bounds(
        self, kind: str, name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        decl = RANGES[name]
        tool = RenderedTool(widget_spec(kind, decl))
        widget = tool.widget
        assert isinstance(widget, Gtk.Range | Gtk.SpinButton)
        low, high = bounds(decl)
        seen: list[object] = []
        tool.runtime.store.subscribe(lambda change: seen.append(change.value))

        step = numeric_step(decl)
        with caplog.at_level(logging.ERROR):
            for position in (high, low, high, (low + high) / 2, low):
                widget.get_adjustment().set_value(position)
                value = tool.runtime.store.value(StateKey("x"))
                assert isinstance(value, int | float)
                assert math.isfinite(value)
                assert low <= value <= high
                # the widget and the state really are where it was driven to (within one step):
                # a value must not collapse to 0 because it is small, nor drift because it is big
                near = max(step, abs(position) * 1e-9)  # (a step below a double's resolution is not
                # a distance a double can show: 2e-308 beside 5e-291)
                assert abs(widget.get_adjustment().get_value() - position) <= near, (position, step)
                assert abs(value - position) <= near, (position, value, step)
            widget.get_adjustment().set_value(1e300)  # beyond the range: clamped by the widget
            assert low <= float(tool.runtime.store.value(StateKey("x"))) <= high  # type: ignore[arg-type]  # a number

        assert caplog.records == []
        assert seen  # the widget did write

    @pytest.mark.parametrize("name", list(RANGES))
    def test_a_patch_to_both_ends_moves_every_widget_and_the_bar_stays_in_0_to_1(
        self, name: str
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        decl = RANGES[name]
        spins: list[Json] = [] if name in SPIN_REFUSED else [{"kind": "spin", "bind": "x"}]
        spec = spec_of(
            {
                "kind": "box",
                "children": [
                    {"kind": "slider", "bind": "x"},
                    *spins,
                    {"kind": "progress", "bind": "x"},
                ],
            },
            [decl],
        )
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(spec)
        (bar,) = of_type(tool, Gtk.ProgressBar)
        low, high = bounds(decl)
        integer = decl["kind"] == "int"

        for end in (high, low):
            tool.runtime.patch_state({"x": int(end) if integer else end})
            assert 0.0 <= bar.get_fraction() <= 1.0
            for scale in of_type(tool, Gtk.Scale):
                assert math.isfinite(scale.get_value())
            for spin in of_type(tool, Gtk.SpinButton):
                assert math.isfinite(spin.get_value())
        tool.runtime.patch_state({"x": int(high) if integer else high})
        assert bar.get_fraction() == 1.0
        tool.runtime.patch_state({"x": int(low) if integer else low})
        assert bar.get_fraction() == 0.0


class TestProgressOverAComputedEntry:
    def test_a_computed_value_beyond_its_bounds_is_clamped_before_the_bar_sees_it(self) -> None:
        from tests.ui.spec.support import binary, lit_int, ref
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [
            number("n", 1, minimum=0, maximum=10),
            {
                "kind": "int",
                "key": "p",
                "default": 0,
                "minimum": 0,
                "maximum": 100,
                "computed": binary("mul", ref("n"), lit_int(1000)),
            },
        ]
        tool = RenderedTool(
            spec_of(
                {
                    "kind": "box",
                    "children": [
                        {"kind": "progress", "bind": "p"},
                        {"kind": "slider", "bind": "n"},
                    ],
                },
                state,
            )
        )
        (bar,) = of_type(tool, Gtk.ProgressBar)
        (slider,) = of_type(tool, Gtk.Scale)

        slider.set_value(10.0)

        assert tool.runtime.store.value(StateKey("p")) == 100
        assert bar.get_fraction() == 1.0


class TestRapidInput:
    def test_a_thousand_slider_moves_are_handled_in_bounded_time_and_end_consistent(self) -> None:
        from tests.ui.gtk.support import golden_spec, labels_text
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(golden_spec("tip_calculator"))
        (slider,) = of_type(tool, Gtk.Scale)
        started = time.monotonic()

        for step in range(1000):
            slider.set_value(float(step % 51))

        assert time.monotonic() - started < 10
        assert tool.runtime.store.value(StateKey("tip_percent")) == 999 % 51
        assert labels_text(tool)[4] == "Tip (30%): $15.00"

    def test_a_thousand_edits_of_a_spin_button_and_an_entry_stay_consistent(self) -> None:
        from tests.ui.gtk.support import golden_spec
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(golden_spec("tip_calculator"))
        bill, _people = of_type(tool, Gtk.SpinButton)
        started = time.monotonic()

        for step in range(1000):
            bill.set_value(float(step))

        assert time.monotonic() - started < 10
        assert tool.runtime.store.value(StateKey("bill")) == 999.0
