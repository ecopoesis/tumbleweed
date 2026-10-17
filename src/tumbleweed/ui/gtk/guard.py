"""Keeps a spec's text entries from opening surfaces of their own (I5, #53).

A `Gtk.Text` (inside `Gtk.Entry` and `Gtk.SpinButton`) can open a popover, which is a window of its
own that clipping cannot contain: its context menu (a right click, the Menu key, Shift+F10, a long
press) and the emoji chooser (Ctrl+.). It can also start a drag, which has a drag icon, from
selected text. `harden_text` turns each off:

- the actions `menu.popup` and `misc.insert-emoji` are disabled (the keyboard routes);
- the long-press gesture is removed (the touch route);
- a click gesture in the capture phase claims a secondary button press, which denies the text's own
  gestures the sequence, so no context menu opens (the pointer route), and on any other press it
  drops the selection first, since a drag starts only from a press inside one;
- a second, touch-only click gesture claims every touch sequence, so the selection bubble, the
  selection handles and the magnifier (popups of their own) never start; a tap still focuses the
  text, but a touch cannot place the cursor or select (a keyboard can).

Selecting and typing, and copy and paste with the shortcuts, work as before.
"""

from collections.abc import Callable
from typing import Protocol

from tumbleweed.shell._gi import Gdk, GLib, Gtk


class _Selectable(Protocol):
    def select_region(self, start_pos: int, end_pos: int) -> None: ...


class _Claimable(Protocol):
    def set_state(self, state: Gtk.EventSequenceState) -> bool: ...


def on_press(button: int, gesture: _Claimable, text: _Selectable) -> None:
    """What a press of `button` does in the capture phase: see the module docstring."""
    if button == Gdk.BUTTON_SECONDARY:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
    else:
        text.select_region(0, 0)


class _Focusable(Protocol):
    def grab_focus(self) -> bool: ...


def on_touch(gesture: _Claimable, text: _Focusable) -> None:
    """A touch on the text: claimed (so its selection bubble and handles never start), and the
    text takes the focus, as a tap would give it."""
    gesture.set_state(Gtk.EventSequenceState.CLAIMED)
    text.grab_focus()


def harden_text(widget: Gtk.Editable) -> Callable[[], None]:
    """Apply the measures above to the `Gtk.Text` inside an entry or spin button.

    Returns the function that disconnects the handlers, which refer back to the widget.
    """
    delegate = widget.get_delegate()  # the Gtk.Text inside an entry or a spin button
    if not isinstance(delegate, Gtk.Text):
        raise TypeError("the widget has no Gtk.Text to harden")
    delegate.action_set_enabled("menu.popup", False)
    delegate.action_set_enabled("misc.insert-emoji", False)
    controllers = delegate.observe_controllers()
    for position in reversed(range(controllers.get_n_items())):
        controller = controllers.get_item(position)
        if isinstance(controller, Gtk.GestureLongPress):
            delegate.remove_controller(controller)
    click = Gtk.GestureClick(button=0)  # every button
    click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    pressed = click.connect(
        "pressed",
        lambda gesture, _presses, _x, _y: on_press(gesture.get_current_button(), gesture, widget),
    )
    touch = Gtk.GestureClick(touch_only=True)
    touch.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    touched = touch.connect(
        "pressed", lambda gesture, _presses, _x, _y: on_touch(gesture, delegate)
    )
    delegate.add_controller(click)
    delegate.add_controller(touch)

    def disconnect() -> None:
        click.disconnect(pressed)
        touch.disconnect(touched)

    return disconnect


_marshal_handler_installed = False


def quiet_spin_input_marshalling() -> None:
    """Silence the one GLib warning that connecting to a spin button's `input` signal causes.

    PyGObject reads the signal's out parameter (`new_value`, a pointer) as a double, and GLib says
    so (`g_value_get_double: assertion 'G_VALUE_HOLDS_DOUBLE (value)' failed`) on every emission.
    Where warnings are errors (the tests) that stops the handler from running at all, and elsewhere
    it is noise on stderr for every number typed. The handler is for that one message in GLib's
    GObject domain; every other message goes where it went before. Done once per process.
    """
    global _marshal_handler_installed
    if _marshal_handler_installed:
        return
    _marshal_handler_installed = True

    def handler(domain: str | None, level: GLib.LogLevelFlags, message: str, _data: object) -> None:
        if "g_value_get_double" not in message:
            GLib.log_default_handler(domain, level, message, None)

    GLib.log_set_handler(
        "GLib-GObject",
        GLib.LogLevelFlags.LEVEL_WARNING | GLib.LogLevelFlags.LEVEL_CRITICAL,
        handler,
        None,
    )
