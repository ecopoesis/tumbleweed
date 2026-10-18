"""The workaround for PyGObject's spin button `input` marshalling touches nothing else (#81).

It is a `warnings` filter for one message in one category, in force while a spin button is alive:
every other GLib warning still takes PyGObject's own road, and tests that make warnings errors
keep catching real ones.
"""

import gc
import warnings
from typing import TYPE_CHECKING

import pytest

from tests.ui.gtk.support import of_type, spec_of
from tests.ui.runtime.support import real

if TYPE_CHECKING:
    from tests.ui.spec.support import Json
    from tumbleweed.ui.gtk import RenderedTool

# A hang here would be the signal mutex: the signal-based timeout cannot interrupt a thread blocked
# in C, so these tests use the thread method, which ends the run instead of wedging it.
pytestmark = [pytest.mark.gtk, pytest.mark.timeout(60, method="thread")]


def provoke_another_critical() -> None:
    """A GLib-GObject warning that has nothing to do with spin buttons.

    GLib logs it while it holds the signal mutex, PyGObject turns the log into a Python warning
    (an error here), and a garbage collection in between could dispose a GObject, which takes the
    same mutex: so the collector is off while the warning is provoked.
    """
    from tumbleweed.shell._gi import GObject, Gtk

    adjustment = Gtk.Adjustment()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        GObject.signal_handler_disconnect(adjustment, 987654)  # no such handler id
    finally:
        if was_enabled:
            gc.enable()


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


def test_a_message_that_merely_starts_like_ours_is_not_ignored() -> None:
    from tumbleweed.shell._gi import GObject
    from tumbleweed.ui.gtk.guard import _SPIN_INPUT_WARNING

    tool = spin_tool()

    own = [f for f in warnings.filters if f[2] is GObject.Warning and f[0] == "ignore"]
    pattern = own[0][1]
    assert pattern is not None
    assert pattern.match(_SPIN_INPUT_WARNING)
    assert not pattern.match(_SPIN_INPUT_WARNING + " EXTRA")
    assert not pattern.match(_SPIN_INPUT_WARNING + "\n")  # `\Z`, not `$`, which allows a newline
    tool.close()


def test_with_the_filter_in_force_the_message_plus_a_suffix_still_raises() -> None:
    from tumbleweed.shell._gi import GObject
    from tumbleweed.ui.gtk.guard import _SPIN_INPUT_WARNING

    tool = (
        spin_tool()
    )  # warnings are errors in these tests; ours is ignored, the suffixed one is not

    warnings.warn(_SPIN_INPUT_WARNING, GObject.Warning, stacklevel=1)  # ignored: no exception
    with pytest.raises(GObject.Warning):
        warnings.warn(_SPIN_INPUT_WARNING + " EXTRA", GObject.Warning, stacklevel=1)
    with pytest.raises(GObject.Warning):
        warnings.warn(_SPIN_INPUT_WARNING + "\n", GObject.Warning, stacklevel=1)
    tool.close()


def test_the_filter_is_gone_from_warnings_once_the_last_spin_is_closed() -> None:
    """In a fresh process, where no other test has left a spin button open."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    probe = """
import sys, warnings
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from tests.ui.gtk.test_marshal_filter import spin_tool
from tumbleweed.shell._gi import GObject

def present():
    return any(f[2] is GObject.Warning and f[0] == "ignore" for f in warnings.filters)

assert not present()
first, second = spin_tool(), spin_tool()
assert present()
first.close()
assert present()  # one is still alive
second.close()
assert not present()
print("ok")
"""
    result = subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
        [sys.executable, "-I", "-c", probe, str(root), str(root / "src")],
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )

    assert result.returncode == 0, result.stderr[-800:]
    assert result.stdout.strip() == "ok"
