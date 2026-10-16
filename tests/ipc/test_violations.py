"""What a hostile or broken peer can do to an established connection, and what it gets for it.

Each case drives a raw peer past the handshake and sends something the protocol does not allow.
The server side of the connection must end, for the right reason, say so to the peer in one
structured error, and leave the server able to serve the next client. The cases are the
security claims of the IPC layer, so each states what it would take to break it.
"""

import asyncio
import struct

import pytest

from tests.ipc.bounded import run
from tests.ipc.helpers import (
    SHORT,
    SMALL_FRAMES,
    RawPeer,
    connected,
    fake_server,
    fast,
    next_connection,
    raw_peer,
    serving,
)
from tumbleweed.core.ids import IpcRequestId
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import (
    ConnectionClosedError,
    ErrorCode,
    FrameTimeoutError,
    FrameTooLargeError,
    FramingError,
    IdleTimeoutError,
    IpcError,
    MalformedFrameError,
    ProtocolViolationError,
    RemoteError,
    TruncatedFrameError,
)
from tumbleweed.ipc.framing import encode_frame
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.messages import Echo, Echoed, Ping, Pong
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.wire import (
    PROTOCOL_VERSION,
    ErrorFrame,
    Frame,
    Hello,
    RequestFrame,
    ResponseFrame,
    encode_message,
)

HELLO = Hello(kind="hello", version=PROTOCOL_VERSION)


def echo(text: str) -> Echo:
    return Echo(kind="echo", text=text)


def request(body: Echo | Ping, request_id: IpcRequestId | None = None) -> RequestFrame:
    return RequestFrame(kind="request", id=request_id or IpcRequestId.new(), body=body)


async def ended(server_side: Connection, peer: RawPeer) -> tuple[IpcError | None, list[Frame]]:
    """Wait for the server to end its side, then collect the reason and what the peer received."""
    await server_side.wait_closed()
    frames = await peer.drain_until_closed()
    return server_side.close_reason, frames


async def server_still_serves(path: SocketPath) -> None:
    async with connected(path) as client:
        assert await client.call(Ping(kind="ping"), Pong) == Pong(kind="pong")


def violation(frames: list[Frame]) -> ErrorFrame:
    """The one structured error the peer is sent before the connection ends."""
    assert len(frames) == 1, frames
    (goodbye,) = frames
    assert isinstance(goodbye, ErrorFrame)
    assert goodbye.id is None
    assert goodbye.code is ErrorCode.PROTOCOL_VIOLATION
    assert 0 < len(goodbye.message) <= 256
    return goodbye


# What the peer sends, and why the connection must end because of it.
VIOLATIONS: dict[str, tuple[bytes, type[IpcError]]] = {
    "a payload that is not JSON": (encode_frame(b"this is not json"), MalformedFrameError),
    "a payload that is not UTF-8": (encode_frame(b'{"kind":"\xff\xfe"}'), MalformedFrameError),
    "JSON nested a million brackets deep": (
        encode_frame(b"[" * (1024 * 1024 - 8)),
        MalformedFrameError,
    ),
    "a frame of another kind than the protocol has": (
        encode_frame(b'{"kind":"goodbye"}'),
        MalformedFrameError,
    ),
    "a request of a kind that is not registered": (
        encode_frame(
            b'{"kind":"request","id":"%s","body":{"kind":"launch"}}'
            % str(IpcRequestId.new()).encode()
        ),
        MalformedFrameError,
    ),
    "a request with an extra field": (
        encode_frame(
            b'{"kind":"request","id":"%s","body":{"kind":"ping"},"extra":1}'
            % str(IpcRequestId.new()).encode()
        ),
        MalformedFrameError,
    ),
    "a zero-length frame": (struct.pack(">I", 0), FramingError),
    "a hello after the handshake": (
        encode_frame(encode_message(HELLO)),
        ProtocolViolationError,
    ),
}


