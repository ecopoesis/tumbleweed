"""The UI runtime: a spec's state, expressions, bindings and handlers, with no GTK.

`tumbleweed.ui.runtime` is the part of the renderer that is unit-tested without a display and never
imports `gi` (an import-linter contract). The GTK adapter (`tumbleweed.ui.gtk`) is a thin layer over
it.

- `UiRuntime` (`runtime`): the state and handlers of one spec; user events in, notifications out.
- `StateStore` (`store`): typed values, computed entries, the state budget, change notifications
  with their origin.
- `evaluate` and `Program` (`interpreter`): the total expression interpreter of
  `docs/ui-spec.md` section 7.
- `coerce_for_write` (`writes`), `bind_of` and `NumericRange` (`bindings`), the event and sink types
  (`events`).
"""

from tumbleweed.ui.runtime.bindings import Binding, NumericRange, bind_of
from tumbleweed.ui.runtime.errors import UiRuntimeError, UnresolvedBindingError, WrongThreadError
from tumbleweed.ui.runtime.events import AgentSink, Dispatch, SandboxSink, UiEvent
from tumbleweed.ui.runtime.interpreter import Program, evaluate, format_number
from tumbleweed.ui.runtime.runtime import UiRuntime
from tumbleweed.ui.runtime.store import MAX_DELIVERIES, Change, Origin, StateStore
from tumbleweed.ui.runtime.values import Value
from tumbleweed.ui.runtime.writes import coerce_for_write

__all__ = [
    "MAX_DELIVERIES",
    "AgentSink",
    "Binding",
    "Change",
    "Dispatch",
    "NumericRange",
    "Origin",
    "Program",
    "SandboxSink",
    "StateStore",
    "UiEvent",
    "UiRuntime",
    "UiRuntimeError",
    "UnresolvedBindingError",
    "Value",
    "WrongThreadError",
    "bind_of",
    "coerce_for_write",
    "evaluate",
    "format_number",
]
