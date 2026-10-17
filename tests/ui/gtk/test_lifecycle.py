"""Showing tools again and again does not keep them alive (round 1: 50 tools left 50 contexts).

A signal handler refers to its widget and to the tool's context, and the context to its widgets: a
cycle through GTK that the garbage collector cannot see. `close` disconnects every handler.
"""

import gc

import pytest

from tests.shell.gtk_support import Shell, pump
from tests.ui.gtk.hostile import hostile_spec
from tests.ui.gtk.support import golden_spec

pytestmark = pytest.mark.gtk


def alive(*names: str) -> dict[str, int]:
    gc.collect()
    counts = dict.fromkeys(names, 0)
    for obj in gc.get_objects():
        kind = type(obj).__name__
        if kind in counts:
            counts[kind] += 1
    return counts


KINDS = ("BuildContext", "RenderedTool", "UiRuntime", "StateStore", "ChoiceList", "TextBlock")


@pytest.mark.parametrize("name", ["tip_calculator", "photo_adjuster", "csv_viewer", "hostile"])
def test_showing_a_spec_fifty_times_leaves_only_the_last_tool_alive(
    echo_shell: Shell, name: str
) -> None:
    canvas = echo_shell.window.canvas
    make = hostile_spec if name == "hostile" else lambda: golden_spec(name)
    canvas.show_spec(make())
    pump(0.05)
    baseline = alive(*KINDS)

    for _ in range(50):
        canvas.show_spec(make())
    pump(0.1)
    after = alive(*KINDS)

    assert after["BuildContext"] <= baseline["BuildContext"] + 1, (baseline, after)
    assert after["RenderedTool"] <= baseline["RenderedTool"] + 1, (baseline, after)
    assert after["UiRuntime"] <= baseline["UiRuntime"] + 1, (baseline, after)
    assert after["StateStore"] <= baseline["StateStore"] + 1, (baseline, after)
    # the widgets of the 50 tools are gone with them (a few may wait for a frame to be freed)
    assert after["ChoiceList"] <= baseline["ChoiceList"] + 2, (baseline, after)
    assert after["TextBlock"] <= baseline["TextBlock"] + 5, (baseline, after)


def test_clearing_the_canvas_lets_the_tool_go(echo_shell: Shell) -> None:
    canvas = echo_shell.window.canvas
    before = alive("BuildContext")["BuildContext"]
    canvas.show_spec(golden_spec("tip_calculator"))

    canvas.set_content(None)
    pump(0.1)

    assert alive("BuildContext")["BuildContext"] <= before


def test_a_closed_tool_has_no_bindings_handlers_or_pending_work() -> None:
    from tests.ui.gtk.support import of_type
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    tool = RenderedTool(golden_spec("tip_calculator"))
    (slider,) = of_type(tool, Gtk.Scale)
    tool.runtime.patch_state({"bill": 80.0})  # leaves a text update waiting

    tool.close()
    slider.set_value(40.0)  # a closed tool's widget no longer writes the state
    pump(0.1)

    assert tool.runtime.store.value("tip_percent") == 18  # type: ignore[arg-type]  # a key's text
    assert tool.widget_at("root") is None  # the widget map is dropped
