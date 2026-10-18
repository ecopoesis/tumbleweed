"""A spin button that is only focused, left or activated is not an edit (reviewed in round 1).

GTK parses a spin button's text when the user leaves the field or presses Enter (`update`), and
writes the value it reads. If the text is rounded to `digits` places, a value that is not on that
grid (a default, a patch, a clamped result) comes back rounded, and the runtime would take that for
the user editing: an agent or sandbox handler fires on mere focus traversal, and the model's value
is lost. The text shows the value whole, so parsing it back changes nothing.
"""

from typing import TYPE_CHECKING

import pytest

from tests.ui.gtk.support import of_type, spec_of
from tests.ui.runtime.support import real, set_handler
from tests.ui.spec.support import Json, binary, lit_int, ref
from tumbleweed.ui.runtime import Change, UiRuntime
from tumbleweed.ui.spec.names import StateKey

if TYPE_CHECKING:
    from tumbleweed.ui.gtk import RenderedTool

pytestmark = pytest.mark.gtk

OFF_GRID = 0.123456789012345


class Sink:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, handler: object, event: object, inputs: object) -> None:
        self.calls += 1

    def send(self, handler: object, event: object, state: object) -> None:
        self.calls += 1


def tool_with(default: float) -> "tuple[RenderedTool, Sink]":
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [
        real("x", default, minimum=0.0, maximum=1.0, step=0.01),
        {"kind": "int", "key": "hits", "default": 0},
        {"kind": "string", "key": "out", "default": ""},
    ]
    handlers: list[Json] = [
        set_handler("count", hits=binary("add", ref("hits"), lit_int(1))),
        {"kind": "agent", "name": "ask"},
        {
            "kind": "sandbox",
            "name": "run",
            "module": "m",
            "function": "f",
            "inputs": ["x"],
            "outputs": ["out"],
        },
    ]
    root: Json = {"kind": "spin", "bind": "x", "on_change": "count"}
    spec = spec_of(root, state, handlers)
    sink = Sink()
    return RenderedTool(spec, runtime=UiRuntime(spec, sandbox=sink, agent=sink)), sink


def leave_and_return(spin: object) -> None:
    """What focus traversal and Enter do to a spin button: GTK's `update`."""
    from tumbleweed.shell._gi import Gtk

    assert isinstance(spin, Gtk.SpinButton)
    spin.update()
    spin.emit("activate")
    spin.update()


@pytest.mark.parametrize("how", ["patch", "default"])
def test_an_off_grid_value_survives_focus_traversal_untouched(how: str) -> None:
    from tumbleweed.shell._gi import Gtk

    tool, sink = tool_with(OFF_GRID if how == "default" else 0.5)
    if how == "patch":
        tool.runtime.patch_state({"x": OFF_GRID})
    (spin,) = of_type(tool, Gtk.SpinButton)
    origins = []
    tool.runtime.store.subscribe(lambda change: origins.append(change.origin))

    leave_and_return(spin)

    assert tool.runtime.store.value(StateKey("x")) == OFF_GRID  # not rounded to 0.12
    assert tool.runtime.store.value(StateKey("hits")) == 0  # the handler did not fire
    assert sink.calls == 0
    assert origins == []  # no write at all
    assert spin.get_value() == OFF_GRID
    assert float(spin.get_text()) == OFF_GRID  # the text reads back as the value, whole


def test_a_change_under_gtks_epsilon_reaches_the_widget() -> None:
    from tumbleweed.shell._gi import Gtk

    tool, _sink = tool_with(0.5)
    (spin,) = of_type(tool, Gtk.SpinButton)

    tool.runtime.patch_state({"x": 0.5 + 1e-12})

    assert spin.get_adjustment().get_value() == 0.5 + 1e-12
    assert float(spin.get_text()) == 0.5 + 1e-12


def test_a_real_edit_still_fires_the_handler_with_the_typed_value() -> None:
    from tumbleweed.shell._gi import Gtk

    tool, sink = tool_with(OFF_GRID)
    (spin,) = of_type(tool, Gtk.SpinButton)

    spin.set_text("0.3")  # the user types ...
    spin.update()  # ... and leaves the field

    assert tool.runtime.store.value(StateKey("x")) == 0.3
    assert tool.runtime.store.value(StateKey("hits")) == 1
    assert sink.calls == 0  # (`on_change` names the set handler)


