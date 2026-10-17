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
from tumbleweed.ui.runtime import UiRuntime
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


@pytest.mark.parametrize("typed", ["abc", "", "1e999", "nan", "inf", "--1", "0.5.5"])
def test_text_that_is_no_number_is_not_taken_for_an_edit(typed: str) -> None:
    from tumbleweed.shell._gi import Gtk

    tool, sink = tool_with(OFF_GRID)
    (spin,) = of_type(tool, Gtk.SpinButton)

    spin.set_text(typed)
    spin.update()

    value = tool.runtime.store.value(StateKey("x"))
    assert isinstance(value, float)
    assert 0.0 <= value <= 1.0  # whatever GTK made of it, the state holds a number in range
    assert sink.calls == 0


def test_very_small_and_very_large_values_are_shown_whole_and_read_back() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [
        real("x", 1e-300, minimum=0.0, maximum=1e-290, step=2.2250738585072014e-308)
    ]
    tool = RenderedTool(spec_of({"kind": "spin", "bind": "x"}, state))
    (spin,) = of_type(tool, Gtk.SpinButton)

    leave_and_return(spin)

    assert float(spin.get_text()) == 1e-300
    assert tool.runtime.store.value(StateKey("x")) == 1e-300