class TestMalformedInput:
    @pytest.mark.parametrize(("sent", "reason"), VIOLATIONS.values(), ids=list(VIOLATIONS))
    def test_the_connection_ends_for_the_right_reason_and_the_peer_is_told(
        self, sock_path: SocketPath, sent: bytes, reason: type[IpcError]
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path) as (server, _handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)
                peer.send_bytes(sent)

                why, frames = await ended(server_side, peer)

                assert type(why) is reason
                violation(frames)
                await server_still_serves(sock_path)

        run(scenario)

    def test_an_oversize_frame_is_refused_from_its_header_alone(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=SMALL_FRAMES) as (server, _handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)
                # Five bytes over the limit, and not one byte of the body is ever sent: a
                # server that waited for the body would never get to end the connection.
                peer.send_bytes(struct.pack(">I", 4096 + 1))

                why, frames = await ended(server_side, peer)

                assert type(why) is FrameTooLargeError
                assert "4097" in str(why)
                violation(frames)
                await server_still_serves(sock_path)

        run(scenario)

    def test_a_frame_exactly_at_the_limit_is_not_oversize(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=SMALL_FRAMES) as (server, _handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)
                padding = b" " * (4096 - len(encode_message(request(Ping(kind="ping")))))
                frame_id = IpcRequestId.new()
                payload = encode_message(request(Ping(kind="ping"), frame_id)) + padding
                assert len(payload) == 4096
                peer.send_bytes(encode_frame(payload))

                answer = await peer.recv()

                assert answer == ResponseFrame(kind="response", id=frame_id, body=Pong(kind="pong"))
                assert not server_side.is_closed

        run(scenario)

    def test_a_frame_cut_short_by_the_peer_closing_is_truncation(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path) as (server, _handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)
                peer.send_bytes(struct.pack(">I", 10) + b"abc")
                peer.writer.write_eof()

                why, _frames = await ended(server_side, peer)

                assert type(why) is TruncatedFrameError
                await server_still_serves(sock_path)

        run(scenario)

    def test_a_duplicate_request_id_while_the_first_is_in_progress_ends_the_connection(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path) as (server, handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)
                request_id = IpcRequestId.new()
                await peer.send(request(echo("hold:x"), request_id))
                await handler.started["x"].wait()
                await peer.send(request(Ping(kind="ping"), request_id))  # the same id again

                why, frames = await ended(server_side, peer)

                assert type(why) is ProtocolViolationError
                assert "reused a request id" in str(why)
                assert handler.cancelled == ["x"]
                violation(frames)

        run(scenario)

    def test_an_id_may_be_used_again_once_its_request_has_been_answered(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path) as (server, _handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)
                request_id = IpcRequestId.new()
                for _ in range(2):
                    await peer.send(request(Ping(kind="ping"), request_id))
                    assert isinstance(await peer.recv(), ResponseFrame)

                assert not server_side.is_closed

        run(scenario)


class TestAnswersThatDoNotBelong:
    def test_an_answer_to_no_waiting_request_is_ignored_and_the_connection_goes_on(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, _handler), raw_peer(sock_path) as peer:
                server_side = await next_connection(server)
                stray = IpcRequestId.new()
                await peer.send(
                    ResponseFrame(kind="response", id=stray, body=Echoed(kind="echoed", text="?"))
                )
                await peer.send(
                    ErrorFrame(kind="error", id=stray, code=ErrorCode.INTERNAL, message="?")
                )
                await peer.send(request(Ping(kind="ping")))

                answer = await peer.recv()

                assert isinstance(answer, ResponseFrame)  # the ping, after both strays
                assert not server_side.is_closed

        run(scenario)

    def test_an_answer_from_another_connection_does_not_answer_a_request(
        self, sock_path: SocketPath
    ) -> None:
        # Answers count only from the connection the request was sent on (I5; S18 relies on it).
        async def scenario() -> None:
            async with serving(sock_path) as (server, _handler), raw_peer(sock_path) as asked:
                asked_side = await next_connection(server)
                async with raw_peer(sock_path) as bystander:
                    bystander_side = await next_connection(server, not_in=frozenset({asked_side}))
                    question = asyncio.create_task(asked_side.call(echo("question"), Echoed))
                    sent = await asked.recv()
                    assert isinstance(sent, RequestFrame)

                    forged = Echoed(kind="echoed", text="forged")
                    await bystander.send(ResponseFrame(kind="response", id=sent.id, body=forged))
                    await bystander.send(
                        ErrorFrame(kind="error", id=sent.id, code=ErrorCode.INTERNAL, message="x")
                    )
                    await bystander.send(request(Ping(kind="ping")))
                    assert isinstance(await bystander.recv(), ResponseFrame)  # both forgeries are
                    assert not question.done()  # behind us in the stream: seen, and not accepted

                    real = Echoed(kind="echoed", text="real")
                    await asked.send(ResponseFrame(kind="response", id=sent.id, body=real))
                    assert (await question).text == "real"
                    assert not asked_side.is_closed
                    assert not bystander_side.is_closed

        run(scenario)

    def test_a_fatal_error_frame_from_the_peer_ends_the_connection_without_an_answer(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, _handler), raw_peer(sock_path) as peer:
                server_side = await next_connection(server)
                await peer.send(
                    ErrorFrame(kind="error", id=None, code=ErrorCode.INTERNAL, message="bye")
                )

                why, frames = await ended(server_side, peer)

                assert isinstance(why, RemoteError)
                assert why.code is ErrorCode.INTERNAL
                assert frames == []  # the peer said it was leaving: nothing is sent back

        run(scenario)


