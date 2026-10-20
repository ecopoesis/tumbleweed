"""The shell's end of the prompt protocol (S18): requests from the broker reach the controller on
the "main" thread, the handler never waits for the user, and the answer goes back on the connection
the prompt came on.

The broker is a bare `IpcServer` whose handler keeps the answers it gets; the shell's main thread is
a worker thread with a queue, so that the hop between the connection's loop and it is a real one. No
GTK: the view is a fake.
"""

import asyncio
import gc
import queue
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Final

import pytest

from tests.ipc.bounded import run
from tests.ipc.helpers import next_connection
from tests.shell.prompt_support import Clock, FakeView, Timers, show_message
from tumbleweed.core.ids import PromptId, TaskId
from tumbleweed.ipc.broker_messages import Refused, StartTask
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import RequestRefused
from tumbleweed.ipc.messages import RequestBody, ResponseBody, Role
from tumbleweed.ipc.prompt_messages import (
    AllowDecision,
    AnswerPrompt,
    ClosePrompt,
    DenyDecision,
    PromptAnswered,
    PromptClosed,
    PromptShown,
    ShowPrompt,
)
from tumbleweed.ipc.server import IpcServer
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.shell.broker_prompts import BrokerPrompts
from tumbleweed.shell.viewmodel.prompts import ARM_SECONDS, MAX_QUEUED

WAIT: Final = 10.0


class MainThread:
    """A thread that runs what is posted to it, standing in for the GTK main thread."""

    def __init__(self) -> None:
        self._work: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self.hold = threading.Event()
        self.hold.set()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="fake-main")
        self._thread.start()

    def post(self, callback: Callable[[], None]) -> None:
        self._work.put(callback)

    def _loop(self) -> None:
        while (work := self._work.get()) is not None:
            self.hold.wait()
            work()

    def call[T](self, function: Callable[[], T]) -> T:
        """Run `function` on this thread and return what it returns (the user acting)."""
        done: queue.Queue[tuple[bool, T | BaseException]] = queue.Queue()

        def work() -> None:
            try:
                done.put((True, function()))
            except BaseException as error:  # noqa: BLE001  (handed to the caller)
                done.put((False, error))

        self.post(work)
        ok, value = done.get(timeout=WAIT)
        if not ok:
            assert isinstance(value, BaseException)
            raise value
        return value  # type: ignore[return-value]  # `ok` says it is the function's result

    def stop(self) -> None:
        self.hold.set()
        self._work.put(None)
        self._thread.join(timeout=WAIT)


class BrokerEnd:
    """The broker's handler: it keeps the answers it is given, with the connection they came on."""

    def __init__(self) -> None:
        self.answers: asyncio.Queue[tuple[AnswerPrompt, Connection]] = asyncio.Queue()

    async def __call__(self, request: RequestBody, connection: Connection, /) -> ResponseBody:
        assert isinstance(request, AnswerPrompt)
        self.answers.put_nowait((request, connection))
        return PromptAnswered(kind="prompt_answered")

    async def next(self) -> tuple[AnswerPrompt, Connection]:
        async with asyncio.timeout(WAIT):
            return await self.answers.get()


class Shell:
    def __init__(self) -> None:
        self.clock = Clock()
        self.timers = Timers(self.clock)
        self.view = FakeView()
        self.main = MainThread()
        self.prompts = BrokerPrompts(self.view, self.main, self.timers, self.clock)

    def act[T](self, function: Callable[[], T]) -> T:
        return self.main.call(function)

    def user_waits(self, seconds: float = ARM_SECONDS) -> None:
        self.act(lambda: self.timers.advance(seconds))


@contextmanager
def shell_end() -> Iterator[Shell]:
    shell = Shell()
    try:
        yield shell
    finally:
        shell.main.stop()


async def pair(
    path: SocketPath, shell: Shell
) -> tuple[IpcServer, BrokerEnd, Connection, Connection]:
    """A broker end and the shell's end of one connection: (server, handler, broker, shell)."""
    broker = BrokerEnd()
    server = IpcServer(path, broker, role=Role.BROKER)
    await server.start()
    client = await connect(path, shell.prompts, role=Role.SHELL)
    return server, broker, await next_connection(server), client


