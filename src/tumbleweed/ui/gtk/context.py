"""What every widget builder shares: the runtime, the bindings, and the guard against echoes.

A bound widget is two things. It **shows** an entry: when the store notifies a change, its update
callback puts the value into the widget, inside `programmatic()`, so that the signal GTK emits for
that change is not mistaken for the user editing it (origin tracking, at the widget end: only a
signal outside `programmatic()` becomes a user event, so a patch, a computed value or a handler's
assignment never fires a handler). And an input **edits** it: its signal handler calls
`user_input`, which writes the store and fires the component's handler.

A binding is applied once when it is made, inside the build of its node, so a widget that cannot
show its entry fails the node (and the node becomes an error placeholder). A later failure of an
update is logged and leaves the widget disabled and marked, not the shell broken.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol, final

from tumbleweed.shell._gi import GLib, Gtk
from tumbleweed.ui.gtk.budget import TextBudget
from tumbleweed.ui.gtk.text import TextBlock
from tumbleweed.ui.runtime import Change, Dispatch, UiEvent, UiRuntime, Value
from tumbleweed.ui.spec.components import Component
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import StateValue

logger = logging.getLogger(__name__)

ERROR_CLASS = "error"


type _Handler = Callable[[object], object] | Callable[[object, object], object]


class _Connectable(Protocol):
    def connect(self, detailed_signal: str, handler: _Handler, /) -> int: ...
    def disconnect(self, handler_id: int, /) -> None: ...


class UpdateWidget(Protocol):
    """Puts an entry's value into a widget; `stale` says it is out of date."""

    def __call__(self, value: StateValue, stale: bool) -> None: ...


type BuildChild = Callable[[Component, str], Gtk.Widget]


@final
@dataclass(eq=False)
class _Binding:
    key: StateKey
    widget: Gtk.Widget
    update: UpdateWidget


@final
class BuildContext:
    """Shared by the builders of one rendered tool."""

    def __init__(self, runtime: UiRuntime, build_child: BuildChild) -> None:
        self.runtime = runtime
        self.decls = runtime.spec.declarations()
        self.build_child = build_child
        self._bindings: list[_Binding] = []
        self._by_key: dict[StateKey, list[_Binding]] = {}
        self._programmatic = 0
        self.budget = TextBudget()
        self._on_close: list[Callable[[], None]] = []
        self._pending_text: dict[TextBlock, tuple[str, bool]] = {}
        self._idle = 0
        self.closed = False

    # --- lifetime ------------------------------------------------------------------------

    def connect(self, widget: _Connectable, signal: str, handler: _Handler) -> None:
        """`widget.connect`, remembered so that `close` can undo it.

        A handler refers to its widget and to this context, and a signal connection is a
        reference GC cannot see: without `close` every tool shown would stay alive.
        """
        handler_id = widget.connect(signal, handler)
        self._on_close.append(lambda: widget.disconnect(handler_id))

    def on_close(self, undo: Callable[[], None]) -> None:
        """Run `undo` when the tool is closed."""
        self._on_close.append(undo)

    def close(self) -> None:
        """Disconnect every handler, drop the bindings and cancel what was waiting for idle."""
        if self.closed:
            return
        self.closed = True
        for undo in self._on_close:
            try:
                undo()
            except Exception:
                logger.exception("could not undo a connection")
        self._on_close.clear()
        self._bindings.clear()
        self._by_key.clear()
        self._pending_text.clear()
        if self._idle:
            GLib.source_remove(self._idle)
            self._idle = 0
        self.budget.clear()

    # --- text shown ----------------------------------------------------------------------

    def show_text(self, block: TextBlock, text: str, *, stale: bool) -> None:
        """Show `text` in `block`: at once the first time, and after that on the next idle turn,
        with only the latest value kept, so that a slider drag that changes a text a hundred times
        a second lays it out once per frame, not a hundred times."""
        if not block.has_text:
            block.set_text(text, stale=stale)
            return
        self._pending_text[block] = (text, stale)
        if not self._idle:
            self._idle = GLib.idle_add(self._apply_text)

    def flush(self) -> None:
        """Show the texts that are waiting for an idle turn now."""
        if self._idle:
            GLib.source_remove(self._idle)
        self._apply_text()

    def _apply_text(self) -> bool:
        self._idle = 0
        pending, self._pending_text = self._pending_text, {}
        for block, (text, stale) in pending.items():
            try:
                block.set_text(text, stale=stale)
            except Exception:
                logger.exception("a text could not be shown")
        return GLib.SOURCE_REMOVE

    @property
    def updating(self) -> bool:
        """Whether a widget is being changed by the runtime, not by the user."""
        return self._programmatic > 0

    @contextmanager
    def programmatic(self) -> Iterator[None]:
        self._programmatic += 1
        try:
            yield
        finally:
            self._programmatic -= 1

    # --- bindings ------------------------------------------------------------------------

    def mark(self) -> int:
        """A point to `rollback` to, taken before building a node."""
        return len(self._bindings)

    def rollback(self, mark: int) -> None:
        """Forget the bindings made since `mark`: their widgets were thrown away."""
        for binding in self._bindings[mark:]:
            self._by_key[binding.key].remove(binding)
        del self._bindings[mark:]

    def bind(self, key: StateKey, widget: Gtk.Widget, update: UpdateWidget) -> None:
        """Show entry `key` in `widget` from now on, starting with its current value.

        Raises if the first update does; the caller's node then becomes a placeholder.
        """
        store = self.runtime.store
        with self.programmatic():
            update(store.value(key), store.is_stale(key))
        binding = _Binding(key, widget, update)
        self._bindings.append(binding)
        self._by_key.setdefault(key, []).append(binding)

    def on_change(self, change: Change) -> None:
        """The store's listener: update every widget bound to the changed entry."""
        for binding in tuple(self._by_key.get(change.key, ())):
            try:
                with self.programmatic():
                    binding.update(change.value, change.stale)
            except Exception:
                logger.exception("a widget could not show %s", change.key)
                binding.widget.set_sensitive(False)
                binding.widget.add_css_class(ERROR_CLASS)

    # --- events --------------------------------------------------------------------------

    def user_input(self, key: StateKey, value: Value, event: UiEvent | None) -> Dispatch | None:
        """The user edited a widget. Ignored while the runtime is the one changing it."""
        if self.updating:
            return None
        return self._guarded(lambda: self.runtime.user_input(key, value, event=event))

    def user_event(self, event: UiEvent) -> Dispatch | None:
        """The user clicked or pressed Enter."""
        if self.updating:
            return None
        return self._guarded(lambda: self.runtime.user_event(event))

    @staticmethod
    def _guarded(action: Callable[[], Dispatch]) -> Dispatch:
        try:
            return action()
        except Exception:
            # A signal handler is the boundary: the shell shows the error, it does not crash.
            logger.exception("a user event failed")
            return Dispatch.FAILED
