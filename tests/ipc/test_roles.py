"""Requests go one way each: the broker serves the shell's, the shell the broker's (#61, S18).

A connection made with a `Role` answers a request of the other direction with
`unsupported_request` before its handler sees it, and refuses to send one that no peer of the other
role serves. Without a role (the transport's own tests) any registered request goes either way.
"""

from typing import get_args

import pytest

from tests.ipc.bounded import run
from tests.ipc.helpers import next_connection
from tumbleweed.core.ids import PromptId, TaskId
from tumbleweed.ipc.broker_messages import NetworkAccess, StartTask, TaskStarted
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import ErrorCode, RemoteError
from tumbleweed.ipc.messages import (
    BROKER_SERVES,
    DIAGNOSTICS,
    SHELL_SERVES,
    Echo,
    Echoed,
    RequestBody,
    ResponseBody,
    Role,
    sends,
    serves,
)
from tumbleweed.ipc.prompt_messages import PromptShown, ShowPrompt
from tumbleweed.ipc.server import IpcServer
from tumbleweed.ipc.sockdir import SocketPath


class Recorder:
    """A handler that answers what a broker or a shell would, and keeps what it was given."""

    def __init__(self) -> None:
        self.seen: list[RequestBody] = []

    async def __call__(self, request: RequestBody, connection: Connection, /) -> ResponseBody:
        self.seen.append(request)
        if isinstance(request, ShowPrompt):
            return PromptShown(kind="prompt_shown")
        return TaskStarted(kind="task_started", task=TaskId.new(), taint=())


def show() -> ShowPrompt:
    return ShowPrompt(
        kind="show_prompt",
        prompt=PromptId.new(),
        task=TaskId.new(),
        tool=None,
        access=NetworkAccess(kind="net.full"),
        scopes=("task",),
        reason="x",
        expires_in_ms=1000,
    )


def test_every_request_goes_one_way_and_the_diagnostics_both() -> None:
    registered = set(get_args(get_args(RequestBody)[0]))

    assert set(BROKER_SERVES) | set(SHELL_SERVES) | set(DIAGNOSTICS) == registered
    assert not set(BROKER_SERVES) & set(SHELL_SERVES)
    assert serves(Role.BROKER) == sends(Role.SHELL)
    assert serves(Role.SHELL) == sends(Role.BROKER)


class TestAnEndWithARole:
    def test_the_broker_end_never_hands_its_handler_a_request_of_the_shell_end(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            handler = Recorder()
            async with IpcServer(sock_path, handler, role=Role.BROKER):
                async with await connect(sock_path, Recorder()) as plain:  # no role
                    with pytest.raises(RemoteError) as caught:
                        await plain.call(show(), PromptShown)

                assert caught.value.code is ErrorCode.UNSUPPORTED_REQUEST
                assert handler.seen == []

        run(scenario)

    def test_the_shell_end_never_hands_its_handler_a_request_of_the_broker_end(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with IpcServer(sock_path, Recorder()) as server:  # no role: it may send anything
                handler = Recorder()
                async with await connect(sock_path, handler, role=Role.SHELL):
                    peer = await next_connection(server)
                    start = StartTask(kind="start_task", previous=None, fresh_context=False)

                    with pytest.raises(RemoteError) as caught:
                        await peer.call(start, TaskStarted)
                    shown = await peer.call(show(), PromptShown)  # what a shell serves

                assert caught.value.code is ErrorCode.UNSUPPORTED_REQUEST
                assert isinstance(shown, PromptShown)
                assert [type(request) for request in handler.seen] == [ShowPrompt]

        run(scenario)

    def test_a_broker_end_serves_what_the_shell_asks(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            handler = Recorder()
            async with IpcServer(sock_path, handler, role=Role.BROKER):
                async with await connect(sock_path, role=Role.SHELL) as shell:
                    start = StartTask(kind="start_task", previous=None, fresh_context=False)

                    started = await shell.call(start, TaskStarted)

                assert isinstance(started, TaskStarted)
                assert handler.seen == [start]

        run(scenario)

    def test_an_end_does_not_send_what_only_its_own_role_serves(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                IpcServer(sock_path, Recorder(), role=Role.BROKER) as server,
                await connect(sock_path, Recorder(), role=Role.SHELL) as shell,
            ):
                broker_end = await next_connection(server)
                start = StartTask(kind="start_task", previous=None, fresh_context=False)

                with pytest.raises(TypeError):
                    await shell.call(show(), PromptShown)  # the shell does not show prompts
                with pytest.raises(TypeError):
                    await broker_end.call(start, TaskStarted)  # nor the broker start tasks
                # Diagnostics go either way.
                echoed = await shell.call(Echo(kind="echo", text="hi"), Echoed)
                assert echoed.text == "hi"

        run(scenario)
