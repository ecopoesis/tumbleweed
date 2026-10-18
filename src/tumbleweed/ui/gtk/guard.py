"""Keeps a spec's text entries and text views from opening surfaces of their own (I5, #53).

A `Gtk.Text` (inside `Gtk.Entry` and `Gtk.SpinButton`) and a `Gtk.TextView` can open a popover,
which is a window of its own that clipping cannot contain: its context menu (a right click, the
Menu key, Shift+F10, a long press) and the emoji chooser (Ctrl+.). It can also start a drag, which
has a drag icon, from selected text. `harden_text` turns each off:

- the actions `menu.popup` and `misc.insert-emoji` are disabled (the keyboard routes);
- the long-press gesture is removed (the touch route);
- a click gesture in the capture phase claims a secondary button press, which denies the text's own
  gestures the sequence, so no context menu opens (the pointer route), and on any other press it
  drops the selection first, since a drag starts only from a press inside one;
- a second, touch-only click gesture claims every touch sequence, so the selection bubble, the
  selection handles and the magnifier (popups of their own) never start; a tap still focuses the
  text, but a touch cannot place the cursor or select (a keyboard can).

Selecting and typing, and copy and paste with the shortcuts, work as before. A `Gtk.TextView` gets
the same measures (`harden_text_view`): its two actions are disabled, its input hints say no emoji,
a capture-phase click claims the secondary button and drops the selection on any other press, and
a touch-only click claims every touch sequence.
"""

import re
import warnings
from collections.abc import Callable
from typing import Protocol

from tumbleweed.shell._gi import Gdk, GObject, Gtk


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


type SignalHandler = (
    Callable[[object], object]
    | Callable[[object, object], object]
    | Callable[[object, object, object, object], object]
)  # what a GTK signal handler of the tool takes: the emitter, then the signal's arguments
type _Connect = Callable[[Gtk.GestureClick, str, SignalHandler], None]


def harden_text(widget: Gtk.Editable, connect: _Connect) -> None:
    """Apply the measures above to the `Gtk.Text` inside an entry or spin button.

    The handlers refer back to the widget, so they are connected with `connect` (the tool's
    `BuildContext.connect`), which disconnects them on close and keeps what they raise from GTK.
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

    def pressed(gesture: object, _presses: object, _x: object, _y: object) -> None:
        if isinstance(gesture, Gtk.GestureClick):
            on_press(gesture.get_current_button(), gesture, widget)

    def touched(gesture: object, _presses: object, _x: object, _y: object) -> None:
        if isinstance(gesture, Gtk.GestureClick):
            on_touch(gesture, delegate)

    connect(click, "pressed", pressed)
    touch = Gtk.GestureClick(touch_only=True)
    touch.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    connect(touch, "pressed", touched)
    delegate.add_controller(click)
    delegate.add_controller(touch)


class _ViewSelection:
    """What `on_press` needs of a text view: to drop its selection (put the cursor where the
    insertion point is), whatever range it is asked to select."""

    def __init__(self, view: Gtk.TextView) -> None:
        self._view = view

    def select_region(self, start_pos: int, end_pos: int) -> None:
        buffer = self._view.get_buffer()
        insert = buffer.get_iter_at_mark(buffer.get_insert())
        buffer.select_range(insert, insert)


def harden_text_view(view: Gtk.TextView, connect: _Connect) -> None:
    """Apply the measures of the module to a `Gtk.TextView`, as `harden_text` does to a `Gtk.Text`.

    The handlers refer back to the view, so they are connected with `connect` (the tool's
    `BuildContext.connect`), which disconnects them on close and keeps what they raise from GTK.
    """
    view.action_set_enabled("menu.popup", False)
    view.action_set_enabled("misc.insert-emoji", False)
    view.set_input_hints(Gtk.InputHints.NO_EMOJI)
    view.set_extra_menu(None)
    controllers = view.observe_controllers()
    for position in reversed(range(controllers.get_n_items())):
        controller = controllers.get_item(position)
        if isinstance(controller, Gtk.GestureLongPress):
            view.remove_controller(controller)
    selection = _ViewSelection(view)
    click = Gtk.GestureClick(button=0)  # every button
    click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def pressed(gesture: object, _presses: object, _x: object, _y: object) -> None:
        if isinstance(gesture, Gtk.GestureClick):
            on_press(gesture.get_current_button(), gesture, selection)

    def touched(gesture: object, _presses: object, _x: object, _y: object) -> None:
        if isinstance(gesture, Gtk.GestureClick):
            on_touch(gesture, view)

    connect(click, "pressed", pressed)
    touch = Gtk.GestureClick(touch_only=True)
    touch.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    connect(touch, "pressed", touched)
    view.add_controller(click)
    view.add_controller(touch)


_SPIN_INPUT_WARNING = "g_value_get_double: assertion 'G_VALUE_HOLDS_DOUBLE (value)' failed"
# `warnings` matches a filter's message with `re.match` (the start only): without the end anchor a
# message that merely begins with ours would be ignored too.
_SPIN_PATTERN = re.escape(_SPIN_INPUT_WARNING) + r"\Z"
_SPIN_FILTER = (
    "ignore",
    re.compile(_SPIN_PATTERN, re.IGNORECASE),
    GObject.Warning,
    None,
    0,
)
_spin_users = 0


def quiet_spin_input_marshalling() -> Callable[[], None]:
    """Silence the one GLib warning that connecting to a spin button's `input` signal causes, for
    as long as a spin button that does is alive. Returns the function that ends it (call it once,
    when the spin button's tool is closed).

    PyGObject reads the signal's out parameter (`new_value`, a pointer) as a double, and GLib says
    so on every emission: `g_value_get_double: assertion 'G_VALUE_HOLDS_DOUBLE (value)' failed`.
    PyGObject turns that into a `GObject.Warning`; where warnings are errors (the tests) the
    handler then never runs, and elsewhere it is noise on stderr for every number typed. Tracked
    upstream-side in #81.

    The workaround is a `warnings` filter, not a GLib log handler: it ignores this exact message in
    this one category and nothing else, so every other GLib warning still takes PyGObject's own
    road (a warning, or an error where warnings are errors), and it is inserted in front of the
    filters in force (a test that makes warnings errors included) and taken out again when the
    spin button's tool is closed. Filters are restored by `warnings.catch_warnings`, so call this
    where the spin button is built.
    """
    global _spin_users
    if (
        _SPIN_FILTER not in warnings.filters
    ):  # (a test's `catch_warnings` may have restored it away)
        warnings.filterwarnings("ignore", message=_SPIN_PATTERN, category=GObject.Warning)
    _spin_users += 1
    released = False

    def release() -> None:
        global _spin_users
        nonlocal released
        if released:
            return
        released = True
        _spin_users -= 1
        if _spin_users == 0 and _SPIN_FILTER in warnings.filters:
            # `warnings.filters` is a list at runtime, typed as a read-only sequence.
            warnings.filters.remove(_SPIN_FILTER)  # type: ignore[attr-defined]

    return release


def spin_filter_users() -> int:
    """How many spin buttons are alive that need the filter (for the tests)."""
    return _spin_users
