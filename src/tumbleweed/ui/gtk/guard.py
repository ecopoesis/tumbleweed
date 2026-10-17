"""Keeps a spec's text entries from opening surfaces of their own (I5, #53).

A `Gtk.Text` (inside `Gtk.Entry` and `Gtk.SpinButton`) can open a popover, which is a window of its
own that clipping cannot contain: its context menu (a right click, the Menu key, Shift+F10, a long
press) and the emoji chooser (Ctrl+.). It can also start a drag, which has a drag icon, from
selected text. `harden_text` turns each off:

- the actions `menu.popup` and `misc.insert-emoji` are disabled (the keyboard routes);
- the long-press gesture is removed (the touch route);
- a click gesture in the capture phase claims a secondary button press, which denies the text's own
  gestures the sequence, so no context menu opens (the pointer route), and on any other press it
  drops the selection first, since a drag starts only from a press inside one.

Selecting and typing, and copy and paste with the shortcuts, work as before.
"""

from typing import Protocol

from tumbleweed.shell._gi import Gdk, Gtk


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


def harden_text(widget: Gtk.Editable) -> None:
    """Apply the measures above to the `Gtk.Text` inside an entry or spin button."""
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
    click.connect(
        "pressed",
        lambda gesture, _presses, _x, _y: on_press(gesture.get_current_button(), gesture, widget),
    )
    delegate.add_controller(click)
