"""When a `sandbox` handler runs, and what becomes of its result (S20, D27 (5), I9).

`HandlerRuns` is the `SandboxSink` of one tool instance. It runs on the main thread, owns no GTK
and does no waiting: it decides when a run starts and hands it to a `HandlerExecutor`, which
reports back on the main thread.

- **Debounce and coalescing.** An event waits for the handler's `debounce_ms`, never less than
  `CHANGE_FLOOR_MS` for a `change` event (a slider fires on every position), and a newer event of
  the same handler replaces it, so a burst is one run with the newest inputs.
- **One run in flight per instance.** A handler that is due meanwhile waits, as the newest inputs
  of its name (one waiting per handler). For `H` handlers a burst of any length gives at most
  `1 + H` runs.
- **The result** is applied with `only=handler.outputs`, whole or not at all, and never fires a
  handler. A failure or a refused result is one line of ours for the card.
- **Inert and closed instances** start nothing, the run in flight is cancelled (nothing waits for
  it; the broker has no cancel yet, so it ends at its own limits), and a result is dropped.
- **Inputs** are those of the event, kept until the run starts: a run that waited behind another
  one uses what the state held when the user acted, not what a later `Reset` made it.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Final, Protocol, final

from pydantic import JsonValue

from tumbleweed.agent.canvas import StoredBinding
from tumbleweed.agent.canvas_events import Scheduler
from tumbleweed.agent.code import HandlerCall, HandlerExecutor, HandlerOutcome, failure_text
from tumbleweed.core.ids import TaskId
from tumbleweed.tools.package import CodeModule
from tumbleweed.ui.runtime.events import UiEvent
from tumbleweed.ui.spec import SpecError
from tumbleweed.ui.spec.handlers import SandboxHandler
from tumbleweed.ui.spec.names import HandlerName, StateKey
from tumbleweed.ui.spec.state import StateValue

logger = logging.getLogger(__name__)

CHANGE_FLOOR_MS: Final = 200
RUNNING_NOTICE: Final = "Running the tool's code..."
NO_TASK_NOTICE: Final = "This tool has no task to run its code in."
REFUSED_NOTICE: Final = (
    "The tool's code returned something the tool does not allow: nothing changed."
)


class RunTarget(Protocol):
    """The instance a run belongs to: where results are applied and the line is shown."""

    def apply(self, handler: SandboxHandler, values: Mapping[str, JsonValue]) -> None:
        """Apply `values` to the handler's outputs. Raises `SpecError`: refused, nothing applied."""
        ...

    def notice(self, text: str | None) -> None:
        """Show a plain line under the tool (None clears it)."""
        ...


@final
class HandlerRuns:
    """See the module docstring."""

    def __init__(
        self,
        *,
        task: TaskId | None,
        code: Sequence[CodeModule],
        executor: HandlerExecutor,
        scheduler: Scheduler,
        target: RunTarget,
        stored: StoredBinding | None = None,
    ) -> None:
        self._stored = stored
        self._task = task
        self._code = tuple(code)
        self._executor = executor
        self._scheduler = scheduler
        self._target = target
        self._latest: dict[HandlerName, tuple[SandboxHandler, Mapping[StateKey, StateValue]]] = {}
        self._timers: dict[HandlerName, Callable[[], None]] = {}
        self._ready: list[HandlerName] = []
        self._in_flight = False
        self._cancel: Callable[[], None] | None = None
        self._stopped = False
        self.started = 0  # runs handed to the executor, for tests and the log

    def run(
        self, handler: SandboxHandler, event: UiEvent, inputs: Mapping[StateKey, StateValue]
    ) -> None:
        if self._stopped:
            return
        if self._task is None:
            self._target.notice(NO_TASK_NOTICE)
            return
        name = handler.name
        self._latest[name] = (handler, inputs)
        if name in self._ready:
            return  # already due and waiting for the run in flight: it will take these inputs
        previous = self._timers.pop(name, None)
        if previous is not None:
            previous()
        delay = handler.debounce_ms
        if event.event == "change":
            delay = max(delay, CHANGE_FLOOR_MS)
        if delay == 0:
            self._due(name)
        else:
            self._timers[name] = self._scheduler.call_later(delay / 1000, partial(self._due, name))

    def stop(self) -> None:
        """The instance is inert or closed: nothing starts, and a result that arrives is dropped."""
        self._stopped = True
        for cancel_timer in self._timers.values():
            cancel_timer()
        self._timers.clear()
        self._latest.clear()
        self._ready.clear()
        cancel, self._cancel = self._cancel, None
        if cancel is not None:
            cancel()

    def _due(self, name: HandlerName) -> None:
        self._timers.pop(name, None)
        if self._stopped or name not in self._latest:
            return
        if name not in self._ready:
            self._ready.append(name)
        self._pump()

    def _pump(self) -> None:
        task = self._task
        if self._stopped or self._in_flight or not self._ready or task is None:
            return
        handler, inputs = self._latest.pop(self._ready.pop(0))
        self._in_flight = True
        self.started += 1
        self._target.notice(RUNNING_NOTICE)
        try:
            cancel = self._executor.start(
                task,
                HandlerCall(
                    str(handler.module), str(handler.function), inputs, self._code, self._stored
                ),
                partial(self._done, handler),
            )
            self._cancel = cancel
        except Exception:  # an executor is another component's code; the tool must not wedge
            logger.exception("starting a run of handler %s failed", handler.name)
            self._in_flight = False
            self._target.notice(failure_text("unavailable"))

    def _done(self, handler: SandboxHandler, outcome: HandlerOutcome) -> None:
        self._in_flight = False
        self._cancel = None
        if self._stopped:
            return
        try:
            line = self._apply(handler, outcome)
        except Exception:  # a UI path never takes the shell down
            logger.exception("applying the result of handler %s failed", handler.name)
            line = failure_text("unavailable")
        try:
            self._target.notice(line)
        except Exception:
            logger.exception("showing the result of handler %s failed", handler.name)
        self._pump()

    def _apply(self, handler: SandboxHandler, outcome: HandlerOutcome) -> str | None:
        if outcome.failure is not None:
            return failure_text(outcome.failure)
        if outcome.values is None:
            return failure_text("bad_result")
        try:
            self._target.apply(handler, outcome.values)
        except SpecError:
            return REFUSED_NOTICE
        return None
