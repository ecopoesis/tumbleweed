"""The canvas hosts a rendered spec (S10), and still cannot cover the chrome (I5)."""

import pytest

from tests.shell.gtk_support import Shell, bounds, pump, pump_until, render, solid
from tests.ui.gtk.hostile import hostile_spec
from tests.ui.gtk.support import golden_spec, labels_text

pytestmark = pytest.mark.gtk

BLUE = (0, 0, 255)


def test_show_spec_renders_the_spec_on_the_canvas_in_place_of_the_placeholder(
    echo_shell: Shell,
) -> None:
    canvas = echo_shell.window.canvas
    placeholder_before = canvas.placeholder_visible

    tool = canvas.show_spec(golden_spec("tip_calculator"))

    assert placeholder_before is True
    assert canvas.placeholder_visible is False
    assert canvas.tool is tool
    assert canvas.content is tool.widget
    assert labels_text(tool)[4] == "Tip (18%): $9.00"


def test_a_second_spec_replaces_the_first_and_the_first_stops_following_its_state(
    echo_shell: Shell,
) -> None:
    canvas = echo_shell.window.canvas
    first = canvas.show_spec(golden_spec("tip_calculator"))
    before = labels_text(first)

    second = canvas.show_spec(golden_spec("tip_calculator"))
    first.runtime.patch_state({"bill": 200.0})
    second.runtime.patch_state({"bill": 300.0})

    assert canvas.tool is second
    assert canvas.content is second.widget
    assert labels_text(first) == before  # closed: its widgets are no longer updated
    assert labels_text(second)[5] == "Total: $354.00"


def test_clearing_the_canvas_closes_the_tool_and_brings_the_placeholder_back(
    echo_shell: Shell,
) -> None:
    canvas = echo_shell.window.canvas
    tool = canvas.show_spec(golden_spec("tip_calculator"))
    before = labels_text(tool)

    canvas.set_content(None)
    tool.runtime.patch_state({"bill": 99.0})

    assert canvas.placeholder_visible
    assert canvas.tool is None
    assert labels_text(tool) == before


def test_a_runtime_with_sinks_can_be_given_to_the_canvas(echo_shell: Shell) -> None:
    from tumbleweed.ui.runtime import UiRuntime

    spec = golden_spec("photo_adjuster")
    runtime = UiRuntime(spec)

    tool = echo_shell.window.canvas.show_spec(spec, runtime=runtime)

    assert tool.runtime is runtime


def test_the_hostile_spec_leaves_the_chrome_unpainted_and_clickable(echo_shell: Shell) -> None:
    window = echo_shell.window
    prompt = solid((0.0, 0.0, 1.0), size=(10, 40))
    window.chrome.append(prompt)
    window.canvas.show_spec(hostile_spec())
    window.present()
    assert pump_until(lambda: window.get_mapped() and window.canvas.get_width() > 0)
    pump(0.5)

    pixels = render(window)
    left, top, right, bottom = bounds(window.chrome, window)

    assert pixels.at((left + right) / 2, (top + bottom) / 2) == BLUE
    from tumbleweed.shell._gi import Gtk

    picked = window.pick((left + right) / 2, (top + bottom) / 2, Gtk.PickFlags.DEFAULT)
    assert picked is prompt
