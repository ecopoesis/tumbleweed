"""One connection: correlation, concurrency in both directions, bounds, errors and disconnects.

Real sockets, real handshakes. Handlers are `ScriptedHandler`s that hold a request open until
the test lets it go, so that "in flight at the same time" is arranged, not hoped for. Nothing
waits a fixed time for an effect: tests wait on events, and a hang fails at the `run` deadline.
"""

import asyncio
import errno
import logging
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.ipc.bounded import run
from tests.ipc.helpers import (
    BIG_TEXT,
    SMALL_FRAMES,
    RawPeer,
    ScriptedHandler,
    connected,
    next_connection,
    serving,
)
from tumbleweed.core.ids import IpcRequestId
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import (
    ConnectionClosedError,
    ErrorCode,
    FrameTooLargeError,
    RemoteError,
    RequestTimeoutError,
    TooManyRequestsError,
    UnexpectedResponseError,
)
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.messages import Echo, Echoed, IpcMessage, Ping, Pong
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.wire import RequestFrame


def echo(text: str) -> Echo:
    return Echo(kind="echo", text=text)


async def is_alive(connection: Connection) -> bool:
    """Whether a ping is answered with a pong on this connection."""
    return await connection.call(Ping(kind="ping"), Pong) == Pong(kind="pong")


