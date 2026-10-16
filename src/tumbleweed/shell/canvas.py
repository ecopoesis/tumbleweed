"""The main canvas: where generated tools render (S10), with an empty-state placeholder.

The canvas is the one part of the window that will hold content derived from model output, so
it is built to stay inside its own allocation (I5): the content sits in a scrolled window whose
minimum size does not follow the content (a huge tool cannot grow the window or push the chrome
and status strip out of it), and the canvas and its scrolled window clip what is drawn and picked
to their bounds. `tests/shell/test_shell_window.py` proves it with a widget that tries to paint
over the chrome region.
"""

from typing import final

from tumbleweed.shell._gi import Gtk
from tumbleweed.shell._widgets import plain_label

_EMPTY_PAGE = "empty"
_CONTENT_PAGE = "content"


def _empty_state() -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_halign(Gtk.Align.CENTER)
    box.set_valign(Gtk.Align.CENTER)
    title = plain_label("Nothing here yet", css_classes=["title-2"])
    title.set_halign(Gtk.Align.CENTER)
    hint = plain_label(
        "Ask for a tool in the chat and it will appear here.",
        wrap=True,
        css_classes=["dim-label"],
    )
    hint.set_halign(Gtk.Align.CENTER)
    box.append(title)
    box.append(hint)
    return box


@final
class CanvasView(Gtk.Box):
    """Shows the placeholder, or the one widget that was given to `set_content`."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_name("tumbleweed-canvas")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._content: Gtk.Widget | None = None
        self._scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_width=False,
            propagate_natural_height=False,
        )
        self._scrolled.set_overflow(Gtk.Overflow.HIDDEN)
        self._stack = Gtk.Stack(hexpand=True, vexpand=True)
        self._stack.add_named(_empty_state(), _EMPTY_PAGE)
        self._stack.add_named(self._scrolled, _CONTENT_PAGE)
        self.append(self._stack)

    @property
    def content(self) -> Gtk.Widget | None:
        return self._content

    @property
    def placeholder_visible(self) -> bool:
        return self._stack.get_visible_child_name() == _EMPTY_PAGE

    def set_content(self, widget: Gtk.Widget | None) -> None:
        """Show `widget` on the canvas, or the placeholder again when it is None."""
        self._content = widget
        self._scrolled.set_child(widget)
        self._stack.set_visible_child_name(_EMPTY_PAGE if widget is None else _CONTENT_PAGE)
