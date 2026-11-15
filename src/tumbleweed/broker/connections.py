"""The rules of tool composition (S25, D31): the fixed texts and the pure checks.

Pure: no I/O, no clock, no transport. `BrokerService` holds the records and calls these where it
has them. Every text here is fixed (none repeats what a request said), so that an answer says only
what the broker's own state decided, which is what the noninterference property of D31 (8) needs.
"""

from collections.abc import Iterable, Sequence
from typing import Final, final

from tumbleweed.core.ids import ToolInstanceId
from tumbleweed.tools.package import Port
from tumbleweed.ui.spec.names import StateKey

UNKNOWN_INSTANCE_TEXT: Final = (
    "No such tool instance can be connected here. Only a saved tool that was opened with "
    "open_tool can be connected: a tool made with show_ui must be saved with save_tool and opened "
    "with open_tool first, and a tool of an earlier task can be connected only while the "
    "conversation that opened it is the same."
)
UNKNOWN_PORT_TEXT: Final = "That tool has no such port. Use a name from its output or input ports."
COMPUTED_INPUT_TEXT: Final = "That input is computed by the tool and cannot take a value."
INPUT_TAKEN_TEXT: Final = "That input is already connected. Only one connection feeds an input."
SAME_INSTANCE_TEXT: Final = "A tool cannot be connected to itself."
CYCLE_TEXT: Final = "That connection would make a loop of tools feeding each other."
TASK_CAP_TEXT: Final = "This task has as many connections as it may. Remove one first."
INSTANCE_CAP_TEXT: Final = "That tool has as many connections as it may. Remove one first."
FAN_OUT_TEXT: Final = "That output already feeds as many inputs as it may."
INTEGRITY_TEXT: Final = "A stored tool failed its integrity check and cannot be connected."
CROSSING_DENIED_TEXT: Final = (
    "The user did not approve that connection: it would give one task both the user's files "
    "and the network. Nothing was connected."
)
CROSSING_BUSY_TEXT: Final = (
    "The user has been asked too often just now, so they were not asked about this connection. "
    "Nothing was connected; try again later."
)
CROSSING_UNAVAILABLE_TEXT: Final = (
    "The user could not be asked about that connection (nobody is there to answer), so nothing "
    "was connected."
)
NET_WITH_INPUTS_TEXT: Final = (
    "A run with the network gets no files from a connected tool. Remove the connection or run "
    "this without the network."
)
INPUTS_GONE_TEXT: Final = (
    "The files of a connected tool are no longer kept, so this run did not start. Remove the "
    "connection."
)
NO_CONNECTION_TEXT: Final = "No such connection."


def type_mismatch_text(output: str, accepts: str) -> str:
    """The text of a type mismatch; both are closed `PortType` names, never request text."""
    return f"The types differ: that output is {output} and that input takes {accepts}."


def port_named(ports: Sequence[Port], name: StateKey) -> Port | None:
    return next((port for port in ports if port.name == name), None)


@final
class Edge:
    """One connection as the cycle check sees it: values go from `producer` to `consumer`."""

    __slots__ = ("consumer", "producer")

    def __init__(self, producer: ToolInstanceId, consumer: ToolInstanceId) -> None:
        self.producer = producer
        self.consumer = consumer


def makes_cycle(edges: Iterable[Edge], producer: ToolInstanceId, consumer: ToolInstanceId) -> bool:
    """Whether adding producer -> consumer closes a loop: whether values already reach `producer`
    from `consumer`. Iterative, over at most the task's few connections."""
    onward: dict[ToolInstanceId, list[ToolInstanceId]] = {}
    for edge in edges:
        onward.setdefault(edge.producer, []).append(edge.consumer)
    seen: set[ToolInstanceId] = set()
    stack = [consumer]
    while stack:
        here = stack.pop()
        if here == producer:
            return True
        if here in seen:
            continue
        seen.add(here)
        stack.extend(onward.get(here, ()))
    return False
