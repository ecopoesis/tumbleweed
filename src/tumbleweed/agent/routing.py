"""Values over connections: a producer's output entry feeds a consumer's input entry (S25, D31 (4)).

The broker decides which connections exist (`connect_tools`) and the router only follows its list:
`set_connections(task, infos)` is the listing the broker gave for one consumer task, and nothing the
router does can make a connection. A connection is **active** while both of its instances are on
the canvas; while it is, every applied change of the producer's output entry is written to the
consumer's input entry through `UiRuntime.connection_input` (whole or not at all, validated like a
patch), and the consumer's `sandbox` handlers that read that input run with the trigger
`connection`. The graph is acyclic (the broker refuses a cycle), so a value moves along each edge
once per change and propagation ends; the store's own delivery cap stays the backstop.

**Bounds (I9).** A value over `MAX_ROUTED_VALUE_BYTES` as JSON is dropped, and the consumer's card
says so in one line of ours; so is a value the consumer's spec does not accept. When a connection
becomes active the producer's current value is sent at once, so that a tool that already has an
output is not waiting for it to change.

No GTK and no broker: the canvas supplies `Endpoint`s and the listing, on the main thread.
"""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, final

from pydantic import JsonValue

from tumbleweed.agent.canvas_events import json_of
from tumbleweed.core.ids import ConnectionId, TaskId, ToolInstanceId
from tumbleweed.ipc.connection_messages import ConnectionInfo
from tumbleweed.ui.runtime import Change
from tumbleweed.ui.spec import SpecError
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import StateValue

logger = logging.getLogger(__name__)

MAX_ROUTED_VALUE_BYTES: Final = 64 * 1024
"""The largest value, as JSON, that a connection passes on."""
MAX_ENDED_REMEMBERED: Final = 1024
"""How many ended tasks the router remembers (the oldest is forgotten)."""
TOO_LARGE_NOTICE: Final = "A value from a connected tool was too large to pass on."
NOT_ACCEPTED_NOTICE: Final = "A value from a connected tool is not one this tool accepts."


class Endpoint(Protocol):
    """One tool instance on the canvas, as the router needs it. Main thread only."""

    def value(self, key: StateKey) -> StateValue: ...

    def subscribe(self, listener: Callable[[Change], None]) -> Callable[[], None]:
        """Call `listener` with every change of the instance's state; returns what stops it."""
        ...

    def receive(self, key: StateKey, value: JsonValue, connection: ConnectionId) -> None:
        """Take a value for the input `key` (`SpecError`: refused, nothing changed)."""
        ...


@final
@dataclass(eq=False)
class _Route:
    info: ConnectionInfo
    task: TaskId
    stop: Callable[[], None] | None = None


def _size(value: JsonValue) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


@final
class ConnectionRouter:
    """See the module docstring."""

    def __init__(self, notify: Callable[[ToolInstanceId, str], None] | None = None) -> None:
        self._notify = notify
        self._ends: dict[ToolInstanceId, Endpoint] = {}
        self._wanted: dict[TaskId, dict[ConnectionId, ConnectionInfo]] = {}
        self._routes: dict[ConnectionId, _Route] = {}
        self._ended: dict[TaskId, None] = {}  # tasks that ended, oldest first, bounded
        self.dropped = 0  # values not passed on (too large, or not accepted), for tests

    @property
    def active(self) -> frozenset[ConnectionId]:
        return frozenset(self._routes)

    def attach(self, instance: ToolInstanceId, endpoint: Endpoint) -> None:
        """An instance is on the canvas: the connections that were waiting for it start."""
        self._ends[instance] = endpoint
        self._reconcile()

    def detach(self, instance: ToolInstanceId) -> None:
        """An instance left the canvas: the connections that end in it stop."""
        self._ends.pop(instance, None)
        self._reconcile()

    def set_connections(self, task: TaskId, connections: Sequence[ConnectionInfo]) -> None:
        """The broker's listing for the consumer task `task`, replacing the last one. A task that
        ended is remembered: a listing that arrives late for it routes nothing."""
        if task in self._ended:
            return
        self._wanted[task] = {each.connection: each for each in connections}
        self._reconcile()

    def task_ended(self, task: TaskId) -> None:
        """A connection ends with its consumer task: routing stops (D31 (7))."""
        self._wanted.pop(task, None)
        self._ended[task] = None
        while len(self._ended) > MAX_ENDED_REMEMBERED:
            del self._ended[next(iter(self._ended))]
        self._reconcile()

    def close(self) -> None:
        self._wanted.clear()
        self._ends.clear()
        self._reconcile()

    # --- internals --------------------------------------------------------------------------

    def _reconcile(self) -> None:
        wanted = {
            connection: (task, info)
            for task, group in self._wanted.items()
            for connection, info in group.items()
            if info.from_instance in self._ends and info.to_instance in self._ends
        }
        for connection in [each for each in self._routes if each not in wanted]:
            route = self._routes.pop(connection)
            if route.stop is not None:
                route.stop()
        for connection, (task, info) in wanted.items():
            if connection not in self._routes:
                self._start(_Route(info, task))

    def _start(self, route: _Route) -> None:
        info = route.info
        producer = self._ends[info.from_instance]
        self._routes[info.connection] = route
        key = info.from_port

        def changed(change: Change) -> None:
            if change.key == key and change.applied and not change.stale:
                self._pass_on(info, change.value)

        route.stop = producer.subscribe(changed)
        self._pass_on(info, producer.value(key))  # what the producer holds now

    def _pass_on(self, info: ConnectionInfo, value: StateValue) -> None:
        consumer = self._ends.get(info.to_instance)
        if consumer is None:
            return
        raw = json_of(value)
        if _size(raw) > MAX_ROUTED_VALUE_BYTES:
            self._drop(info, TOO_LARGE_NOTICE)
            return
        try:
            consumer.receive(info.to_port, raw, info.connection)
        except SpecError:
            self._drop(info, NOT_ACCEPTED_NOTICE)

    def _drop(self, info: ConnectionInfo, notice: str) -> None:
        self.dropped += 1
        if self._notify is not None:
            try:
                self._notify(info.to_instance, notice)
            except Exception:  # a UI path never takes the shell down
                logger.exception("showing a dropped value failed")
