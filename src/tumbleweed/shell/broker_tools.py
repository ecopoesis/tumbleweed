"""The saved-tools list's connection to the broker (S24): the shell's own link and calls.

`BrokerToolsPort` asks the broker for the stored tools, their versions and the tool grants, opens a
version under the **current task** (the same broker request, and so the same taint rules and the
same refusal text, as the model's `open_tool`), saves a copy of an older version as the newest and
revokes a tool grant, and reports on the main thread. It starts no task: a tool is opened into the
task the conversation is in. The broker's refusals are fixed text of the broker's; nothing the
model wrote reaches the sentences here, and a tool's own name is never part of one.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final, final

from tumbleweed.agent.broker_link import BrokerLink, BrokerUnavailableError
from tumbleweed.agent.runtime import MainThreadDispatcher
from tumbleweed.agent.saved_tools import FetchedTool, open_with
from tumbleweed.agent.tasks import TaskSession
from tumbleweed.agent.tools import ToolError
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.toolflow import (
    BIT_CAP_TEXT,
    LINEAGE_BOTH_TEXT,
    NOT_VISIBLE_NET_TEXT,
    SAVE_CAP_TEXT,
)
from tumbleweed.core.ids import ConnectionId, GrantId, TaskId, ToolId
from tumbleweed.core.types import ToolVersion
from tumbleweed.ipc.connection_messages import ConnectionInfo
from tumbleweed.ipc.tool_messages import ToolGrantInfo, ToolSummary
from tumbleweed.shell.viewmodel.tools import ToolsOutcome

if TYPE_CHECKING:
    import concurrent.futures

_log = logging.getLogger(__name__)

_SECONDS: Final = 20.0
NO_TASK: Final = (
    "Not opened: a tool is opened into the conversation's task, and there is none yet. Say "
    "something to the assistant first."
)
_UNAVAILABLE: Final = "Not done: the broker is not available."
REMOVED: Final = (
    "The connection was removed. What this conversation's task has seen or used stays on its "
    "record."
)


_REFUSED: Final[dict[str, str]] = {
    "denied": (
        "Not done: this tool and this conversation's task cannot be mixed, because one has seen "
        "your files and the other used the network. Ask the assistant to start a new task, then "
        "try again."
    ),
    "not_found": "Not done: that tool or version is not stored.",
    "busy": "Not done: the store is full or busy.",
    "too_large": "Not done: the tool has no version number left.",
    "unsafe": "Not done: the stored tool failed its integrity check and was not used.",
    "unknown_task": "Not done: the conversation's task has ended. Say something to the assistant.",
}
_REFUSED_OTHERWISE: Final = "Not done: the broker would not."


_LINEAGE_BOTH: Final = (
    "Not done: this tool came from a file that claims both file and network use, so it cannot be "
    "changed. Save a new tool instead."
)


_NOT_VISIBLE: Final = (
    "Not done: this conversation used the network, so it cannot use tools made in tasks that read "
    "your files. Ask the assistant to start a new task, then try again."
)
_SAVE_CAP: Final = (
    "Not done: this conversation's task has saved as many tools and versions as it may. Ask the "
    "assistant to start a new task, then try again."
)
_BIT_CAP: Final = (
    "Not done: this conversation, while it had the network, has tried to change saved tools as "
    "many times as it may. Ask the assistant to start a fresh task (it will need the network "
    "allowed again), then try again."
)
_MARKED: Final = {
    "fs": " This conversation's task now counts as having seen your files.",
    "net": " This conversation's task now counts as having used the network.",
}


def _marked(taint: tuple[str, ...]) -> str:
    return _MARKED["net" if taint == ("net",) else "fs"]


def _refusal(refusal: BrokerRefused) -> ToolsOutcome:
    """Our own sentence for a refusal: the broker's text is written for the model."""
    if refusal.message == LINEAGE_BOTH_TEXT:
        return ToolsOutcome("refused", _LINEAGE_BOTH)
    if refusal.message == NOT_VISIBLE_NET_TEXT:
        return ToolsOutcome("refused", _NOT_VISIBLE)
    if refusal.message == SAVE_CAP_TEXT:
        return ToolsOutcome("refused", _SAVE_CAP)
    if refusal.message == BIT_CAP_TEXT:
        return ToolsOutcome("refused", _BIT_CAP)
    return ToolsOutcome("refused", _REFUSED.get(refusal.reason, _REFUSED_OTHERWISE))