class TestCalls:
    def test_a_ping_is_answered_with_a_pong_and_an_echo_with_its_text(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                assert await client.call(Ping(kind="ping"), Pong) == Pong(kind="pong")
                text = "héllo \U0001f600 世界 \x00 end"
                assert (await client.call(echo(text), Echoed)).text == text
                assert (await client.call(echo(""), Echoed)).text == ""

        run(scenario)

    def test_concurrent_calls_each_get_their_own_answer_whatever_order_they_finish_in(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (_server, handler), connected(sock_path) as client:
                keys = [f"k{i}" for i in range(24)]
                calls = [
                    asyncio.create_task(client.call(echo(f"hold:{key}"), Echoed)) for key in keys
                ]
                for key in keys:  # all of them are inside the handler at the same time
                    await handler.started[key].wait()
                for key in reversed(keys):  # and they finish in the opposite order
                    handler.gates[key].set()

                answers = await asyncio.gather(*calls)

                assert [a.text for a in answers] == [f"hold:{key}" for key in keys]

        run(scenario)

    def test_either_peer_can_call_the_other_on_the_same_connection(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            client_handler = ScriptedHandler()
            async with (
                serving(sock_path) as (server, handler),
                connected(sock_path, client_handler) as client,
            ):
                await client.call(Ping(kind="ping"), Pong)
                from_server = await next_connection(server)

                reply = await from_server.call(echo("from the server"), Echoed)

                assert reply.text == "from the server"
                assert client_handler.calls == ["from the server"]
                assert handler.calls == []

        run(scenario)

    def test_both_peers_have_requests_in_flight_at_the_same_moment(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            client_handler = ScriptedHandler()
            async with (
                serving(sock_path) as (server, handler),
                connected(sock_path, client_handler) as client,
            ):
                await client.call(Ping(kind="ping"), Pong)
                from_server = await next_connection(server)
                up = asyncio.create_task(client.call(echo("hold:up"), Echoed))
                down = asyncio.create_task(from_server.call(echo("hold:down"), Echoed))
                await handler.started["up"].wait()
                await client_handler.started["down"].wait()  # each is waiting on the other side

                client_handler.gates["down"].set()
                handler.gates["up"].set()

                assert (await up).text == "hold:up"
                assert (await down).text == "hold:down"

        run(scenario)

    def test_a_handler_can_call_the_peer_back_while_it_handles_the_peers_request(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            client_handler = ScriptedHandler()
            async with serving(sock_path), connected(sock_path, client_handler) as client:
                answer = await client.call(echo("callback:ping-back"), Echoed)

                assert answer.text == "via:ping-back"
                assert client_handler.calls == ["ping-back"]

        run(scenario)

    def test_a_peer_without_a_handler_refuses_every_request_with_unsupported_request(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, _handler), connected(sock_path) as client:
                await client.call(Ping(kind="ping"), Pong)
                from_server = await next_connection(server)

                with pytest.raises(RemoteError) as raised:
                    await from_server.call(Ping(kind="ping"), Pong)

                assert raised.value.code is ErrorCode.UNSUPPORTED_REQUEST
                assert not from_server.is_closed  # a refusal is an answer, not a failure

        run(scenario)


class TestAnswers:
    def test_a_handler_can_refuse_with_a_code_and_a_message(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                with pytest.raises(RemoteError) as raised:
                    await client.call(echo("refuse"), Echoed)

                assert raised.value.code is ErrorCode.UNSUPPORTED_REQUEST
                assert raised.value.message == "nope"
                assert (await client.call(echo("still works"), Echoed)).text == "still works"

        run(scenario)

    def test_a_refusal_message_is_cut_to_the_wire_limit(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                with pytest.raises(RemoteError) as raised:
                    await client.call(echo("refuse-long"), Echoed)

                assert raised.value.message == "x" * 256

        run(scenario)

    def test_a_handler_that_crashes_is_reported_as_internal_without_its_message(
        self, sock_path: SocketPath, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                with pytest.raises(RemoteError) as raised:
                    await client.call(echo("boom"), Echoed)

                assert raised.value.code is ErrorCode.INTERNAL
                assert "secret detail" not in str(raised.value)
                assert "secret detail" not in raised.value.message
                assert (await client.call(echo("alive"), Echoed)).text == "alive"

        run(scenario)
        assert "secret detail" in caplog.text  # it went to the callee's log instead

    def test_a_handler_that_returns_nothing_valid_is_reported_as_internal(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                with pytest.raises(RemoteError) as raised:
                    await client.call(echo("none"), Echoed)

                assert raised.value.code is ErrorCode.INTERNAL

        run(scenario)

    def test_a_response_of_the_wrong_type_is_the_callers_unexpected_response_error(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                with pytest.raises(UnexpectedResponseError, match=r"expected a Echoed.*Pong"):
                    await client.call(echo("wrong"), Echoed)

                assert (await client.call(echo("fine"), Echoed)).text == "fine"

        run(scenario)

    def test_a_response_too_large_to_send_becomes_an_internal_error(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path, limits=SMALL_FRAMES), connected(sock_path) as client:
                with pytest.raises(RemoteError) as raised:
                    await client.call(echo("big"), Echoed)

                assert raised.value.code is ErrorCode.INTERNAL
                assert (await client.call(echo("small"), Echoed)).text == "small"

        run(scenario)

    def test_a_request_too_large_to_send_is_refused_locally_and_sends_nothing(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path) as (_server, handler),
                connected(sock_path, limits=SMALL_FRAMES) as client,
            ):
                with pytest.raises(FrameTooLargeError):
                    await client.call(echo(BIG_TEXT), Echoed)

                assert handler.calls == []
                assert (await client.call(echo("small"), Echoed)).text == "small"

        run(scenario)

    def test_a_request_that_is_not_in_the_registry_is_refused_locally(
        self, sock_path: SocketPath
    ) -> None:
        class Rogue(IpcMessage):
            """Not a Request subclass: tests never subclass Request (see test_schema)."""

        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                with pytest.raises(TypeError, match="not a registered request body: Rogue"):
                    await client.call(Rogue(), Pong)  # type: ignore[arg-type]  # a request that is not registered: the call must refuse it at runtime

                assert await is_alive(client)  # nothing was left pending

        run(scenario)


class TestTimeouts:
    def test_a_call_that_gets_no_answer_in_time_fails_and_the_connection_goes_on(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (_server, handler), connected(sock_path) as client:
                with pytest.raises(RequestTimeoutError, match=r"0\.2 s"):
                    await client.call(
                        echo("hold:slow"), Echoed, request_timeout=timedelta(seconds=0.2)
                    )
                assert handler.started["slow"].is_set()

                assert (await client.call(echo("next"), Echoed)).text == "next"

        run(scenario)

    def test_the_default_timeout_comes_from_the_limits(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            limits = replace(IpcLimits(), request_timeout=timedelta(seconds=0.2))
            async with serving(sock_path), connected(sock_path, limits=limits) as client:
                with pytest.raises(RequestTimeoutError):
                    await client.call(echo("hold:slow"), Echoed)

        run(scenario)

    def test_an_answer_that_arrives_after_its_call_gave_up_is_dropped_quietly(
        self, sock_path: SocketPath, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="tumbleweed.ipc.connection")

        async def scenario() -> None:
            async with serving(sock_path) as (_server, handler), connected(sock_path) as client:
                with pytest.raises(RequestTimeoutError):
                    await client.call(
                        echo("hold:late"), Echoed, request_timeout=timedelta(seconds=0.1)
                    )

                handler.gates["late"].set()  # the answer is sent now, to nobody
                while "dropped an answer" not in caplog.text:  # noqa: ASYNC110  (a log line)
                    await asyncio.sleep(0.01)

                assert not client.is_closed
                assert (await client.call(echo("after"), Echoed)).text == "after"

        run(scenario)

    def test_a_handler_that_runs_too_long_is_cancelled_and_the_caller_is_told_timeout(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            limits = replace(IpcLimits(), handler_timeout=timedelta(seconds=0.2))
            async with (
                serving(sock_path, limits=limits) as (_server, handler),
                connected(sock_path) as client,
            ):
                with pytest.raises(RemoteError) as raised:
                    await client.call(echo("hold:forever"), Echoed)

                assert raised.value.code is ErrorCode.TIMEOUT
                assert handler.cancelled == ["forever"]
                assert (await client.call(echo("next"), Echoed)).text == "next"

        run(scenario)

    @pytest.mark.parametrize("bad", [timedelta(0), timedelta(seconds=-1), 5, "5"])
    def test_a_timeout_that_is_not_a_positive_timedelta_is_refused(
        self, sock_path: SocketPath, bad: object
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                await client.call(Ping(kind="ping"), Pong, request_timeout=bad)  # type: ignore[arg-type]  # a bad timeout on purpose: the call must refuse it at runtime

        with pytest.raises(ValueError, match="positive timedelta"):
            run(scenario)


class TestBounds:
    def test_calls_beyond_the_in_flight_limit_are_refused_at_once_and_room_is_made_by_answers(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            limits = replace(IpcLimits(), max_inflight_calls=3)
            async with (
                serving(sock_path) as (_server, handler),
                connected(sock_path, limits=limits) as client,
            ):
                held = [
                    asyncio.create_task(client.call(echo(f"hold:{i}"), Echoed)) for i in range(3)
                ]
                for i in range(3):
                    await handler.started[str(i)].wait()

                with pytest.raises(TooManyRequestsError, match="limit 3"):
                    await client.call(Ping(kind="ping"), Pong)

                handler.gates["0"].set()
                await held[0]
                assert await is_alive(client)  # room again
                for i in (1, 2):
                    handler.gates[str(i)].set()
                await asyncio.gather(*held)

        run(scenario)

    def test_requests_beyond_the_handler_limit_are_answered_too_many_requests_unrun(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            limits = replace(IpcLimits(), max_inflight_handlers=2)
            async with (
                serving(sock_path, limits=limits) as (_server, handler),
                connected(sock_path) as client,
            ):
                held = [
                    asyncio.create_task(client.call(echo(f"hold:{i}"), Echoed)) for i in range(2)
                ]
                for i in range(2):
                    await handler.started[str(i)].wait()

                with pytest.raises(RemoteError) as raised:
                    await client.call(echo("hold:2"), Echoed)

                assert raised.value.code is ErrorCode.TOO_MANY_REQUESTS
                assert handler.calls == ["hold:0", "hold:1"]  # the third never ran
                for i in range(2):
                    handler.gates[str(i)].set()
                await asyncio.gather(*held)
                assert (await client.call(echo("room again"), Echoed)).text == "room again"

        run(scenario)

    def test_a_cancelled_call_gives_its_slot_back(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            limits = replace(IpcLimits(), max_inflight_calls=1)
            async with (
                serving(sock_path) as (_server, handler),
                connected(sock_path, limits=limits) as client,
            ):
                held = asyncio.create_task(client.call(echo("hold:x"), Echoed))
                await handler.started["x"].wait()
                with pytest.raises(TooManyRequestsError):
                    await client.call(Ping(kind="ping"), Pong)

                held.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await held

                assert await is_alive(client)

        run(scenario)


class TestDisconnects:
    def test_when_the_server_closes_a_waiting_call_fails_at_once_not_at_its_timeout(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, handler), connected(sock_path) as client:
                waiting = asyncio.create_task(client.call(echo("hold:x"), Echoed))
                await handler.started["x"].wait()  # the default timeout is 30 s

                await (await next_connection(server)).close()

                with pytest.raises(ConnectionClosedError, match="peer closed"):
                    await waiting
                await client.wait_closed()
                assert client.is_closed
                assert isinstance(client.close_reason, ConnectionClosedError)

        run(scenario)

    def test_when_the_client_closes_the_servers_handler_is_cancelled_and_the_connection_ends(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, handler):
                client = await connect(sock_path)
                waiting = asyncio.create_task(client.call(echo("hold:x"), Echoed))
                await handler.started["x"].wait()
                at_the_server = await next_connection(server)

                await client.close()

                with pytest.raises(ConnectionClosedError, match="closed locally"):
                    await waiting
                await at_the_server.wait_closed()
                assert handler.cancelled == ["x"]
                assert not server.connections

        run(scenario)

    def test_a_connection_is_not_closed_until_its_cancelled_handlers_have_cleaned_up(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, handler):
                client = await connect(sock_path)
                waiting = asyncio.create_task(client.call(echo("slow-cleanup:x"), Echoed))
                await handler.started["x"].wait()
                at_the_server = await next_connection(server)

                await at_the_server.close()  # the handler takes 0.2 s to clean up after itself

                assert handler.cancelled == ["x"]  # `close` came back only after it was done
                with pytest.raises(ConnectionClosedError):
                    await waiting
                await client.close()

        run(scenario)

    def test_when_the_peer_drops_the_transport_without_a_word_everything_is_released(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, handler):
                peer = await RawPeer.handshaken(sock_path)
                at_the_server = await next_connection(server)
                request = RequestFrame(kind="request", id=IpcRequestId.new(), body=echo("hold:x"))
                await peer.send(request)
                await handler.started["x"].wait()

                peer.writer.transport.abort()

                await at_the_server.wait_closed()
                assert handler.cancelled == ["x"]
                assert isinstance(at_the_server.close_reason, ConnectionClosedError)

        run(scenario)

    def test_a_connection_closed_the_moment_it_was_made_does_not_hang(
        self, sock_path: SocketPath
    ) -> None:
        # The connection's reader has not run yet: closing must still release everything.
        async def scenario() -> None:
            async with serving(sock_path):
                client = await connect(sock_path)
                await client.close()
                assert client.is_closed

        run(scenario)

    def test_a_call_on_a_closed_connection_fails_at_once_and_closing_again_is_harmless(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path):
                client = await connect(sock_path)
                await client.close()
                await client.close()
                await client.wait_closed()

                with pytest.raises(ConnectionClosedError, match="the connection is closed"):
                    await client.call(Ping(kind="ping"), Pong)

        run(scenario)

    def test_closing_fails_every_waiting_call(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (_server, handler):
                client = await connect(sock_path)
                waiting = [
                    asyncio.create_task(client.call(echo(f"hold:{i}"), Echoed)) for i in range(10)
                ]
                for i in range(10):
                    await handler.started[str(i)].wait()

                await client.close()

                results = await asyncio.gather(*waiting, return_exceptions=True)
                assert all(isinstance(r, ConnectionClosedError) for r in results)

        run(scenario)

    def test_a_handler_that_closes_its_own_connection_does_not_deadlock(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, _handler):
                client = await connect(sock_path)
                try:
                    with pytest.raises(ConnectionClosedError):
                        await client.call(echo("closeme"), Echoed)
                    await client.wait_closed()
                    assert not server.connections
                finally:
                    await client.close()

        run(scenario)

    def test_closing_the_server_closes_every_connection_and_fails_what_waits_on_them(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            server_calls: list[asyncio.Task[Echoed]] = []
            async with serving(sock_path) as (server, handler):
                clients = [await connect(sock_path) for _ in range(3)]
                server_calls = [
                    asyncio.create_task(c.call(echo(f"hold:{i}"), Echoed))
                    for i, c in enumerate(clients)
                ]
                for i in range(3):
                    await handler.started[str(i)].wait()
                assert len(server.connections) == 3

            results = await asyncio.gather(*server_calls, return_exceptions=True)
            assert all(isinstance(r, ConnectionClosedError) for r in results)
            for client in clients:
                await client.wait_closed()
                assert client.is_closed
            assert not sock_path.path.exists()

        run(scenario)


class TestConcurrentLoad:
    def test_many_calls_in_batches_all_get_their_own_answers(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                for batch in range(8):
                    texts = [f"{batch}-{i}" for i in range(25)]
                    answers = await asyncio.gather(*(client.call(echo(t), Echoed) for t in texts))
                    assert [a.text for a in answers] == texts

        run(scenario)


class StubTransport:
    def set_write_buffer_limits(self, high: int | None = None, low: int | None = None) -> None:
        pass

    def get_write_buffer_size(self) -> int:
        return 0

    def abort(self) -> None:
        pass


class ResettingWriter:
    """A writer whose connection was reset behind the connection's back: `drain` says so."""

    def __init__(self) -> None:
        self.transport = StubTransport()

    def write(self, data: bytes) -> None:
        raise AssertionError("nothing may be written to a connection that is known to be reset")

    async def drain(self) -> None:
        raise ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def test_a_reset_that_only_the_writer_has_noticed_is_a_closed_connection_not_an_os_error() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()  # it has not been told, and never will be
        connection = Connection(
            reader,
            ResettingWriter(),  # type: ignore[arg-type]  # a stand-in writer
            limits=IpcLimits(),
            handler=None,
        )
        try:
            with pytest.raises(ConnectionClosedError, match="Connection reset by peer"):
                await connection.call(Ping(kind="ping"), Pong)

            assert connection.is_closed
            assert isinstance(connection.close_reason, ConnectionClosedError)
        finally:
            await connection.close()

    run(scenario)
