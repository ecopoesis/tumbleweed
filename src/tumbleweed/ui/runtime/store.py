"""The state store: typed values, computed entries, the state budget and change notifications.

The store holds the current value of every entry of a spec, starting from the defaults, and is the
only thing that changes them. Three kinds of write, all through the same rules:

- `assign` (a widget, or the assignments of a `set` handler): the value is coerced for the entry
  (`writes.coerce_for_write`: clamped, cut, int to float), checked once more with `check_value`,
  and applied unless it would grow a state that is over `MAX_STATE_TEXT_BYTES`.
- `patch` (`patch_state`, a sandbox result): `UiSpec.validate_patch` refuses the whole patch or
  returns checked values, which are applied as they are, never clamped.
- computed entries are re-evaluated, in `computed_order()`, after either, and only when an entry
  they read (directly, or through another computed entry) changed.

**Origin.** Every change says where it came from (`Origin`). Whether a handler fires is not decided
here: `UiRuntime` fires one only for an event the user caused, so a patch, a sandbox result, a
computed value and a handler's own assignments never fire one. The origin travels with the change
so that a listener, and a test, can see it.

**Notification is on assignment, not on change** (`docs/ui-spec.md` section 13): an equal image
reference written again still notifies, so that a widget reloads a rewritten file. An assignment
that is held back notifies too, with the value the entry really has, so that a widget that was
edited puts the old value back. A computed entry notifies when its value or its `stale` mark
changes.

**Stale.** A computed write that is held back by the state budget leaves the entry with its old
value, which no longer matches its expression. The entry is marked stale (and so is every computed
entry that reads a stale one) until a recomputation applies; the adapter shows the mark instead of
the old value.

Listeners are called in order, after the state is consistent, from a queue: a listener that writes
to the store has its notifications delivered after the ones already queued, not inside them, and
at most `MAX_DELIVERIES` notifications are delivered per call, so two listeners that answer each
other cannot hang the shell. A listener that raises is logged and skipped.

The store belongs to the thread that created it (`WrongThreadError` from another).
"""

import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final, final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.ui.runtime.errors import WrongThreadError
from tumbleweed.ui.runtime.interpreter import Program, evaluate
from tumbleweed.ui.runtime.values import Value
from tumbleweed.ui.runtime.writes import coerce_for_write
from tumbleweed.ui.spec import UiSpec, check_value, references
from tumbleweed.ui.spec.limits import MAX_STATE_TEXT_BYTES
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import (
    BoolState,
    FloatState,
    IntState,
    StateDecl,
    StateValue,
    StringState,
    value_text_bytes,
)

logger = logging.getLogger(__name__)

MAX_DELIVERIES: Final = 10_000  # notifications delivered by one call, whatever the listeners do


class Origin(Enum):
    """Where a change came from."""

    USER = auto()  # a widget edit
    HANDLER = auto()  # the assignments of a `set` handler
    PATCH = auto()  # `patch_state` (the model)
    SANDBOX = auto()  # the result of a sandbox handler
    COMPUTED = auto()  # a computed entry re-evaluated


@final
@dataclass(frozen=True)
class Change:
    """One notification: `key` now has `value` (what it really has, not what was asked).

    `applied` is False when the write was held back or refused, so the value is the old one.
    `stale` is True for a computed entry whose value no longer matches its expression.
    """

    key: StateKey
    value: StateValue
    origin: Origin
    applied: bool = True
    stale: bool = False


type Listener = Callable[[Change], None]