@final
class BrokerToolsPort:
    def __init__(
        self,
        link: BrokerLink,
        dispatcher: MainThreadDispatcher,
        tasks: TaskSession,
        show: Callable[[FetchedTool], None],
        on_connections: Callable[[TaskId, tuple[ConnectionInfo, ...]], None] | None = None,
    ) -> None:
        self._on_connections = on_connections
        self._link = link
        self._dispatcher = dispatcher
        self._tasks = tasks
        self._show = show

    def list(self, done: Callable[[tuple[ToolSummary, ...] | None], None]) -> None:
        async def work(client: BrokerClient) -> tuple[ToolSummary, ...]:
            return (await client.list_tools()).tools

        self._ask(work, done)

    def versions(
        self, tool: ToolId, done: Callable[[tuple[ToolSummary, ...] | None], None]
    ) -> None:
        async def work(client: BrokerClient) -> tuple[ToolSummary, ...]:
            return (await client.list_tool_versions(tool)).versions

        self._ask(work, done)

    def grants(self, done: Callable[[tuple[ToolGrantInfo, ...] | None], None]) -> None:
        async def work(client: BrokerClient) -> tuple[ToolGrantInfo, ...]:
            return (await client.list_tool_grants()).grants

        self._ask(work, done)

    def connections(self, done: Callable[[tuple[ConnectionInfo, ...] | None], None]) -> None:
        task = self._tasks.current
        if task is None:
            self._dispatcher.post(lambda: done(()))  # no task yet, so none
            return

        async def work(client: BrokerClient) -> tuple[ConnectionInfo, ...]:
            return (await client.list_connections(task)).connections

        self._ask(work, done)

    def remove_connection(
        self, connection: ConnectionId, done: Callable[[ToolsOutcome], None]
    ) -> None:
        """The user removes a connection (the chrome's action: no model tool does). The canvas is
        then given the broker's new list, so that values stop at once."""
        task = self._tasks.current
        if task is None:
            self._dispatcher.post(lambda: done(ToolsOutcome("refused", NO_TASK)))
            return

        async def work(client: BrokerClient) -> tuple[ToolsOutcome, tuple[ConnectionInfo, ...]]:
            try:
                await client.remove_connection(task, connection)
            except BrokerRefused as refusal:
                return _refusal(refusal), ()
            kept = (await client.list_connections(task)).connections
            return ToolsOutcome("done", REMOVED), kept

        def finished(outcome: tuple[ToolsOutcome, tuple[ConnectionInfo, ...]]) -> None:
            result, kept = outcome
            if result.kind == "done" and self._on_connections is not None:
                try:
                    self._on_connections(task, kept)
                except Exception:  # noqa: BLE001  (a UI path never takes the shell down)
                    _log.exception("giving the canvas the connections failed")
            done(result)

        self._act(work, finished, done)

    def open(
        self, tool: ToolId, version: ToolVersion | None, done: Callable[[ToolsOutcome], None]
    ) -> None:
        task = self._tasks.current
        if task is None:
            self._dispatcher.post(lambda: done(ToolsOutcome("refused", NO_TASK)))
            return

        async def work(client: BrokerClient) -> FetchedTool | ToolsOutcome:
            try:
                return await open_with(client, task, tool, version)
            except BrokerRefused as refusal:
                return _refusal(refusal)
            except ToolError:
                return ToolsOutcome("failed", "Not opened: the stored tool could not be read.")

        def finished(outcome: FetchedTool | ToolsOutcome) -> None:
            if isinstance(outcome, ToolsOutcome):
                done(outcome)
                return
            try:
                self._show(outcome)
            except Exception:  # noqa: BLE001  (a UI path never takes the shell down)
                _log.exception("showing a stored tool failed")
                done(ToolsOutcome("failed", "Not opened: the tool could not be shown."))
                return
            raised = (
                " This conversation's task now counts as having seen your files."
                if outcome.opened.raised and outcome.opened.taint == ("fs",)
                else " This conversation's task now counts as having used the network."
                if outcome.opened.raised
                else ""
            )
            done(ToolsOutcome("done", f"Opened on the canvas.{raised}"))

        self._act(work, finished, done)

    def restore(
        self, tool: ToolId, version: ToolVersion, done: Callable[[ToolsOutcome], None]
    ) -> None:
        task = self._tasks.current
        if task is None:
            self._dispatcher.post(lambda: done(ToolsOutcome("refused", NO_TASK)))
            return

        async def work(client: BrokerClient) -> ToolsOutcome:
            try:
                saved = await client.revert_tool(task, tool, version)
            except BrokerRefused as refusal:
                return _refusal(refusal)
            return ToolsOutcome(
                "done",
                f"Saved a copy of version {version} as version {saved.summary.version}. Access "
                f"for it is asked again.{_marked(saved.summary.taint) if saved.raised else ''}",
            )

        self._act(work, done, done)

    def revoke(self, grant: GrantId, done: Callable[[ToolsOutcome], None]) -> None:
        async def work(client: BrokerClient) -> ToolsOutcome:
            try:
                await client.revoke_tool_grant(grant)
            except BrokerRefused as refusal:
                return _refusal(refusal)
            return ToolsOutcome("done", "Access taken back. It applies from the next run.")

        self._act(work, done, done)

    # --- plumbing ---------------------------------------------------------------------------

    def _ask[R](
        self,
        work: Callable[[BrokerClient], Awaitable[R]],
        done: Callable[[R | None], None],
    ) -> None:
        def finished(future: "concurrent.futures.Future[R]") -> None:
            found: R | None
            try:
                found = future.result()
            except Exception:  # noqa: BLE001  (any failure is "the broker is not answering")
                _log.warning("a read of the tools list failed")
                found = None
            self._dispatcher.post(lambda: done(found))

        try:
            self._link.submit(work, deadline=_SECONDS).add_done_callback(finished)
        except BrokerUnavailableError:
            self._dispatcher.post(lambda: done(None))

    def _act[R](
        self,
        work: Callable[[BrokerClient], Awaitable[R]],
        finished: Callable[[R], None],
        failed: Callable[[ToolsOutcome], None],
    ) -> None:
        def settle(future: "concurrent.futures.Future[R]") -> None:
            try:
                outcome = future.result()
            except Exception:  # noqa: BLE001
                _log.warning("a tools action could not be asked of the broker")
                self._dispatcher.post(
                    lambda: failed(ToolsOutcome("failed", "Not done: the broker is not answering."))
                )
                return
            self._dispatcher.post(lambda: finished(outcome))

        try:
            self._link.submit(work, deadline=_SECONDS).add_done_callback(settle)
        except BrokerUnavailableError:
            self._dispatcher.post(lambda: failed(ToolsOutcome("failed", _UNAVAILABLE)))
