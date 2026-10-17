"""`UiRuntime`: a spec's state and handlers, the part of the renderer that needs no display.

It owns a `StateStore` and runs what a user does to the widgets:

- `user_input`: an edit of a bound widget. The value is written to the store (origin `USER`), and,
  if the write was applied and the component names an `on_change` handler, the handler fires.
- `user_event`: a click or an Enter. The handler fires.
- `patch_state`: the model's change of state. It changes values and recomputes computed entries,
  and **never fires a handler** (`docs/ui-spec.md` section 9): there is no path from here to
  `_fire`, and a handler's own assignments, a computed value and a sandbox result are not user
  events either, so handlers cannot trigger one another.

A `set` handler evaluates every assignment against the state as it was when the event fired, then
applies them together; the computed entries are recomputed once. `sandbox` and `agent` handlers go
to the sinks given at construction (S20, S12) and are reported as `NO_SINK` without one.

Like the store, the runtime belongs to the thread that created it.
"""

import logging
from collections.abc import Mapping
from typing import assert_never, final

from tumbleweed.ui.runtime.events import AgentSink, Dispatch, SandboxSink, UiEvent
from tumbleweed.ui.runtime.interpreter import Program, evaluate
from tumbleweed.ui.runtime.store import Origin, StateStore
from tumbleweed.ui.runtime.values import Value
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.handlers import AgentHandler, Handler, SandboxHandler, SetHandler
from tumbleweed.ui.spec.names import HandlerName, StateKey

logger = logging.getLogger(__name__)


@final
class UiRuntime:
    """The state and the handlers of one rendered spec."""

    def __init__(
        self,
        spec: UiSpec,
        *,
        sandbox: SandboxSink | None = None,
        agent: AgentSink | None = None,
    ) -> None:
        self.store = StateStore(spec)
        self._handlers: dict[HandlerName, Handler] = spec.handlers_by_name()
        self._programs: dict[HandlerName, tuple[tuple[StateKey, Program], ...]] = {
            handler.name: tuple(
                (assignment.target, Program(assignment.value)) for assignment in handler.assignments
            )
            for handler in spec.handlers
            if isinstance(handler, SetHandler)
        }
        self._sandbox = sandbox
        self._agent = agent

    @property
    def spec(self) -> UiSpec:
        return self.store.spec

    def user_input(self, key: StateKey, value: Value, *, event: UiEvent | None = None) -> Dispatch:
        """The user edited the widget bound to `key`: write `value`, then fire `event`'s handler.

        A write that is held back (it would grow a state that is over budget) fires nothing.
        """
        if not self.store.write(key, value, origin=Origin.USER):
            return Dispatch.HELD_BACK
        return Dispatch.IGNORED if event is None else self._fire(event)

    def user_event(self, event: UiEvent) -> Dispatch:
        """The user clicked or pressed Enter: fire the handler the event names."""
        self.store.check_thread()
        return self._fire(event)

    def patch_state(self, patch: Mapping[str, object]) -> None:
        """Apply the model's patch (`SpecError` if it is refused). Fires no handler."""
        self.store.patch(patch, origin=Origin.PATCH)

    def _fire(self, event: UiEvent) -> Dispatch:
        handler = self._handlers.get(event.handler)
        if handler is None:
            logger.warning("an event names a handler the spec does not have")
            return Dispatch.IGNORED
        try:
            match handler:
                case SetHandler():
                    self._run_set(handler)
                    return Dispatch.RAN
                case SandboxHandler():
                    return self._run_sandbox(handler, event)
                case AgentHandler():
                    return self._run_agent(handler, event)
                case _ as unreachable:
                    assert_never(unreachable)
        except Exception:
            # A sink is another component's code, and a UI event must never take the shell down.
            logger.exception("handler %s failed", event.handler)
            return Dispatch.FAILED

    def _run_set(self, handler: SetHandler) -> None:
        store = self.store
        # All the values first, against the state as it is now; only then the writes.
        values = [
            (target, evaluate(program, store.read))
            for target, program in self._programs[handler.name]
        ]
        store.assign(values, origin=Origin.HANDLER)

    def _run_sandbox(self, handler: SandboxHandler, event: UiEvent) -> Dispatch:
        if self._sandbox is None:
            return Dispatch.NO_SINK
        inputs = {key: self.store.value(key) for key in handler.inputs}
        self._sandbox.run(handler, event, inputs)
        return Dispatch.SENT

    def _run_agent(self, handler: AgentHandler, event: UiEvent) -> Dispatch:
        if self._agent is None:
            return Dispatch.NO_SINK
        self._agent.send(handler, event, self.store.snapshot())
        return Dispatch.SENT
