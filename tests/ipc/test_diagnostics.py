"""The connection answers ping and echo itself, and the idle timeout leaves requests alone.

Both are properties of a connection as opposed to its application: whatever handler the
broker or the shell supplies, a peer can ask whether it is there, and a connection that is busy
is never idle.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta

from tests.ipc.bounded import run
from tests.ipc.helpers import (
    SMALL_FRAMES,
    ScriptedHandler,
    connected,
    next_connection,
    raw_peer,
    serving,
)
from tumbleweed.core.ids import IpcRequestId
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.errors import ErrorCode, IdleTimeoutError
from tumbleweed.ipc.framing import encode_frame
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.messages import Echo, Echoed, Ping, Pong
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.wire import ErrorFrame, RequestFrame, ResponseFrame, encode_message


def echo(text: str) -> Echo:
    return Echo(kind="echo", text=text)


class TestTheDiagnosticsAreTheConnectionsOwn:
    def test_a_handler_never_sees_a_ping_or_an_echo(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            handler = ScriptedHandler()
            async with (
                serving(sock_path, handler, diagnostics=True),
                connected(sock_path, diagnostics=True) as client,
            ):
                assert await client.call(Ping(kind="ping"), Pong) == Pong(kind="pong")
                assert (await client.call(echo("hello"), Echoed)).text == "hello"
                assert (await client.call(echo("h\u00e9llo \U0001f600"), Echoed)).text == (
                    "h\u00e9llo \U0001f600"
                )

            assert handler.calls == []  # the handler was never called
            assert handler.connections == []

        run(scenario)

    def test_a_connection_without_any_handler_still_answers_them(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path, diagnostics=True) as (server, _handler):
                client = await connect(sock_path)  # no handler, diagnostics on
                try:
                    await client.call(Ping(kind="ping"), Pong)
                    from_server = await next_connection(server)

                    assert await from_server.call(Ping(kind="ping"), Pong) == Pong(kind="pong")
                    assert (await from_server.call(echo("up"), Echoed)).text == "up"
                finally:
                    await client.close()

        run(scenario)

    def test_the_opt_out_hands_them_to_the_handler(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            handler = ScriptedHandler()
            async with (
                serving(sock_path, handler, diagnostics=False),
                connected(sock_path) as client,
            ):
                await client.call(Ping(kind="ping"), Pong)
                await client.call(echo("seen"), Echoed)

            assert handler.calls == ["seen"]
            assert len(handler.connections) == 2

        run(scenario)

    def test_they_take_no_handler_slot_whatever_the_cap(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            limits = replace(IpcLimits(), max_inflight_handlers=1)
            handler = ScriptedHandler()
            async with (
                serving(sock_path, handler, limits=limits, diagnostics=True),
                connected(sock_path, diagnostics=True) as client,
            ):
                answers = await asyncio.gather(
                    *(client.call(Ping(kind="ping"), Pong) for _ in range(30)),
                    *(client.call(echo(f"e{i}"), Echoed) for i in range(2)),
                )

                assert len(answers) == 32  # none of them was refused for want of a slot
            assert handler.connections == []

        run(scenario)

    def test_an_echo_whose_answer_would_not_fit_the_frame_limit_is_answered_with_an_error(
        self, sock_path: SocketPath
    ) -> None:
        # The answer is three bytes longer than the question; a question at the limit can
        # therefore not be answered in full, and must be answered all the same.
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=SMALL_FRAMES, diagnostics=True),
                raw_peer(sock_path) as peer,
            ):
                request_id = IpcRequestId.new()
                shell = encode_message(RequestFrame(kind="request", id=request_id, body=echo("")))
                room = 4096 - len(shell)  # bytes left for the text, to make a frame of exactly 4096
                text = "\U0001f600" * (room // 4) + "a" * (room % 4)  # at most 1024 characters
                payload = encode_message(
                    RequestFrame(kind="request", id=request_id, body=echo(text))
                )
                assert len(payload) == 4096
                peer.send_bytes(encode_frame(payload))

                answer = await peer.recv()

                assert isinstance(answer, ErrorFrame)
                assert (answer.id, answer.code) == (request_id, ErrorCode.INTERNAL)

        run(scenario)

    def test_an_answered_ping_is_a_response_frame_with_the_same_id(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path, diagnostics=True), raw_peer(sock_path) as peer:
                request_id = IpcRequestId.new()
                await peer.send(RequestFrame(kind="request", id=request_id, body=Ping(kind="ping")))

                assert await peer.recv() == ResponseFrame(
                    kind="response", id=request_id, body=Pong(kind="pong")
                )

        run(scenario)


class TestTheIdleTimeoutLeavesBusyConnectionsAlone:
    def test_a_request_in_progress_keeps_a_quiet_connection_open_and_silence_after_it_does_not(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            limits = replace(IpcLimits(), idle_timeout=timedelta(seconds=0.3))
            async with serving(sock_path, limits=limits) as (server, handler):
                client = await connect(sock_path, answer_diagnostics=False)
                try:
                    waiting = asyncio.create_task(client.call(echo("hold:x"), Echoed))
                    await handler.started["x"].wait()
                    at_the_server = await next_connection(server)

                    await asyncio.sleep(1.2)  # four idle timeouts, with a handler running

                    assert not at_the_server.is_closed
                    handler.gates["x"].set()
                    assert (await waiting).text == "hold:x"
                    await at_the_server.wait_closed()  # and now it is idle for real
                    assert isinstance(at_the_server.close_reason, IdleTimeoutError)
                finally:
                    await client.close()

        run(scenario)

    def test_a_call_waiting_for_its_answer_is_not_idle_either(self, sock_path: SocketPath) -> None:
        # The side that waits has the idle timeout: its peer is working on the answer.
        async def scenario() -> None:
            limits = replace(IpcLimits(), idle_timeout=timedelta(seconds=0.3))
            async with serving(sock_path) as (_server, handler):
                client = await connect(sock_path, limits=limits, answer_diagnostics=False)
                try:
                    waiting = asyncio.create_task(client.call(echo("hold:x"), Echoed))
                    await handler.started["x"].wait()

                    await asyncio.sleep(1.2)

                    assert not client.is_closed
                    handler.gates["x"].set()
                    assert (await waiting).text == "hold:x"
                finally:
                    await client.close()

        run(scenario)
