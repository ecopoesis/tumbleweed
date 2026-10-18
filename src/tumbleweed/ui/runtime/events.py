"""User events and the interface through which `sandbox` and `agent` handlers are run.

A handler fires only when a user acts on a widget (`docs/ui-spec.md` section 9). The adapter turns
a click, an edit or an Enter into a `UiEvent` and gives it to the runtime; what a `set` handler does
is the runtime's own business, and what a `sandbox` or `agent` handler does is the business of a
sink, injected later (S20 runs the sandbox, S12 talks to the agent). Until a sink is given the event
is reported as `NO_SINK` and nothing happens.

A sink is called on the thread that created the runtime, with the state as it is at the event, and
must return quickly: it queues work, it does not wait for it. Sink errors are logged, and do not
reach the widget that was clicked.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal, Protocol, final

from tumbleweed.ui.spec.handlers import AgentHandler, SandboxHandler
from tumbleweed.ui.spec.names import HandlerName, StateKey
from tumbleweed.ui.spec.state import StateValue


@final
@dataclass(frozen=True)
class UiEvent:
    """What the user did: `event` on a `component` of kind ..., which names `handler`.

    `bind` is the entry the component edits, if it has one. The values are names of ours (a
    component kind, an event, a state key): none of it is text the model can choose freely beyond
    the validated names of the spec.
    """

    handler: HandlerName
    component: str
    event: Literal["click", "change", "activate"]
    bind: StateKey | None = None


class SandboxSink(Protocol):
    """Runs a `sandbox` handler (S20): `inputs` are the values of the handler's declared inputs."""

    def run(
        self, handler: SandboxHandler, event: UiEvent, inputs: Mapping[StateKey, StateValue]
    ) -> None: ...


class AgentSink(Protocol):
    """Sends an `agent` handler's event and a snapshot of the whole state to the agent (S12)."""

    def send(
        self, handler: AgentHandler, event: UiEvent, state: Mapping[StateKey, StateValue]
    ) -> None: ...


class Dispatch(Enum):
    """What became of a user event."""

    IGNORED = auto()  # the component names no handler
    HELD_BACK = auto()  # the edit was not applied (over the state budget), so nothing fired
    RAN = auto()  # a `set` handler ran
    SENT = auto()  # a `sandbox` or `agent` handler was given to its sink
    NO_SINK = auto()  # a `sandbox` or `agent` handler, and no sink is connected
    FAILED = auto()  # the handler or its sink raised; it was logged