class TestAShownPrompt:
    def test_it_is_acknowledged_at_once_and_appears_without_waiting_for_the_user(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, _broker, to_shell, client = await pair(sock_path, shell)
                try:
                    message = show_message()

                    answer = await to_shell.call(message, PromptShown)

                    assert isinstance(answer, PromptShown)
                    assert [card.prompt for card in shell.view.shown] == [message.prompt]
                    assert shell.prompts.controller.current is not None
                finally:
                    await client.close()
                    await server.close()

        run(scenario)

    def test_the_users_answer_goes_back_on_the_connection_the_prompt_came_on(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, broker, to_shell, client = await pair(sock_path, shell)
                try:
                    message = show_message()
                    await to_shell.call(message, PromptShown)
                    shell.user_waits()
                    allow = AllowDecision(kind="allow", scope="task", picked=None)

                    taken = shell.act(
                        lambda: shell.prompts.controller.choose(message.prompt, allow)
                    )
                    answered, came_on = await broker.next()

                    assert taken
                    assert answered.prompt == message.prompt
                    assert answered.decision == allow
                    assert came_on is to_shell  # the broker's side of the connection it asked on
                finally:
                    await client.close()
                    await server.close()

        run(scenario)

    def test_the_shells_own_deadline_is_a_denial_the_broker_is_told_of(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, broker, to_shell, client = await pair(sock_path, shell)
                try:
                    message = show_message(expires_in_ms=5000)
                    await to_shell.call(message, PromptShown)

                    shell.user_waits(6)
                    answered, _ = await broker.next()

                    assert answered.prompt == message.prompt
                    assert answered.decision == DenyDecision(kind="deny", reason="prompt_timeout")
                    assert shell.prompts.controller.current is None
                finally:
                    await client.close()
                    await server.close()

        run(scenario)

    def test_a_prompt_the_broker_takes_down_comes_down_unanswered(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, broker, to_shell, client = await pair(sock_path, shell)
                try:
                    message = show_message()
                    await to_shell.call(message, PromptShown)

                    closed = await to_shell.call(
                        ClosePrompt(kind="close_prompt", prompt=message.prompt), PromptClosed
                    )
                    shell.user_waits(3600)  # nothing is left to expire either

                    assert isinstance(closed, PromptClosed)
                    assert shell.prompts.controller.current is None
                    assert broker.answers.empty()
                    # And the user cannot answer a prompt that is gone.
                    allow = AllowDecision(kind="allow", scope="task", picked=None)
                    assert not shell.act(
                        lambda: shell.prompts.controller.choose(message.prompt, allow)
                    )
                finally:
                    await client.close()
                    await server.close()

        run(scenario)

    def test_taking_down_a_prompt_that_is_not_there_is_answered_all_the_same(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, _broker, to_shell, client = await pair(sock_path, shell)
                try:
                    closed = await to_shell.call(
                        ClosePrompt(kind="close_prompt", prompt=PromptId.new()), PromptClosed
                    )

                    assert isinstance(closed, PromptClosed)
                finally:
                    await client.close()
                    await server.close()

        run(scenario)


class TestWhatTheShellRefuses:
    def test_a_ninth_prompt_is_refused_busy_and_never_shown(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, _broker, to_shell, client = await pair(sock_path, shell)
                try:
                    for _ in range(MAX_QUEUED):
                        await to_shell.call(show_message(), PromptShown)

                    refused = await to_shell.call(show_message(), Refused)

                    assert refused.reason == "busy"
                    assert len(shell.view.shown) == 1
                    assert shell.prompts.controller.queued == MAX_QUEUED - 1
                finally:
                    await client.close()
                    await server.close()

        run(scenario)

    def test_the_same_prompt_id_twice_is_refused(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, _broker, to_shell, client = await pair(sock_path, shell)
                try:
                    message = show_message()
                    await to_shell.call(message, PromptShown)

                    refused = await to_shell.call(message, Refused)

                    assert refused.reason == "busy"
                    assert shell.prompts.controller.queued == 0
                finally:
                    await client.close()
                    await server.close()

        run(scenario)

    def test_a_request_that_is_not_a_shells_is_not_served(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, _broker, _to_shell, client = await pair(sock_path, shell)
                try:
                    start = StartTask(kind="start_task", previous=None, fresh_context=False)

                    with pytest.raises(RequestRefused):
                        await shell.prompts(start, client)
                finally:
                    await client.close()
                    await server.close()

        run(scenario)

    def test_a_request_given_up_on_does_not_put_a_prompt_on_the_screen(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, _broker, _to_shell, client = await pair(sock_path, shell)
                try:
                    shell.main.hold.clear()  # the main thread is busy
                    handling = asyncio.create_task(shell.prompts(_show_for(TaskId.new()), client))
                    await asyncio.sleep(0.05)
                    handling.cancel()  # the connection ended, or the broker gave up
                    await asyncio.sleep(0)
                    shell.main.hold.set()
                    shell.act(lambda: None)  # let the posted work run

                    assert shell.view.shown == []
                    assert shell.prompts.controller.current is None
                finally:
                    await client.close()
                    await server.close()

        run(scenario)


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(WAIT):
        while not check():  # noqa: ASYNC110  (a bounded poll of the shell's main thread)
            await asyncio.sleep(0.01)


class TestAPromptDoesNotOutliveItsConnection:
    def test_the_prompts_of_a_connection_that_ends_are_taken_down(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, broker, to_shell, client = await pair(sock_path, shell)
                try:
                    message = show_message()
                    await to_shell.call(message, PromptShown)
                    assert shell.act(lambda: shell.prompts.controller.current) is not None

                    await client.close()  # the broker is gone (or the shell lost it)
                    await until(lambda: shell.act(lambda: shell.prompts.controller.current) is None)

                    assert shell.view.cleared >= 1
                    assert broker.answers.empty()  # nobody was answered: nobody is there
                    shell.user_waits(3600)
                    assert broker.answers.empty()
                finally:
                    await server.close()

        run(scenario)

    def test_a_new_connection_gets_its_prompt_shown_not_a_ghost_in_front_of_it(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, _broker, to_shell, client = await pair(sock_path, shell)
                try:
                    await to_shell.call(show_message(reason="from the old broker"), PromptShown)
                    await client.close()
                    await until(lambda: shell.act(lambda: shell.prompts.controller.current) is None)

                    again = await connect(sock_path, shell.prompts, role=Role.SHELL)
                    try:
                        fresh = await next_connection(server, not_in=frozenset({to_shell}))
                        new = show_message(reason="from the new broker")
                        await fresh.call(new, PromptShown)

                        current = shell.act(lambda: shell.prompts.controller.current)
                        assert current is not None
                        assert current.prompt == new.prompt
                        assert shell.prompts.controller.queued == 0
                    finally:
                        await again.close()
                finally:
                    await server.close()

        run(scenario)

    def test_a_prompt_waiting_behind_a_lost_connections_comes_up_when_it_is_taken_down(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, _broker, first_end, first = await pair(sock_path, shell)
                try:
                    second = await connect(sock_path, shell.prompts, role=Role.SHELL)
                    second_end = await next_connection(server, not_in=frozenset({first_end}))
                    old, new = show_message(), show_message()
                    await first_end.call(old, PromptShown)
                    await second_end.call(new, PromptShown)

                    await first.close()
                    await until(
                        lambda: (
                            (shown := shell.act(lambda: shell.prompts.controller.current))
                            is not None
                            and shown.prompt == new.prompt
                        )
                    )

                    shell.user_waits()
                    allow = AllowDecision(kind="allow", scope="once", picked=None)
                    assert shell.act(lambda: shell.prompts.controller.choose(new.prompt, allow))
                    await second.close()
                finally:
                    await server.close()

        run(scenario)

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
    def test_a_loop_torn_down_with_its_connection_takes_the_prompts_down_too(
        self, sock_path: SocketPath
    ) -> None:
        """`asyncio.run` ending cancels what is left, the connection's watcher included: the
        prompts of that connection must not stay up for want of its close being seen."""
        with shell_end() as shell:

            async def scenario() -> None:
                _server, _broker, to_shell, _client = await pair(sock_path, shell)
                await to_shell.call(show_message(), PromptShown)
                assert shell.act(lambda: shell.prompts.controller.current) is not None
                # ... and the loop ends here, with the connection still open.

            run(scenario)
            gc.collect()  # the sockets the torn-down loop left open are reported here, not later

            deadline = time.monotonic() + WAIT
            while shell.act(lambda: shell.prompts.controller.current) is not None:
                assert time.monotonic() < deadline, "the prompt of a dead connection stayed up"
                time.sleep(0.02)

    def test_a_closed_window_takes_no_more_prompts(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, _broker, to_shell, client = await pair(sock_path, shell)
                try:
                    shell.act(shell.prompts.close)

                    refused = await to_shell.call(show_message(), Refused)

                    assert refused.reason == "busy"
                    assert shell.view.shown == []
                finally:
                    await client.close()
                    await server.close()

        run(scenario)


class TestAnAnswerThatCannotBeSent:
    def test_an_answer_that_loses_its_connection_on_the_way_is_dropped_without_a_fuss(
        self, sock_path: SocketPath, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def scenario() -> None:
            with shell_end() as shell:
                server, _broker, to_shell, client = await pair(sock_path, shell)
                try:
                    message = show_message()
                    await to_shell.call(message, PromptShown)
                    shell.user_waits()
                    deny = DenyDecision(kind="deny", reason="user_denied")

                    taken = shell.act(lambda: shell.prompts.controller.choose(message.prompt, deny))
                    await to_shell.close()  # the broker goes in the same moment
                    await asyncio.sleep(0.2)
                    await until(lambda: shell.act(lambda: shell.prompts.controller.current) is None)

                    assert taken  # the user's decision was taken; whether it got there is the
                    assert not [
                        r for r in caplog.records if r.levelname == "ERROR"
                    ]  # broker's loss
                finally:
                    await client.close()
                    await server.close()

        run(scenario)


def _show_for(task: TaskId) -> ShowPrompt:
    return show_message(task=task)
