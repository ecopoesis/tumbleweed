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
from tumbleweed.core.ids import GrantId, ToolId
from tumbleweed.core.types import ToolVersion
from tumbleweed.ipc.tool_messages import ToolGrantInfo, ToolSummary
from tumbleweed.shell.viewmodel.prompts import plain_reason
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


def _refusal(refusal: BrokerRefused) -> ToolsOutcome:
    text, _ = plain_reason(f"Not done: {refusal.message}.", 300)
    return ToolsOutcome("refused", text)


@final
class BrokerToolsPort:
    def __init__(
        self,
        link: BrokerLink,
        dispatcher: MainThreadDispatcher,
        tasks: TaskSession,
        show: Callable[[FetchedTool], None],
    ) -> None:
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
            done(ToolsOutcome("done", "Opened on the canvas."))

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
                "for it is asked again.",
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