@final
class StateStore:
    """The values of one tool instance's state. See the module docstring."""

    def __init__(self, spec: UiSpec) -> None:
        self._thread = threading.get_ident()
        self._spec = spec
        self._decls = spec.declarations()
        self._values: dict[StateKey, StateValue] = {
            key: decl.default for key, decl in self._decls.items()
        }
        self._sizes = {key: value_text_bytes(value) for key, value in self._values.items()}
        self._total = sum(self._sizes.values())
        self._order = spec.computed_order()
        self._programs: dict[StateKey, Program] = {}
        self._reads: dict[StateKey, tuple[StateKey, ...]] = {}
        for key in self._order:
            decl = self._decls[key]
            if isinstance(decl, BoolState | IntState | FloatState | StringState) and decl.computed:
                self._programs[key] = Program(decl.computed)
                self._reads[key] = tuple(references(decl.computed))
        self._stale: dict[StateKey, bool] = dict.fromkeys(self._order, False)
        self._listeners: list[Listener] = []
        self._queue: deque[Change] = deque()
        self._delivering = False
        self._recompute(set(), everything=True)  # no listener yet: nothing to tell

    # --- reading -------------------------------------------------------------------------

    @property
    def spec(self) -> UiSpec:
        return self._spec

    def value(self, key: StateKey) -> StateValue:
        """The current value of an entry (`KeyError` for a key the spec does not declare)."""
        self.check_thread()
        return self._values[key]

    def snapshot(self) -> dict[StateKey, StateValue]:
        """A copy of the whole state. The values are shared, not copied: never mutate one (a
        `json` value is a list or dict, and the store replaces it, never edits it)."""
        self.check_thread()
        return dict(self._values)

    def is_stale(self, key: StateKey) -> bool:
        """Whether a computed entry's value is out of date (see the module docstring)."""
        self.check_thread()
        return self._stale.get(key, False)

    @property
    def text_bytes(self) -> int:
        """The bytes of text in the whole state, as the budget counts them."""
        return self._total

    def read(self, key: StateKey) -> object:
        """What an expression sees for `key`: its value if it is a scalar, else `False`."""
        value = self._values.get(key)
        return value if isinstance(value, bool | int | float | str) else False

    # --- listening -----------------------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call `listener` with every change from now on; returns the function that stops it."""
        self.check_thread()
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    # --- writing -------------------------------------------------------------------------

    def assign(
        self, assignments: Sequence[tuple[StateKey, Value]], *, origin: Origin
    ) -> tuple[bool, ...]:
        """Write values, in order; for each, whether it was applied.

        A key that is not declared, or is computed, is refused (a validated spec never asks).
        The computed entries that read what changed are re-evaluated once, after all the writes.
        """
        self.check_thread()
        dirty: set[StateKey] = set()
        changes: list[Change] = []
        applied: list[bool] = []
        for key, raw in assignments:
            decl = self._decls.get(key)
            if decl is None or key in self._programs:
                logger.warning("refused a write to %s: not a writable entry", key)
                applied.append(False)
                continue
            old = self._values[key]
            value = self._coerce(decl, raw)
            ok = value is not None and self._try_set(key, value)
            applied.append(ok)
            if ok and self._values[key] != old:
                dirty.add(key)
            changes.append(Change(key, self._values[key], origin, applied=ok))
        changes.extend(self._recompute(dirty))
        self._deliver(changes)
        return tuple(applied)

    def write(self, key: StateKey, value: Value, *, origin: Origin) -> bool:
        """`assign` of one value."""
        return self.assign([(key, value)], origin=origin)[0]

    def patch(
        self,
        patch: Mapping[str, object],
        *,
        origin: Origin = Origin.PATCH,
        only: Sequence[StateKey] | None = None,
    ) -> None:
        """Apply a patch of raw JSON values, whole or not at all (`SpecError` says why not).

        `only` limits the keys, for a sandbox handler's declared outputs. The values are checked
        by `check_value`, never clamped, and the budget is checked by `validate_patch` against
        the state as it is now.
        """
        self.check_thread()
        checked = self._spec.validate_patch(patch, current=self._values, only=only)
        dirty: set[StateKey] = set()
        changes: list[Change] = []
        for key, value in checked.items():
            if self._values[key] != value:
                dirty.add(key)
            self._values[key] = value
            size = value_text_bytes(value)
            self._total += size - self._sizes[key]
            self._sizes[key] = size
            changes.append(Change(key, value, origin))
        changes.extend(self._recompute(dirty))
        self._deliver(changes)

    # --- internals -----------------------------------------------------------------------

    def check_thread(self) -> None:
        """Raise `WrongThreadError` unless called on the thread that created the store."""
        if threading.get_ident() != self._thread:
            raise WrongThreadError(
                "the state store belongs to the thread that created it "
                "(marshal the call, for example with GLib.idle_add)"
            )

    @staticmethod
    def _coerce(decl: StateDecl, raw: Value) -> StateValue | None:
        value = coerce_for_write(decl, raw)
        if value is None:
            return None
        try:
            return check_value(decl, value)  # the last word: a bug above cannot store a bad value
        except InvalidValueError:
            logger.warning("refused a value for %s that check_value rejects", decl.key)
            return None

    def _try_set(self, key: StateKey, value: StateValue) -> bool:
        """Store `value` unless it grows a state that would be over budget (section 7)."""
        size = value_text_bytes(value)
        old = self._sizes[key]
        after = self._total - old + size
        if after > MAX_STATE_TEXT_BYTES and size > old:
            return False
        self._values[key] = value
        self._sizes[key] = size
        self._total = after
        return True

    def _recompute(self, dirty: set[StateKey], *, everything: bool = False) -> list[Change]:
        """Re-evaluate the computed entries that read something in `dirty`, in order."""
        changes: list[Change] = []
        for key in self._order:
            reads = self._reads[key]
            if not everything and dirty.isdisjoint(reads):
                continue
            decl = self._decls[key]
            raw = evaluate(self._programs[key], self.read)
            value = self._coerce(decl, raw)
            old = self._values[key]
            held_back = value is None or not self._try_set(key, value)
            stale = held_back or any(self._stale.get(read, False) for read in reads)
            if self._values[key] != old or stale != self._stale[key]:
                dirty.add(key)
                changes.append(
                    Change(
                        key, self._values[key], Origin.COMPUTED, applied=not held_back, stale=stale
                    )
                )
            self._stale[key] = stale
        return changes

    def _deliver(self, changes: Sequence[Change]) -> None:
        self._queue.extend(changes)
        if self._delivering:
            return  # the call that is delivering will get to them, in order
        self._delivering = True
        try:
            delivered = 0
            while self._queue:
                if delivered >= MAX_DELIVERIES:
                    logger.error(
                        "dropped %d notifications: listeners keep writing", len(self._queue)
                    )
                    self._queue.clear()
                    break
                change = self._queue.popleft()
                delivered += 1
                for listener in tuple(self._listeners):
                    try:
                        listener(change)
                    except Exception:  # one widget's bug must not stop the others
                        logger.exception("a state listener failed on %s", change.key)
        finally:
            self._delivering = False
