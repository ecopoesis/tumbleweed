"""The life of a request on the handling side: its slot, its answer, and what must not leak.

A request is in progress, and counts against the cap, from the moment its handler is started
until its reply has been written. Whatever a handler does (return, fail, be cancelled by
something other than its connection, refuse in an odd way) the caller is answered, the slot is
given back, and a failure to send ends the connection instead of leaving anything dangling.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.ipc.bounded import run
from tests.ipc.helpers import (
    SMALL_FRAMES,
    connected,
    next_connection,
    raw_peer,
    serving,
)
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import IpcRequestId
from tumbleweed.ipc import connection as connection_module
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.connection import Connection, _error
from tumbleweed.ipc.errors import (
    ConnectionClosedError,
    ErrorCode,
    RemoteError,
    RequestRefused,
)
from tumbleweed.ipc.framing import encode_frame
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.messages import Echo, Echoed
from tumbleweed.ipc.server import IpcServer
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.wire import Frame, RequestFrame, ResponseFrame, encode_message

ONE_AT_A_TIME = replace(IpcLimits(), max_inflight_handlers=1)


def echo(text: str) -> Echo:
    return Echo(kind="echo", text=text)


class TestTheSlotOfARequest:
    def test_a_peer_that_does_not_read_cannot_make_the_server_run_more_than_its_cap(
        self, sock_path: SocketPath
    ) -> None:
        # Replies pile up behind a peer that reads nothing. A handler that has returned must
        # still hold its slot until its reply is written, or the reader keeps admitting
        # requests: tens of thousands of tasks and hundreds of megabytes in seconds.
        async def scenario() -> None:
            limits = replace(SMALL_FRAMES, write_timeout=timedelta(seconds=60))
            async with (
                serving(sock_path, limits=limits) as (server, handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)
                flood = b"".join(
                    _frame(request(echo("x" * 1000))) for _ in range(4000)
                )  # about 4 MB of requests, each answered with 1 KB
                peer.send_bytes(flood)

                await server_side.wait_closed()  # the flood ends the connection by itself

                assert len(handler.calls) > IpcLimits().max_inflight_handlers  # it did flood
                assert handler.peak_tasks <= IpcLimits().max_inflight_handlers
                assert isinstance(server_side.close_reason, ConnectionClosedError)
                assert "pile up" in str(server_side.close_reason)

        run(scenario)

    def test_a_slot_is_held_until_the_reply_is_written_not_until_the_handler_returns(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=ONE_AT_A_TIME) as (_server, handler),
                connected(sock_path) as client,
            ):
                first = await client.call(echo("one"), Echoed)
                second = await client.call(echo("two"), Echoed)  # the slot came back

                assert (first.text, second.text) == ("one", "two")
                assert handler.peak_tasks == 1

        run(scenario)


class TestAHandlerThatFails:
    def test_a_handler_cancelled_by_something_else_still_answers_internal_and_frees_its_slot(
        self, sock_path: SocketPath, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=ONE_AT_A_TIME) as (_server, handler),
                connected(sock_path) as client,
            ):
                waiting = asyncio.create_task(client.call(echo("outside:x"), Echoed))
                await handler.started["x"].wait()

                handler.futures["x"].cancel()  # not the connection: whatever the handler awaited

                with pytest.raises(RemoteError) as raised:
                    await waiting
                assert raised.value.code is ErrorCode.INTERNAL
                # The slot was given back: with a cap of one, this would be refused otherwise.
                assert (await client.call(echo("next"), Echoed)).text == "next"

        run(scenario)
        assert "cancelled from outside" in caplog.text

    def test_a_handler_cancelled_with_its_connection_is_not_answered(
        self, sock_path: SocketPath
    ) -> None:
        # The other side of the same rule: when the connection itself is closing, the
        # cancellation is the connection's and must go through.
        async def scenario() -> None:
            async with serving(sock_path) as (server, handler):
                client = await _client(sock_path)
                waiting = asyncio.create_task(client.call(echo("hold:x"), Echoed))
                await handler.started["x"].wait()
                at_the_server = await next_connection(server)

                await at_the_server.close()

                with pytest.raises(ConnectionClosedError):
                    await waiting
                assert handler.cancelled == ["x"]
                await client.close()

        run(scenario)

    def test_a_refusal_whose_text_is_not_valid_utf8_is_still_an_answer(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=ONE_AT_A_TIME),
                connected(sock_path) as client,
            ):
                with pytest.raises(RemoteError) as raised:
                    await client.call(echo("refuse-escape"), Echoed)

                assert raised.value.code is ErrorCode.UNSUPPORTED_REQUEST
                assert raised.value.message == "\\udcff"  # written out as an escape
                assert (await client.call(echo("next"), Echoed)).text == "next"

        run(scenario)

    def test_a_refusal_with_a_code_that_is_not_an_error_code_is_the_handlers_bug(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=ONE_AT_A_TIME),
                connected(sock_path) as client,
            ):
                with pytest.raises(RemoteError) as raised:
                    await client.call(echo("refuse-bad"), Echoed)

                assert raised.value.code is ErrorCode.INTERNAL
                assert (await client.call(echo("next"), Echoed)).text == "next"

        run(scenario)

    def test_a_reply_that_cannot_be_sent_ends_the_connection_and_everyone_waiting_on_it(
        self,
        sock_path: SocketPath,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:

        def broken(frame: Frame) -> bytes:
            if isinstance(frame, ResponseFrame):
                raise RuntimeError("a codec bug")
            return encode_message(frame)

        monkeypatch.setattr(connection_module, "encode_message", broken)

        async def scenario() -> None:
            async with serving(sock_path) as (server, _handler):
                client = await _client(sock_path)
                try:
                    with pytest.raises(ConnectionClosedError):  # at once, not at the timeout
                        await client.call(echo("anything"), Echoed)
                    await next_connection_closed(server)
                finally:
                    await client.close()

        run(scenario)
        assert "could not be sent" in caplog.text


class TestAnErrorFrameIsAlwaysBuilt:
    @pytest.mark.parametrize(
        "message",
        ["plain", "", "\udcff", "\ud800 and more", "x" * 5000, "\U0001f600" * 300, "tab\tnl\n"],
    )
    def test_whatever_the_text_the_frame_is_valid_and_within_the_limit(self, message: str) -> None:
        frame = _error(IpcRequestId.new(), ErrorCode.TIMEOUT, message)

        assert frame.code is ErrorCode.TIMEOUT
        assert len(frame.message) <= 256
        encode_message(frame).decode("utf-8")  # real UTF-8, whatever went in

    @pytest.mark.parametrize("bad_code", ["not a code", None, 3])
    def test_whatever_the_code_the_frame_is_an_internal_error(self, bad_code: object) -> None:
        frame = _error(None, bad_code, "m")  # type: ignore[arg-type]  # a bad code on purpose

        assert frame.code is ErrorCode.INTERNAL
        assert frame.message == "the request could not be handled"

    @pytest.mark.parametrize("bad_message", [None, 5, b"bytes"])
    def test_whatever_the_message_type_the_frame_is_an_internal_error(
        self, bad_message: object
    ) -> None:
        frame = _error(None, ErrorCode.TIMEOUT, bad_message)  # type: ignore[arg-type]  # on purpose

        assert frame.code is ErrorCode.INTERNAL


class TestRequestRefused:
    def test_it_takes_an_error_code_and_a_str_and_nothing_else(self) -> None:
        refusal = RequestRefused(ErrorCode.TIMEOUT, "slow")
        assert (refusal.code, refusal.message) == (ErrorCode.TIMEOUT, "slow")

        for code, message in [("timeout", "m"), (ErrorCode.TIMEOUT, 5), (None, None)]:
            with pytest.raises(InvalidValueError, match="ErrorCode and a str"):
                RequestRefused(code, message)  # type: ignore[arg-type]  # bad types on purpose


# --- helpers ----------------------------------------------------------------------------------


def request(body: Echo) -> RequestFrame:
    return RequestFrame(kind="request", id=IpcRequestId.new(), body=body)


def _frame(frame: RequestFrame) -> bytes:
    return encode_frame(encode_message(frame))


async def _client(path: SocketPath) -> Connection:
    return await connect(path, answer_diagnostics=False)


async def next_connection_closed(server: IpcServer) -> None:
    """Wait until the server has no open connection left."""
    while server.connections:  # noqa: ASYNC110  (the server has no signal for "closed")
        await asyncio.sleep(0.01)
