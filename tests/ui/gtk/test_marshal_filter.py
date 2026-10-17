"""The workaround for PyGObject's spin button `input` marshalling touches nothing else (#81).

It is a `warnings` filter for one message in one category, in force while a spin button is alive:
every other GLib warning still takes PyGObject's own road, and tests that make warnings errors
keep catching real ones.
"""

import warnings
from typing import TYPE_CHECKING

import pytest

from tests.ui.gtk.support import of_type, spec_of
from tests.ui.runtime.support import real

if TYPE_CHECKING:
    from tests.ui.spec.support import Json
    from tumbleweed.ui.gtk import RenderedTool

pytestmark = pytest.mark.gtk


def provoke_another_critical() -> None:
    """A GLib-GObject warning that has nothing to do with spin buttons."""
    from tumbleweed.shell._gi import GObject, Gtk

    GObject.signal_handler_disconnect(Gtk.Adjustment(), 987654)  # no such handler id


def spin_tool() -> "RenderedTool":
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [real("x", 0.5, minimum=0.0, maximum=1.0, step=0.01)]
    return RenderedTool(spec_of({"kind": "spin", "bind": "x"}, state))


def caught() -> list[str]:
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        provoke_another_critical()
    return [str(w.message) for w in record]


def test_the_search_for_the_other_warning_is_not_blind() -> None:
    # Before any spin button: PyGObject's own handler turns it into a Python warning.
    assert any("no handler with id" in text for text in caught())


def test_with_a_spin_button_alive_other_glib_warnings_are_still_python_warnings() -> None:
    tool = spin_tool()

    assert any("no handler with id" in text for text in caught())  # forwarded, not swallowed
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # as in the tests of this project
        with pytest.raises(SystemError) as raised:
            provoke_another_critical()
    # (PyGObject reports a warning that is an error as a SystemError, caused by the warning)
    assert "no handler with id" in str(raised.value.__cause__)
    tool.close()


def test_the_forwarded_warning_has_pygobjects_category() -> None:
    from tumbleweed.shell._gi import GObject

    tool = spin_tool()

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        provoke_another_critical()

    assert [type(w.message) for w in record] == [GObject.Warning]
    tool.close()


def test_the_spin_inputs_own_warning_is_dropped_and_the_typed_number_is_taken() -> None:
    from tumbleweed.shell._gi import Gtk

    tool = spin_tool()  # (warnings are errors in these tests: an unfiltered one would be raised)
    (spin,) = of_type(tool, Gtk.SpinButton)

    spin.set_text("0.25")
    spin.update()

    assert spin.get_adjustment().get_value() == 0.25
    tool.close()


def test_the_filter_ignores_that_message_in_that_category_only() -> None:
    from tumbleweed.shell._gi import GObject

    tool = spin_tool()
    own = [f for f in warnings.filters if f[2] is GObject.Warning and f[0] == "ignore"]
    assert len(own) == 1
    pattern = own[0][1]
    assert pattern is not None
    assert pattern.match("g_value_get_double: assertion 'G_VALUE_HOLDS_DOUBLE (value)' failed")
    assert not pattern.match("g_value_get_double: assertion 'something else' failed")
    assert not pattern.match(
        "x g_value_get_double: assertion 'G_VALUE_HOLDS_DOUBLE (value)' failed"
    )
    assert own[0][3] is None  # no module restriction, but this category only
    tool.close()


def test_closing_the_tool_takes_the_filter_out_and_does_so_once() -> None:
    from tumbleweed.shell._gi import GObject
    from tumbleweed.ui.gtk.guard import spin_filter_users

    def present() -> bool:
        return any(f[2] is GObject.Warning and f[0] == "ignore" for f in warnings.filters)

    before = spin_filter_users()
    first, second = spin_tool(), spin_tool()
    assert spin_filter_users() == before + 2
    assert present()

    first.close()
    first.close()  # twice takes nothing more away
    assert spin_filter_users() == before + 1
    assert present()
    second.close()
    assert spin_filter_users() == before
    if before == 0:  # (a tool another test left open keeps the filter in force)
        assert not present()
