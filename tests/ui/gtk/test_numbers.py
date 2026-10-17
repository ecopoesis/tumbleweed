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
NO_STEP = "float, narrower than any step"  # the one range with no step a widget can use
SMALLEST = sys.float_info.min

RANGES: dict[str, Json] = {
    "widest int range": number("x", 0, minimum=-HALF, maximum=HALF, step=1),
    "int range at the top": number("x", MAX_SAFE_INT, minimum=0, maximum=MAX_SAFE_INT),
    "int range at the bottom": number("x", -MAX_SAFE_INT, minimum=-MAX_SAFE_INT, maximum=0, step=7),
    "one step wide": number("x", 0, minimum=0, maximum=1),
    "huge step": number("x", 0, minimum=0, maximum=MAX_SAFE_INT, step=MAX_SAFE_INT),
    "negative range": number("x", -5, minimum=-10, maximum=-1),
    "float, smallest step": real("x", 0.0, minimum=0.0, maximum=1.0, step=SMALLEST),
    "float, tiny range and step": real("x", 1e-300, minimum=0.0, maximum=1e-290, step=SMALLEST),
    "float, fine step": real("x", 0.5, minimum=-1.0, maximum=1.0, step=1e-9),
    "float, step is the range": real("x", 0.0, minimum=0.0, maximum=3.0, step=3.0),
    "float, no step, wide": real("x", 0.0, minimum=-4e15, maximum=4e15),
    "float, no step, narrow": real("x", 0.0, minimum=0.0, maximum=1e-300),
    "float, narrower than any step": real("x", 0.0, minimum=0.0, maximum=5e-324),
    "float, narrowest with a default step": real("x", 0.0, minimum=0.0, maximum=SMALLEST),
    "float, huge bounds": real("x", 0.0, minimum=-4.5e15, maximum=4.5e15, step=0.5),
}


DRIVEN = [name for name in RANGES if name != NO_STEP]


def widget_spec(kind: str, decl: Json) -> "UiSpec":
    return spec_of({"kind": kind, "bind": "x"}, [decl])


def bounds(decl: Json) -> tuple[float, float]:
    return float(decl["minimum"]), float(decl["maximum"])  # type: ignore[arg-type]  # JSON


class TestRendering:
    @pytest.mark.parametrize("name", list(RANGES))
    @pytest.mark.parametrize("kind", ["slider", "spin", "progress"])
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
        elif name == NO_STEP and kind != "progress":
            # a range this narrow leaves no step a widget can use: a placeholder, not a crash
            assert isinstance(widget, Gtk.Label)
            assert "could not be shown" in widget.get_text()
        else:
            assert isinstance(widget, Gtk.Scale if kind == "slider" else Gtk.SpinButton)
            assert caplog.records == []
            low, high = bounds(decl)
            value = widget.get_value()
            assert math.isfinite(value)
            assert low <= value <= high

    def test_a_range_too_narrow_for_a_step_is_a_placeholder_for_a_slider_and_a_spin_only(
        self,
    ) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        decl = real("x", 0.0, minimum=0.0, maximum=5e-324)

        slider = RenderedTool(widget_spec("slider", decl)).widget
        spin = RenderedTool(widget_spec("spin", decl)).widget
        progress = RenderedTool(widget_spec("progress", decl)).widget

        assert isinstance(slider, Gtk.Label)
        assert isinstance(spin, Gtk.Label)
        assert isinstance(progress, Gtk.ProgressBar)


class TestDrivenToTheEnds:
    @pytest.mark.parametrize("name", DRIVEN)
    @pytest.mark.parametrize("kind", ["slider", "spin"])
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

        with caplog.at_level(logging.ERROR):
            for position in (high, low, high, (low + high) / 2, low):
                widget.get_adjustment().set_value(position)
                value = tool.runtime.store.value(StateKey("x"))
                assert isinstance(value, int | float)
                assert math.isfinite(value)
                assert low <= value <= high
            widget.get_adjustment().set_value(1e300)  # beyond the range: clamped by the widget
            assert low <= float(tool.runtime.store.value(StateKey("x"))) <= high  # type: ignore[arg-type]  # a number

        assert caplog.records == []
        assert seen  # the widget did write

    def test_the_ranges_driven_above_are_all_the_ones_that_make_a_widget(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        placeholders = [
            name
            for name, decl in RANGES.items()
            if isinstance(RenderedTool(widget_spec("slider", decl)).widget, Gtk.Label)
        ]

        assert placeholders == [NO_STEP]
        assert [name for name in RANGES if name not in placeholders] == DRIVEN

    @pytest.mark.parametrize("name", list(RANGES))
    def test_a_patch_to_both_ends_moves_every_widget_and_the_bar_stays_in_0_to_1(
        self, name: str
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        decl = RANGES[name]
        spec = spec_of(
            {
                "kind": "box",
                "children": [
                    {"kind": "slider", "bind": "x"},
                    {"kind": "spin", "bind": "x"},
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