def test_integer_spin_buttons_show_whole_numbers() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [
        {
            "kind": "int",
            "key": "n",
            "default": 9007199254740991 // 2,
            "minimum": 0,
            "maximum": 9007199254740991,
        }
    ]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "n"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    leave_and_return(spin)

    assert spin.get_text() == str(9007199254740991 // 2)
    assert tool.runtime.store.value(StateKey("n")) == 9007199254740991 // 2


@pytest.mark.parametrize(
    "typed",
    [
        "abc",
        "",
        " ",
        "1,5",
        "0x10",
        "inf",
        "-inf",
        "nan",
        "-5",
        "5",
        "1.2.3",
        "1_0",
        "1e999",
        "--1",
    ],
)
def test_text_that_is_no_usable_number_changes_nothing_and_fires_nothing(typed: str) -> None:
    from tumbleweed.shell._gi import Gtk

    tool, sink = tool_with(OFF_GRID)
    (spin,) = of_type(tool, Gtk.SpinButton)
    values = []
    tool.runtime.store.subscribe(lambda change: values.append(change.value))

    spin.set_text(typed)  # (the range is 0 to 1: "-5" and "5" are numbers, but out of it)
    spin.update()
    spin.emit("activate")

    assert tool.runtime.store.value(StateKey("x")) == OFF_GRID
    assert tool.runtime.store.value(StateKey("hits")) == 0  # the `on_change` handler did not run
    assert sink.calls == 0
    assert spin.get_adjustment().get_value() == OFF_GRID
    assert float(spin.get_text()) == OFF_GRID  # the old value's text came back


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("0.3", 0.3), (" 0.5 ", 0.5), ("1e-1", 0.1), (".25", 0.25), ("1", 1.0), ("1e-12", 1e-12)],
)
def test_a_number_that_was_typed_is_taken_exactly_even_when_it_is_tiny(
    typed: str, expected: float
) -> None:
    from tumbleweed.shell._gi import Gtk

    tool, _sink = tool_with(OFF_GRID)
    (spin,) = of_type(tool, Gtk.SpinButton)

    spin.set_text(typed)
    spin.update()

    assert tool.runtime.store.value(StateKey("x")) == expected  # 1e-12 is below GTK's epsilon
    assert tool.runtime.store.value(StateKey("hits")) == 1
    assert spin.get_adjustment().get_value() == expected
    assert float(spin.get_text()) == expected


@pytest.mark.parametrize("typed", ["2.5", "abc", "1e400", "-1", "101", "0x1"])
def test_an_int_spin_button_takes_whole_numbers_in_range_only(typed: str) -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [{"kind": "int", "key": "n", "default": 7, "minimum": 0, "maximum": 100}]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "n"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    spin.set_text(typed)
    spin.update()

    assert tool.runtime.store.value(StateKey("n")) == 7
    assert spin.get_text() == "7"


@pytest.mark.parametrize(
    ("typed", "expected"), [("12", 12), ("12.0", 12), ("1e1", 10), ("100", 100)]
)
def test_an_int_spin_button_takes_a_whole_number(typed: str, expected: int) -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [{"kind": "int", "key": "n", "default": 7, "minimum": 0, "maximum": 100}]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "n"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    spin.set_text(typed)
    spin.update()

    assert tool.runtime.store.value(StateKey("n")) == expected
    assert type(tool.runtime.store.value(StateKey("n"))) is int
    assert spin.get_text() == str(expected)


def step(spin: object, times: int, direction: str = "forward") -> None:
    """What the arrow buttons do: step by the spin button's own increment."""
    from tumbleweed.shell._gi import Gtk

    assert isinstance(spin, Gtk.SpinButton)
    kind = Gtk.SpinType.STEP_FORWARD if direction == "forward" else Gtk.SpinType.STEP_BACKWARD
    for _ in range(times):
        spin.spin(kind, spin.get_increments()[0])


def test_stepping_the_tip_calculators_bill_nine_times_gives_50_09_not_noise() -> None:
    from tests.ui.gtk.support import golden_spec
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    tool = RenderedTool(golden_spec("tip_calculator"))
    bill, _people = of_type(tool, Gtk.SpinButton)

    step(bill, 9)

    assert tool.runtime.store.value(StateKey("bill")) == 50.09  # not 50.08999999999998
    assert bill.get_text() == "50.09"
    assert bill.get_adjustment().get_value() == 50.09
    step(bill, 9, "backward")
    assert tool.runtime.store.value(StateKey("bill")) == 50.0
    assert bill.get_text() == "50.0"


