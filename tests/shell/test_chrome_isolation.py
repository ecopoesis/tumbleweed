"""The reserved trusted-chrome region cannot be overlapped by the canvas (I5, S02 acceptance).

Three layers, each with a test that would fail if the layer were removed:

1. Structure: the chrome region is a sibling of the canvas in a plain box, not inside it, over
   it or on an overlay, so the two have separate allocations that do not intersect.
2. Content cannot grow the layout: a canvas widget that asks for 6000 x 6000 pixels does not make
   the window bigger or move the chrome or the status strip.
3. Content cannot draw or take input outside the canvas: a hostile widget that paints, and claims
   clicks on, an area far larger than itself leaves the chrome's pixels alone, and a click on the
   chrome lands on the chrome.

They use real layout and real rendering (a window presented on the display), not a model of it.
"""

import pytest

from tests.shell.gtk_support import (
    Shell,
    ancestor_of,
    pump,
    pump_until,
    render,
    solid,
)

pytestmark = pytest.mark.gtk

BLUE = (0, 0, 255)  # what the chrome shows
RED = (255, 0, 0)  # what the hostile canvas content paints
CHROME_HEIGHT = 40


def bounds(widget: object, relative_to: object) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) of `widget` in the coordinates of `relative_to`."""
    from tumbleweed.shell._gi import Gtk

    assert isinstance(widget, Gtk.Widget)
    assert isinstance(relative_to, Gtk.Widget)
    found, rect = widget.compute_bounds(relative_to)
    assert found
    return (
        rect.get_x(),
        rect.get_y(),
        rect.get_x() + rect.get_width(),
        rect.get_y() + rect.get_height(),
    )


def overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def laid_out(shell: Shell) -> None:
    shell.window.present()
    assert pump_until(lambda: shell.window.get_mapped() and shell.window.canvas.get_width() > 0), (
        "the window was never laid out"
    )
    pump(0.2)


def test_the_regions_are_siblings_in_a_plain_box_not_one_inside_the_other(
    echo_shell: Shell,
) -> None:
    from tumbleweed.shell._gi import Gtk

    window = echo_shell.window

    assert not window.chrome.is_ancestor(window.canvas)
    assert not window.canvas.is_ancestor(window.chrome)
    for widget in (window.chrome, window.canvas):
        for kind in (Gtk.Overlay, Gtk.Fixed):
            assert ancestor_of(widget, kind) is None, f"{kind.__name__} above {widget}"
    # They meet in one vertical box, with the chrome first.
    root = window.chrome.get_parent()
    assert isinstance(root, Gtk.Box)
    assert root.get_orientation() == Gtk.Orientation.VERTICAL
    assert window.canvas.is_ancestor(root)
    assert root.get_first_child() is window.chrome


def test_with_content_in_both_the_allocations_are_disjoint_and_the_chrome_is_on_top(
    echo_shell: Shell,
) -> None:
    window = echo_shell.window
    window.chrome.append(solid((0.0, 0.0, 1.0), size=(10, CHROME_HEIGHT)))
    window.canvas.set_content(solid((1.0, 0.0, 0.0), size=(300, 200)))
    laid_out(echo_shell)

    chrome = bounds(window.chrome, window)
    canvas = bounds(window.canvas, window)
    status = bounds(window.status_strip, window)

    assert chrome[3] - chrome[1] >= CHROME_HEIGHT
    assert not overlap(chrome, canvas)
    assert not overlap(chrome, status)
    assert chrome[3] <= canvas[1]  # the chrome ends where the canvas begins
    assert chrome[1] == 0  # and sits at the very top


def test_content_that_asks_for_a_huge_size_does_not_grow_the_window_or_move_the_chrome(
    echo_shell: Shell,
) -> None:
    window = echo_shell.window
    window.chrome.append(solid((0.0, 0.0, 1.0), size=(10, CHROME_HEIGHT)))
    laid_out(echo_shell)
    size_before = (window.get_width(), window.get_height())
    chrome_before = bounds(window.chrome, window)
    status_before = bounds(window.status_strip, window)
    hostile = solid((1.0, 0.0, 0.0), size=(6000, 6000))

    window.canvas.set_content(hostile)

    assert pump_until(lambda: hostile.get_width() > 0)  # it was laid out, at its full size
    pump(0.3)
    assert (window.get_width(), window.get_height()) == size_before
    assert bounds(window.chrome, window) == chrome_before
    assert bounds(window.status_strip, window) == status_before
    assert bounds(window.status_strip, window)[3] <= window.get_height()  # still inside


def test_content_that_paints_far_outside_itself_leaves_the_chrome_and_the_panel_alone(
    echo_shell: Shell,
) -> None:
    window = echo_shell.window
    window.chrome.append(solid((0.0, 0.0, 1.0), size=(10, CHROME_HEIGHT)))
    hostile = solid((1.0, 0.0, 0.0), leak=True, size=(100, 100))
    window.canvas.set_content(hostile)
    laid_out(echo_shell)
    pixels = render(window)
    chrome = bounds(window.chrome, window)
    canvas = bounds(window.canvas, window)
    panel = bounds(window.chat_panel, window)
    status = bounds(window.status_strip, window)

    # The hostile widget does paint, inside the canvas: the check below is not vacuous.
    assert pixels.at(canvas[0] + 5, canvas[1] + 5) == RED
    assert pixels.at(canvas[2] - 5, canvas[3] - 5) == RED
    # And nowhere else: not over the chrome, nor beside the canvas, nor below it.
    assert pixels.at((chrome[0] + chrome[2]) / 2, (chrome[1] + chrome[3]) / 2) == BLUE
    assert pixels.at(chrome[0] + 20, chrome[1] + 10) == BLUE  # inside the window's rounded corner
    assert pixels.at(chrome[2] - 20, chrome[3] - 3) == BLUE
    assert pixels.at(panel[0] + 5, (panel[1] + panel[3]) / 2) != RED
    assert pixels.at(status[0] + 2, (status[1] + status[3]) / 2) != RED


def test_a_click_on_the_chrome_lands_on_the_chrome_not_on_canvas_content(
    echo_shell: Shell,
) -> None:
    from tumbleweed.shell._gi import Gtk

    window = echo_shell.window
    prompt = solid((0.0, 0.0, 1.0), size=(10, CHROME_HEIGHT))
    window.chrome.append(prompt)
    window.canvas.set_content(solid((1.0, 0.0, 0.0), leak=True, size=(100, 100)))
    laid_out(echo_shell)
    left, top, right, bottom = bounds(window.chrome, window)

    picked = window.pick((left + right) / 2, (top + bottom) / 2, Gtk.PickFlags.DEFAULT)

    assert picked is prompt


def test_the_canvas_clips_its_own_content(echo_shell: Shell) -> None:
    from tumbleweed.shell._gi import Gtk

    canvas = echo_shell.window.canvas

    assert canvas.get_overflow() == Gtk.Overflow.HIDDEN
    canvas.set_content(solid((1.0, 0.0, 0.0)))
    scrolled = ancestor_of(canvas.content or canvas, Gtk.ScrolledWindow)
    assert scrolled is not None
    assert scrolled.get_overflow() == Gtk.Overflow.HIDDEN
