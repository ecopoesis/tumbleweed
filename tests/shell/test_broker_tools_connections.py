"""The chrome's port for connections (S25, D31 (9)): list, and remove (the user's own action).

A fake link runs the port's work against a fake client, so what is checked is what the port asks
of the broker and what it hands the canvas afterwards.
"""

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future

from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.core.ids import ConnectionId, TaskId, ToolInstanceId
from tumbleweed.ipc.connection_messages import ConnectionInfo, ConnectionList, ConnectionRemoved
from tumbleweed.shell.broker_tools import NO_TASK, REMOVED, BrokerToolsPort
from tumbleweed.shell.viewmodel.tools import ToolsOutcome


def info() -> ConnectionInfo:
    return ConnectionInfo.model_validate(
        {
            "connection": ConnectionId.new(),
            "from_instance": ToolInstanceId.new(),
            "from_port": "out",
            "from_name": "A",
            "to_instance": ToolInstanceId.new(),
            "to_port": "src",
            "to_name": "B",
            "port_type": "text",
            "same_task": True,
            "passes_on": (),
            "crossing": False,
        }
    )


class FakeClient:
    def __init__(self) -> None:
        self.held = [info(), info()]
        self.removed: list[tuple[TaskId, ConnectionId]] = []
        self.refuse = False

    async def list_connections(self, task: TaskId) -> ConnectionList:
        return ConnectionList(kind="connection_list", connections=tuple(self.held))

    async def remove_connection(self, task: TaskId, connection: ConnectionId) -> ConnectionRemoved:
        if self.refuse:
            raise BrokerRefused("not_found", "No such connection.")
        self.removed.append((task, connection))
        self.held = [each for each in self.held if each.connection != connection]
        return ConnectionRemoved(kind="connection_removed")


class FakeLink:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def submit[R](
        self, work: Callable[[BrokerClient], Awaitable[R]], *, deadline: float
    ) -> "Future[R]":
        future: Future[R] = Future()
        future.set_result(asyncio.run(work(self._client)))  # type: ignore[arg-type]  # a fake client
        return future


class Now:
    def post(self, callback: Callable[[], None]) -> None:
        callback()


class Tasks:
    def __init__(self, current: TaskId | None) -> None:
        self.current = current


def port(client: FakeClient, task: TaskId | None, seen: list[object]) -> BrokerToolsPort:
    return BrokerToolsPort(
        FakeLink(client),  # type: ignore[arg-type]  # a fake link
        Now(),
        Tasks(task),  # type: ignore[arg-type]  # a fake task session
        lambda fetched: None,
        lambda current, connections: seen.append((current, connections)),
    )


def test_it_lists_the_connections_of_the_conversations_task() -> None:
    client = FakeClient()
    got: list[tuple[ConnectionInfo, ...] | None] = []
    port(client, TaskId.new(), []).connections(got.append)

    assert got == [tuple(client.held)]


def test_with_no_task_there_is_nothing_to_list_and_nothing_to_remove() -> None:
    client = FakeClient()
    got: list[tuple[ConnectionInfo, ...] | None] = []
    outcomes: list[ToolsOutcome] = []
    chrome = port(client, None, [])

    chrome.connections(got.append)
    chrome.remove_connection(client.held[0].connection, outcomes.append)

    assert got == [()]
    assert outcomes == [ToolsOutcome("refused", NO_TASK)]
    assert client.removed == []


def test_removing_asks_the_broker_then_gives_the_canvas_what_is_left() -> None:
    client = FakeClient()
    outcomes: list[ToolsOutcome] = []
    seen: list[object] = []
    task = TaskId.new()
    gone, kept = client.held[0].connection, client.held[1]

    port(client, task, seen).remove_connection(gone, outcomes.append)

    assert client.removed == [(task, gone)]
    assert outcomes == [ToolsOutcome("done", REMOVED)]
    assert seen == [(task, (kept,))]  # the canvas stops passing values at once


def test_a_refused_removal_is_one_of_our_sentences_and_the_canvas_is_not_touched() -> None:
    client = FakeClient()
    outcomes: list[ToolsOutcome] = []
    seen: list[object] = []
    client.refuse = True

    port(client, TaskId.new(), seen).remove_connection(client.held[0].connection, outcomes.append)

    (outcome,) = outcomes
    assert outcome.kind == "refused"
    assert seen == []
