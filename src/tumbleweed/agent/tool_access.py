"""The user's own way to give a stored tool access: a button on its card (S24, D30 (8)).

A card of a tool opened from the broker's store has one button for each kind of access its manifest
declares. The press is a direct user event, so the request carries `user_event` and the tool's
binding; the broker checks the binding and shows **its own** prompt, which then offers "allow for
this tool". Nothing here decides anything and the model has no way to reach it: the request is made
only from `shell.canvas_tools` on a button press.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Literal, Protocol, final

from tumbleweed.agent.broker_link import BrokerLink, BrokerUnavailableError
from tumbleweed.agent.canvas import StoredBinding
from tumbleweed.agent.runtime import MainThreadDispatcher
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.config import PROMPT_WAIT
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.ipc.broker_messages import (
    NetworkAccess,
    PickAccess,
    RequestedAccess,
    RunOrigin,
    WireBinding,
)

if TYPE_CHECKING:
    import concurrent.futures

_log = logging.getLogger(__name__)

type ToolAccessKind = Literal["file", "folder", "network"]

REASON: Final = "You pressed the button on the tool to give it access."
"""Our words: the broker shows it as the reason of the request, with no model text in it."""
_SECONDS: Final = PROMPT_WAIT.total_seconds() + 10.0


class ToolAccess(Protocol):
    """Asks for access for a stored tool; `done` gets one plain line, on the main thread."""

    def request(
        self,
        task: TaskId,
        stored: StoredBinding,
        kind: ToolAccessKind,
        done: Callable[[str], None],
    ) -> None: ...


def _access_of(kind: ToolAccessKind) -> RequestedAccess:
    match kind:
        case "file":
            return PickAccess(kind="fs.pick", choose="file")
        case "folder":
            return PickAccess(kind="fs.pick", choose="folder")
        case "network":
            return NetworkAccess(kind="net.full")


@final
class BrokerToolAccess:
    def __init__(self, link: BrokerLink, dispatcher: MainThreadDispatcher) -> None:
        self._link = link
        self._dispatcher = dispatcher

    def request(
        self,
        task: TaskId,
        stored: StoredBinding,
        kind: ToolAccessKind,
        done: Callable[[str], None],
    ) -> None:
        origin = RunOrigin(
            trigger="user_event",
            binding=WireBinding(
                instance=stored.instance, tool=stored.tool, content_hash=stored.content_hash
            ),
        )

        async def work(client: BrokerClient) -> str:
            try:
                outcome = await client.request_grant(
                    task, RunId.new(), _access_of(kind), REASON, origin
                )
            except BrokerRefused:
                return "The tool was not given access."
            if not outcome.granted:
                return "The tool was not given access."
            if outcome.scope == "tool":
                return "This version of the tool can use that access whenever you use it."
            return "The tool was given access for this task only."

        def finished(future: "concurrent.futures.Future[str]") -> None:
            try:
                text = future.result()
            except Exception:  # noqa: BLE001  (any failure is "not given")
                _log.warning("a request for a tool's access did not complete")
                text = "The tool was not given access: the broker is not answering."
            self._dispatcher.post(lambda: done(text))

        try:
            self._link.submit(work, deadline=_SECONDS).add_done_callback(finished)
        except BrokerUnavailableError:
            self._dispatcher.post(
                lambda: done("The tool was not given access: the broker is not available.")
            )
