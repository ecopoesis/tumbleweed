"""`ShellPrompts` by itself, over a bare IPC pair: what the daemon's wiring hides (S18)."""

import asyncio
from datetime import timedelta

import pytest

from tests.broker.fake_shell import FakeShell
from tests.broker.rig import short_directory
from tests.ipc.helpers import next_connection
from tumbleweed.broker import shellprompts
from tumbleweed.broker.capabilities import NetFull
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.owner import Owner
from tumbleweed.broker.prompts import Denied, GrantPrompt
from tumbleweed.broker.shellprompts import ShellPrompts
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import ErrorCode
from tumbleweed.ipc.messages import RequestBody, ResponseBody, Role
from tumbleweed.ipc.server import IpcServer
from tumbleweed.ipc.sockdir import SocketDirectory


def prompt() -> GrantPrompt:
    return GrantPrompt(
        task=TaskId.new(),
        run=RunId.new(),
        capability=NetFull(),
        scopes=frozenset({GrantScope.ONCE, GrantScope.TASK}),
        reason="x",
    )


def test_detaching_a_connection_denies_what_waits_for_it_at_once() -> None:
    async def scenario() -> None:
        with short_directory() as directory:
            socket = SocketDirectory(directory, create=True).socket_path("direct")
            shell = FakeShell()
            prompts = ShellPrompts(timeout=timedelta(seconds=60))
            async with (
                IpcServer(socket, _noop, role=Role.BROKER) as server,
                await connect(socket, shell, role=Role.SHELL),
            ):
                prompts.attach(Owner(1), await next_connection(server))
                asking = asyncio.create_task(prompts.ask(prompt(), Owner(1)))
                await shell.next_prompt()

                prompts.detach(Owner(1))

                async with asyncio.timeout(5):  # not the 60 s of the prompt's own timeout
                    assert await asking == Denied(reason="no_prompt_ui")

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_a_prompt_that_does_not_answer_in_time_is_taken_down_in_case_the_shell_has_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(shellprompts, "_SHOW_TIMEOUT", timedelta(milliseconds=200))
        with short_directory() as directory:
            socket = SocketDirectory(directory, create=True).socket_path("direct")
            shell = FakeShell()
            shell.delay = 1.0  # it takes the prompt, and its acknowledgement is late
            prompts = ShellPrompts(timeout=timedelta(seconds=60))
            async with (
                IpcServer(socket, _noop, role=Role.BROKER) as server,
                await connect(socket, shell, role=Role.SHELL),
            ):
                prompts.attach(Owner(1), await next_connection(server))

                answer = await prompts.ask(prompt(), Owner(1))
                await shell.wait_closed()
                await prompts.close()

                assert answer == Denied(reason="prompt_failed")
                assert [shown.prompt for shown in shell.shown] == shell.closed

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_a_shell_too_busy_to_run_the_request_is_not_told_to_close_a_prompt() -> None:
    async def scenario() -> None:
        with short_directory() as directory:
            socket = SocketDirectory(directory, create=True).socket_path("direct")
            shell = FakeShell()
            shell.fail_with = ErrorCode.TOO_MANY_REQUESTS  # its handler never ran
            prompts = ShellPrompts(timeout=timedelta(seconds=60))
            async with (
                IpcServer(socket, _noop, role=Role.BROKER) as server,
                await connect(socket, shell, role=Role.SHELL),
            ):
                prompts.attach(Owner(1), await next_connection(server))

                answer = await prompts.ask(prompt(), Owner(1))
                await prompts.close()

                assert answer == Denied(reason="prompt_failed")
                assert shell.closed == []

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_any_other_failure_of_the_shell_after_it_took_the_prompt_closes_it() -> None:
    async def scenario() -> None:
        with short_directory() as directory:
            socket = SocketDirectory(directory, create=True).socket_path("direct")
            shell = FakeShell()
            shell.fail_with = ErrorCode.INTERNAL
            prompts = ShellPrompts(timeout=timedelta(seconds=60))
            async with (
                IpcServer(socket, _noop, role=Role.BROKER) as server,
                await connect(socket, shell, role=Role.SHELL),
            ):
                prompts.attach(Owner(1), await next_connection(server))

                answer = await prompts.ask(prompt(), Owner(1))
                await shell.wait_closed()

                assert answer == Denied(reason="prompt_failed")
                assert shell.closed == [shell.shown[0].prompt]

    asyncio.run(asyncio.wait_for(scenario(), 20))


async def _noop(request: RequestBody, connection: Connection, /) -> ResponseBody:
    raise AssertionError("the broker end serves nothing in these tests")
