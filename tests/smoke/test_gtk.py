"""A headless GTK widget can be created (S01 acceptance), on macOS natively or under weston."""

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402  (PyGObject needs require_version first)

pytestmark = pytest.mark.gtk


def test_a_widget_tree_is_created_on_the_display() -> None:
    display = Gdk.Display.get_default()
    assert display is not None

    label = Gtk.Label(label="tumbleweed")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(label)
    window = Gtk.Window(child=box)
    try:
        # Realizing creates the native surface without showing it: no window flashes up on a
        # developer's screen, and a missing or broken display still fails here.
        window.realize()
        GLib.MainContext.default().iteration(may_block=False)

        assert label.get_text() == "tumbleweed"
        assert window.get_child() is box
        assert window.get_surface() is not None
        print(f"created a Gtk.Window with a Gtk.Label on {type(display).__name__}")  # noqa: T201
    finally:
        window.destroy()