def test_stepping_by_a_tenth_three_times_gives_0_3() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 0.0, minimum=0.0, maximum=10.0, step=0.1)]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "x"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    step(spin, 3)

    assert tool.runtime.store.value(StateKey("x")) == 0.3
    assert spin.get_text() == "0.3"


def test_stepping_from_a_value_off_the_grid_keeps_its_digits_without_the_noise() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 0.123, minimum=0.0, maximum=10.0, step=0.1)]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "x"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    step(spin, 3)

    assert tool.runtime.store.value(StateKey("x")) == 0.423
    assert spin.get_text() == "0.423"


def test_a_slider_stepped_by_the_keyboard_is_snapped_too() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 50.0, minimum=0.0, maximum=100.0, step=0.01)]
    tool = RenderedTool(spec_of({"kind": "slider", "bind": "x"}, state))
    (scale,) = of_type(tool, Gtk.Scale)

    for _ in range(9):
        scale.emit("move-slider", Gtk.ScrollType.STEP_FORWARD)

    assert tool.runtime.store.value(StateKey("x")) == 50.09
    assert scale.get_value() == 50.09


def test_a_typed_or_patched_value_with_more_digits_than_the_step_is_not_rounded() -> None:
    from tumbleweed.shell._gi import Gtk

    tool, _sink = tool_with(0.5)
    (spin,) = of_type(tool, Gtk.SpinButton)

    spin.set_text("0.123456789")
    spin.update()

    assert tool.runtime.store.value(StateKey("x")) == 0.123456789


def test_a_default_off_the_grid_that_is_the_minimum_shows_whole_at_once() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 0.123, minimum=0.123, maximum=5.0, step=0.1)]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "x"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    assert spin.get_text() == "0.123"  # not "0.1", which GTK would write until the first update


def test_very_small_values_are_shown_whole_and_read_back() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 1.2345e-8, minimum=0.0, maximum=1e-6, step=1e-9)]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "x"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    leave_and_return(spin)

    assert float(spin.get_text()) == 1.2345e-8
    assert tool.runtime.store.value(StateKey("x")) == 1.2345e-8


@pytest.mark.parametrize(
    ("typed", "step", "expected"),
    [
        ("0.0000000004", 0.01, 4e-10),
        ("3000000.678", 0.01, 3000000.678),
        ("2.0000000001", 0.1, 2.0000000001),
        ("0.00000000000000000001", 0.01, 1e-20),
        ("1234567.123456789", 0.5, 1234567.123456789),
    ],
)
def test_a_typed_number_is_kept_exactly_whatever_the_step(
    typed: str, step: float, expected: float
) -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 2.0, minimum=0.0, maximum=4e6, step=step)]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "x"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    spin.set_text(typed)
    spin.update()

    assert tool.runtime.store.value(StateKey("x")) == expected
    assert spin.get_adjustment().get_value() == expected
    assert float(spin.get_text()) == expected


def hits_spec(kind: str, written: float) -> "RenderedTool":
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [
        real("x", 0.5, minimum=0.0, maximum=1.0, step=0.01),
        {"kind": "int", "key": "hits", "default": 0},
    ]
    handlers: list[Json] = [
        set_handler("h", x={"kind": "float", "value": written}),
        set_handler("count", hits=binary("add", ref("hits"), lit_int(1))),
    ]
    root: Json = {"kind": kind, "bind": "x", "on_change": "h"}
    return RenderedTool(spec_of(root, state, handlers))


@pytest.mark.parametrize("written", [5e-324, 1e-320, 1e-300, 1e-100, 1e-9])
@pytest.mark.parametrize("kind", ["slider", "spin"])
@pytest.mark.timeout(30)
def test_a_handler_that_writes_a_tiny_value_back_does_not_loop(kind: str, written: float) -> None:
    import time

    from tumbleweed.shell._gi import Gtk

    tool = hits_spec(kind, written)
    widget = tool.widget
    assert isinstance(widget, Gtk.Scale | Gtk.SpinButton)
    started = time.monotonic()

    widget.get_adjustment().set_value(0.75)  # the user moves it: the handler answers with `written`

    assert time.monotonic() - started < 5
    assert tool.runtime.store.value(StateKey("x")) == written
    assert widget.get_adjustment().get_value() == written
    from tests.shell.gtk_support import pump

    pump(0.2)  # whatever GTK reports late
    assert tool.runtime.store.value(StateKey("x")) == written


