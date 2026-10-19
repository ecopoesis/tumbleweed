"""The widget for a `text_view`: multi-line plain text, read-only or two-way bound (I4, I9).

It shows a `string` entry the way an entry does, bounded the same way (I9): at most
`MAX_SHOWN_PER_TEXT` characters, then `CUT_MARK`, claimed from the tool's text budget. A text that
is cut, or that the budget has no room for, is shown read-only with a warning style, because an
edit would write the cut text over the state's value; the state keeps all of it. What the user
types or pastes is held to the entry's own cap (`insert-text` is stopped when it would pass it),
so the buffer never grows past what a layout may be asked to show.

**The text goes in as it is.** A label needs `display_text`, which puts U+200B into long runs so
that it can wrap them, and a selection copied from a label has to be given back without them
(#63). A text view wraps at any character (`WORD_CHAR`), lays out lazily (a whole line at a time,
not the whole text: 8,192 characters of one run, of CJK or of one grapheme cluster take a few
hundredths of a second), and holds the text in a buffer, so there is nothing to insert, the primary
selection and a drag carry exactly the source text, and `display_text` is not used here.

No surface (I5, #53): `harden_text_view` turns off the context menu and the emoji chooser, the
long press and the touch handles, and drops the selection a drag would start from. Text goes into
the buffer with `set_text`, which parses nothing.
"""

import logging
from typing import Final

from tumbleweed.shell._gi import Gtk
from tumbleweed.ui.gtk.budget import CUT_MARK, MAX_SHOWN_PER_TEXT, NOT_SHOWN
from tumbleweed.ui.gtk.context import BuildContext
from tumbleweed.ui.gtk.guard import harden_text_view
from tumbleweed.ui.gtk.text import ERROR_CLASS, STALE_TEXT
from tumbleweed.ui.runtime import UiEvent
from tumbleweed.ui.spec.components import TextView
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import StateValue, StringState

logger = logging.getLogger(__name__)

HEIGHT: Final = 160  # pixels the view is tall before it scrolls
WARNING_CLASS: Final = "warning"


def _text_of(buffer: Gtk.TextBuffer) -> str:
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


def make_text_view(
    component: TextView, key: StateKey, decl: StringState, ctx: BuildContext
) -> Gtk.Widget:
    """The scrolled text view for `component`, bound to the string entry `key`."""
    view = Gtk.TextView(
        wrap_mode=Gtk.WrapMode.WORD_CHAR,
        monospace=component.monospace,
        accepts_tab=False,  # Tab moves the focus on: a keyboard cannot be trapped in the view
        editable=False,
        top_margin=4,
        bottom_margin=4,
        left_margin=4,
        right_margin=4,
    )
    scrolled = Gtk.ScrolledWindow(
        hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        min_content_height=HEIGHT,
        hexpand=True,
        child=view,
    )
    scrolled.add_css_class("frame")
    buffer = view.get_buffer()
    cap = min(decl.max_length, MAX_SHOWN_PER_TEXT)  # the most the user may have in it
    harden_text_view(view, ctx.connect)
    change = (
        UiEvent(handler=component.on_change, component=component.kind, event="change", bind=key)
        if component.on_change is not None
        else None
    )
    editing = False  # inside this view's own `changed`: it shows what the user typed already
    first = True

    def current() -> tuple[StateValue, bool]:
        store = ctx.runtime.store
        return store.value(key), store.is_stale(key)

    def settle() -> None:
        # Once the user is done (the next idle turn after an edit, and when the focus leaves) the
        # claim is made again as any other: it fits, or the view is marked like the others.
        ctx.defer(view, lambda: show(*current()))

    def show(value: StateValue, stale: bool) -> None:
        text = value if isinstance(value, str) else ""
        whole = len(text) <= MAX_SHOWN_PER_TEXT and not stale
        shown = STALE_TEXT if stale else text[:MAX_SHOWN_PER_TEXT]
        if not stale and len(text) > MAX_SHOWN_PER_TEXT:
            shown += CUT_MARK
        # The view the user is in keeps its claim (it must not turn into a mark under their
        # fingers), so at most that one is over the budget, by at most its cap.
        if ctx.budget.claim(view, len(shown), keep=editing or view.is_focus()):
            ctx.budget.stop_waiting(view)
        else:
            shown, whole = NOT_SHOWN, False
            ctx.budget.wait_for_room(view, lambda: show(*current()))
        editable = component.editable and whole
        view.set_editable(editable)
        view.set_cursor_visible(True)
        for name, on in ((WARNING_CLASS, component.editable and not whole), (ERROR_CLASS, stale)):
            if on:
                view.add_css_class(name)
            else:
                view.remove_css_class(name)
        if _text_of(buffer) == shown:
            return
        if editing:
            settle()  # not inside the buffer's own `changed`: the next idle turn
            return
        with ctx.programmatic():
            position = buffer.get_property("cursor-position")
            buffer.begin_irreversible_action()  # what is put in is no edit to undo
            buffer.set_text(shown, -1)
            buffer.end_irreversible_action()
            buffer.place_cursor(buffer.get_iter_at_offset(min(position, buffer.get_char_count())))

    def update(value: StateValue, stale: bool) -> None:
        nonlocal first
        if first or editing:
            first = False
            show(value, stale)  # the first time, and what the user is typing: at once
        else:
            ctx.defer(view, lambda: show(*current()))  # the latest value, once per frame

    def on_insert(_buffer: object, _location: object, text: object, _length: object) -> None:
        # Typing and pasting are held to the cap: GTK would take any amount, and the buffer is
        # what the layout is made from. A refused insert changes nothing, so no `changed`.
        if ctx.updating or not isinstance(text, str):
            return
        if buffer.get_char_count() + len(text) > cap:
            buffer.stop_emission_by_name("insert-text")

    def on_changed(_buffer: object) -> None:
        nonlocal editing
        if ctx.updating:
            return
        editing = True
        try:
            ctx.user_input(key, _text_of(buffer), change)
        finally:
            editing = False
        settle()

    focus = Gtk.EventControllerFocus()
    view.add_controller(focus)
    ctx.connect(focus, "leave", lambda _controller: settle())
    ctx.connect(buffer, "insert-text", on_insert)
    ctx.connect(buffer, "changed", on_changed)
    ctx.on_close(lambda: ctx.budget.stop_waiting(view))
    ctx.bind(key, view, update)
    return scrolled
