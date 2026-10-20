"""Nothing that is not chrome can sit above, or take input meant for, a prompt (S18, #53, I5).

Clipping the canvas contains what is drawn in the window's own surface and nothing else: a popover,
a menu, a dialog, a tooltip are surfaces of their own, which no allocation contains. The renderer
creates none from a spec (`tumbleweed.ui.gtk.guard`); this is the second line, for whatever else
could be on screen when a prompt arrives, and for what could appear while it is there:

- everything in the window but the chrome (the canvas, the side panel with the chat, whose labels
  are selectable and so open a menu, the terminal pane and the strip's Terminal button) is made
  insensitive, so that it takes no pointer or keyboard input and opens nothing; F12 does not move
  the focus to the terminal either (`ShellWindow`). The strip's voice buttons stay: turning the
  microphone off with the pointer must always work, and nothing there can answer a prompt (I10);
- every popover under the window is popped down, and every toplevel that is not the shell's window
  is closed, when the prompt appears, and any new one while it is pending is closed at once
  (the picker's own dialog is let through, while it is open);
- the focus is taken from whatever had it (the caller puts it on Deny) and given back when the last
  prompt is gone, to where it was or, if that is gone, to the chat input.

`engage` and `release` are idempotent. Both run on the main thread.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from typing import final

from tumbleweed.shell._gi import GLib, Gtk

_log = logging.getLogger(__name__)


def _walk(widget: Gtk.Widget) -> Iterator[Gtk.Widget]:
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


@final
class PromptGuard:
    """Locks the window down while a prompt is pending."""

    def __init__(
        self,
        window: Gtk.Window,
        locked: Sequence[Gtk.Widget],
        restore_focus: Callable[[], None],
    ) -> None:
        self._window = window
        self._locked = tuple(locked)
        self._restore_focus = restore_focus
        self._engaged = False
        self._previous: list[tuple[Gtk.Widget, bool]] = []
        self._focus: Gtk.Widget | None = None
        self._toplevels = Gtk.Window.get_toplevels()
        self._watch = 0
        self.picking = False
        """True while the trusted picker's dialog is open: its toplevel is not closed."""

    @property
    def engaged(self) -> bool:
        return self._engaged

    def engage(self) -> None:
        if self._engaged:
            self.close_surfaces()
            return
        self._engaged = True
        self._focus = self._window.get_focus()
        self._previous = [(widget, widget.get_sensitive()) for widget in self._locked]
        for widget in self._locked:
            widget.set_sensitive(False)
        self.close_surfaces()
        self._watch = self._toplevels.connect("items-changed", self._on_toplevels)

    def release(self) -> None:
        if not self._engaged:
            return
        self._engaged = False
        if self._watch:
            self._toplevels.disconnect(self._watch)
            self._watch = 0
        for widget, was_sensitive in self._previous:
            widget.set_sensitive(was_sensitive)
        self._previous = []
        focus, self._focus = self._focus, None
        if focus is not None and focus.get_mapped() and focus.get_sensitive():
            self._window.set_focus(focus)
        else:
            self._restore_focus()

    def close_surfaces(self) -> None:
        """Pop down every popover under the window and close every other toplevel."""
        for widget in _walk(self._window):
            if isinstance(widget, Gtk.Popover):
                widget.popdown()
        if self.picking:
            return
        for toplevel in Gtk.Window.list_toplevels():
            if isinstance(toplevel, Gtk.Window) and toplevel is not self._window:
                toplevel.close()

    def _on_toplevels(self, model: object, position: int, _removed: int, added: int) -> None:
        if added == 0 or self.picking:
            return
        # Closed from an idle callback: closing inside the signal of the list that is changing.
        GLib.idle_add(self._close_new)

    def _close_new(self) -> bool:
        if self._engaged and not self.picking:
            self.close_surfaces()
        return GLib.SOURCE_REMOVE