class TestSlowAndSilentPeers:
    def test_a_peer_that_stalls_inside_a_frame_is_dropped(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=fast()) as (server, _handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)
                peer.send_bytes(b"\x00\x00")  # half a header, then nothing

                why, frames = await ended(server_side, peer)

                assert type(why) is FrameTimeoutError
                violation(frames)
                await server_still_serves(sock_path)

        run(scenario)

    def test_a_peer_that_trickles_a_frame_a_byte_at_a_time_is_dropped_whatever_its_pace(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=fast()) as (server, _handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)
                frame = encode_frame(encode_message(request(Ping(kind="ping"))))

                async def trickle() -> None:
                    for byte in frame:  # each byte comes well inside the frame timeout
                        peer.send_bytes(bytes([byte]))
                        await asyncio.sleep(0.05)

                feeder = asyncio.create_task(trickle())
                try:
                    why, _frames = await ended(server_side, peer)
                finally:
                    feeder.cancel()
                    await asyncio.gather(feeder, return_exceptions=True)

                assert type(why) is FrameTimeoutError

        run(scenario)

    def test_an_idle_timeout_ends_a_silent_connection_and_says_so(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            limits = fast(idle_timeout=SHORT)
            async with (
                serving(sock_path, limits=limits) as (server, _handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)

                why, frames = await ended(server_side, peer)

                assert type(why) is IdleTimeoutError
                (goodbye,) = frames
                assert isinstance(goodbye, ErrorFrame)
                assert goodbye.code is ErrorCode.TIMEOUT

        run(scenario)

    def test_by_default_a_quiet_connection_is_left_alone(self, sock_path: SocketPath) -> None:
        assert IpcLimits().idle_timeout is None

        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                # Longer than the idle timeout that the test above shows ending a connection.
                await asyncio.sleep(SHORT.total_seconds() * 1.5)

                assert await client.call(Ping(kind="ping"), Pong) == Pong(kind="pong")

        run(scenario)

    def test_a_peer_that_does_not_read_is_dropped_when_a_write_to_it_stalls(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=fast()) as (server, _handler),
                raw_peer(sock_path) as peer,
            ):
                server_side = await next_connection(server)
                # Answers are 1 KiB each, and this peer never reads one: the socket buffers
                # and the writer's buffer fill, and the server's write must give up.
                flood = b"".join(
                    encode_frame(encode_message(request(echo("x" * 1000)))) for _ in range(3000)
                )
                peer.send_bytes(flood)

                await server_side.wait_closed()

                assert isinstance(server_side.close_reason, ConnectionClosedError)
                assert "not reading" in str(server_side.close_reason)
                await server_still_serves(sock_path)

        run(scenario)


class TestTheClientSideOfTheSameRules:
    def test_a_server_that_breaks_the_protocol_ends_the_clients_connection_and_its_calls(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> bytes:
            received = bytearray()
            finished = asyncio.Event()

            async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                await reader.readexactly(4 + len(encode_message(HELLO)))
                writer.write(encode_frame(encode_message(HELLO)))
                await writer.drain()
                await reader.readexactly(4)  # the request's header, so that the call is waiting
                writer.write(encode_frame(b"this is not json"))
                await writer.drain()
                received.extend(await reader.read())  # the client's goodbye, then its end
                finished.set()

            async with fake_server(sock_path, script):
                client = await connect(sock_path)
                try:
                    with pytest.raises(ConnectionClosedError) as raised:
                        await client.call(Ping(kind="ping"), Pong)
                    await client.wait_closed()
                finally:
                    await client.close()
                assert isinstance(raised.value.__cause__, MalformedFrameError)
                assert type(client.close_reason) is MalformedFrameError
                await finished.wait()
            return bytes(received)

        assert b'"code":"protocol_violation"' in run(scenario)