def test_a_late_report_of_the_value_the_runtime_set_is_no_edit() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [
        real("x", 0.5, minimum=0.0, maximum=1.0, step=0.01),
        {"kind": "int", "key": "hits", "default": 0},
    ]
    handlers: list[Json] = [set_handler("count", hits=binary("add", ref("hits"), lit_int(1)))]
    spec = spec_of({"kind": "slider", "bind": "x", "on_change": "count"}, state, handlers)
    tool = RenderedTool(spec)
    (scale,) = of_type(tool, Gtk.Scale)

    tool.runtime.patch_state({"x": 0.3})
    changes: list[Change] = []
    tool.runtime.store.subscribe(changes.append)
    scale.emit("value-changed")  # GTK reporting it again, outside the runtime's own update

    assert tool.runtime.store.value(StateKey("hits")) == 0
    assert changes == []  # nothing was written, not even the unchanged value


def test_a_typed_number_that_looks_like_stepping_noise_is_kept_exactly() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 0.2, minimum=0.0, maximum=1.0, step=0.1)]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "x"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    spin.set_text("0.30000000000000004")  # one step up from 0.2, as floating point makes it
    spin.update()

    assert tool.runtime.store.value(StateKey("x")) == 0.30000000000000004  # not "cleaned" to 0.3
    assert float(spin.get_text()) == 0.30000000000000004


def test_a_page_increment_of_a_slider_is_snapped_like_a_step() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 0.07, minimum=0.0, maximum=100.0, step=0.01)]
    tool = RenderedTool(spec_of({"kind": "slider", "bind": "x"}, state))
    (scale,) = of_type(tool, Gtk.Scale)
    assert scale.get_adjustment().get_page_increment() == 10.0  # a tenth of the range

    scale.emit("move-slider", Gtk.ScrollType.PAGE_FORWARD)

    assert tool.runtime.store.value(StateKey("x")) == 10.07  # not 10.07000000000000028
    assert scale.get_value() == 10.07


def test_stepping_again_after_a_typed_value_is_snapped_again() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 0.9, minimum=0.0, maximum=10.0, step=0.1)]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "x"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    spin.set_text("0.2")
    spin.update()  # typed: exact, and the "typed" mark is over
    spin.spin(Gtk.SpinType.STEP_FORWARD, spin.get_increments()[0])

    assert tool.runtime.store.value(StateKey("x")) == 0.3  # not 0.30000000000000004


def test_a_slider_dragged_to_a_position_on_whole_steps_is_snapped_and_one_off_them_is_not() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 50.0, minimum=0.0, maximum=100.0, step=0.01)]
    tool = RenderedTool(spec_of({"kind": "slider", "bind": "x"}, state))
    (scale,) = of_type(tool, Gtk.Scale)

    scale.get_adjustment().set_value(50.0 + 0.01 * 3)  # 50.03 with its noise: whole steps from 50
    assert tool.runtime.store.value(StateKey("x")) == 50.03
    scale.get_adjustment().set_value(50.0 + 0.0123456789)  # a position with digits of its own
    assert tool.runtime.store.value(StateKey("x")) == 50.0123456789


def test_nothing_a_signal_handler_raises_reaches_gtk_and_the_widget_is_put_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest import mock

    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool
    from tumbleweed.ui.runtime import NumericRange

    state: list[Json] = [real("x", 0.25, minimum=0.0, maximum=1.0, step=0.01)]
    tool = RenderedTool(spec_of({"kind": "slider", "bind": "x"}, state))
    (scale,) = of_type(tool, Gtk.Scale)

    with (
        caplog.at_level("ERROR"),
        mock.patch.object(NumericRange, "snap_step", side_effect=ZeroDivisionError("boom")),
    ):
        scale.get_adjustment().set_value(0.75)  # the handler raises; nothing may come out of GTK

    assert "a signal handler of a rendered widget failed" in caplog.text
    assert tool.runtime.store.value(StateKey("x")) == 0.25  # the state was not written
    assert scale.get_value() == 0.25  # and the widget shows what the state holds


def test_a_slider_over_the_smallest_step_can_be_dragged_anywhere() -> None:
    import sys

    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 0.0, minimum=0.0, maximum=10.0, step=sys.float_info.min)]
    tool = RenderedTool(spec_of({"kind": "slider", "bind": "x"}, state))
    (scale,) = of_type(tool, Gtk.Scale)

    scale.get_adjustment().set_value(5.0)

    assert tool.runtime.store.value(StateKey("x")) == 5.0
    assert scale.get_value() == 5.0
