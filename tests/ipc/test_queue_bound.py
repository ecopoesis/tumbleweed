"""The bound on what waits to be written is real, and it only ever drops peers that do not read.

Three properties of the write path, each found by a probe that broke it:

- However many senders are waiting when the transport drains (asyncio wakes them all at once),
  the queue never exceeds the cap by more than the frame that crossed it.
- A peer that is reading, even slowly, even while the queue is full, is not dropped for what the
  reader writes to it (diagnostics, refusals): only the stall watch decides who is not reading.
- A request is sent once its frame is in the transport: a call whose answer has arrived does not
  time out waiting for the queue to drain, and a handler's slot does not wait for it either.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from time import monotonic

import pytest

from tests.ipc.bounded import run
from tests.ipc.helpers import SHORT, SMALL_FRAMES, connected, fake_server, raw_peer, serving
from tests.ipc.stalled import (
    BIG,
    CAP,
    FRAME,
    limits_for_a_stalled_peer,
    shake_hands,
)
from tumbleweed.core.ids import IpcRequestId
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import FramingError, RequestTimeoutError
from tumbleweed.ipc.framing import encode_frame, read_frame
from tumbleweed.ipc.limits import MAX_FRAME_BYTES, IpcLimits
from tumbleweed.ipc.messages import Echo, Echoed, Ping
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.wire import (
    Frame,
    RequestFrame,
    ResponseFrame,
    decode_message,
    encode_message,
)


def echo(text: str) -> Echo:
    return Echo(kind="echo", text=text)


def frame_of(body: Echo | Ping) -> bytes:
    return encode_frame(
        encode_message(RequestFrame(kind="request", id=IpcRequestId.new(), body=body))
    )


class TestTheCapHoldsWhenSendersAreWokenTogether:
    def test_a_peer_that_reads_in_bursts_never_lets_the_queue_exceed_the_cap_by_a_frame(
        self, sock_path: SocketPath, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The peer reads a burst, then pauses. Each time the queue drains, asyncio wakes every
        # sender that waits; if each wrote without looking again, the queue would jump by one
        # frame per sender (84 KB over a cap of 8 KB with 32 calls, 913 KB with 300).
        peaks: list[int] = []
        real_write = Connection.__dict__["_write"]

        def recording(self: Connection, data: bytes) -> None:
            real_write(self, data)
            peaks.append(self.buffered_bytes)

        monkeypatch.setattr(Connection, "_write", recording)

        async def bursty_reader(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await shake_hands(reader, writer)
            while await reader.read(32768):  # noqa: ASYNC110  (pacing a bursty reader)
                await asyncio.sleep(0.2)

        async def scenario() -> int:
            limits = limits_for_a_stalled_peer(write_timeout=timedelta(seconds=60))
            async with fake_server(sock_path, bursty_reader):
                client = await connect(sock_path, limits=limits, answer_diagnostics=False)
                try:
                    await asyncio.gather(
                        *(
                            client.call(echo(BIG), Echoed, request_timeout=timedelta(seconds=3))
                            for _ in range(300)
                        ),
                        return_exceptions=True,
                    )
                finally:
                    await client.close()
            return len(frame_of(echo(BIG)))

        frame = run(scenario, deadline=60)

        assert len(peaks) > 100  # many writes were measured ...
        assert max(peaks) > FRAME  # ... and the queue did fill up
        assert max(peaks) <= CAP + frame  # but never by more than the frame that crossed the cap


class TestTheReaderDoesNotDropReadingPeers:
    @pytest.mark.parametrize("diagnostic", [Ping(kind="ping"), echo("are you there")])
    def test_a_peer_that_reads_slowly_and_pings_while_the_queue_is_full_is_not_dropped(
        self, sock_path: SocketPath, diagnostic: Ping | Echo
    ) -> None:
        # The reader answers a ping at once, and under backpressure the queue is over the cap,
        # which is the normal state. That alone does not mean that the peer is not reading.
        async def scenario() -> None:
            queue_is_full = asyncio.Event()
            pinged = asyncio.Event()
            seen: list[Frame] = []

            async def slow_pinging_reader(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                await shake_hands(reader, writer)

                async def pinger() -> None:
                    await queue_is_full.wait()
                    for _ in range(6):
                        body = diagnostic
                        request = RequestFrame(kind="request", id=IpcRequestId.new(), body=body)
                        writer.write(encode_frame(encode_message(request)))
                        await writer.drain()
                        await asyncio.sleep(0.3)
                    pinged.set()

                ping_task = asyncio.create_task(pinger())
                try:
                    while (
                        payload := await read_frame(
                            reader, max_bytes=MAX_FRAME_BYTES, frame_timeout=timedelta(seconds=5)
                        )
                    ) is not None:
                        seen.append(decode_message(payload))
                        await asyncio.sleep(0.02)  # about 150 KB/s: slow, but reading
                except (OSError, FramingError):  # the test drops the connection
                    pass
                finally:
                    ping_task.cancel()
                    await asyncio.gather(ping_task, return_exceptions=True)

            limits = limits_for_a_stalled_peer(write_timeout=timedelta(seconds=60))
            async with fake_server(sock_path, slow_pinging_reader):
                client = await connect(sock_path, limits=limits)  # answers diagnostics itself
                try:
                    calls = [
                        asyncio.create_task(
                            client.call(echo(BIG), Echoed, request_timeout=timedelta(seconds=20))
                        )
                        for _ in range(300)
                    ]
                    while client.buffered_bytes <= CAP:  # noqa: ASYNC110  (until the queue fills)
                        await asyncio.sleep(0.02)
                    queue_is_full.set()

                    await pinged.wait()  # six pings, 1.8 s, all arriving over a full queue

                    assert not client.is_closed
                    for call in calls:
                        call.cancel()
                    await asyncio.gather(*calls, return_exceptions=True)
                finally:
                    await client.close()

        run(scenario, deadline=60)

    def test_a_well_behaved_client_reading_slowly_is_never_dropped(
        self, sock_path: SocketPath
    ) -> None:
        # Never more than 16 requests outstanding, each answered by the connection with 3 KB,
        # read at about 300 KB/s: nothing here is a flood, and nothing may end the connection.
        async def scenario() -> None:
            async with (
                serving(sock_path, limits=SMALL_FRAMES, diagnostics=True) as (server, _handler),
                raw_peer(sock_path) as peer,
            ):
                total, window = 300, 16
                sent = answered = 0
                while sent < min(window, total):
                    peer.send_bytes(frame_of(echo(BIG)))
                    sent += 1
                while answered < total:
                    reply = await peer.recv()
                    assert isinstance(reply, ResponseFrame), reply  # nothing closed it
                    answered += 1
                    await asyncio.sleep(0.01)
                    if sent < total:
                        peer.send_bytes(frame_of(echo(BIG)))
                        sent += 1

                assert len(server.connections) == 1

        run(scenario, deadline=60)


class TestARequestIsSentOnceItsFrameIsInTheTransport:
    def test_a_call_whose_answer_has_arrived_does_not_time_out_for_a_full_queue(self) -> None:
        # The peer answers at once (the answer is already in the reader when `call` gets back
        # from the write) and the queue is full and never drains. The call has its answer.
        class Full:
            """A transport whose queue is empty until something is written, then stays full."""

            def __init__(self) -> None:
                self.size = 0

            def set_write_buffer_limits(
                self, high: int | None = None, low: int | None = None
            ) -> None:
                pass

            def get_write_buffer_size(self) -> int:
                return self.size

            def abort(self) -> None:
                pass

        class AnsweringWriter:
            def __init__(self, reader: asyncio.StreamReader) -> None:
                self.reader = reader
                self.transport = Full()

            def write(self, data: bytes) -> None:
                request = decode_message(data[4:])
                assert isinstance(request, RequestFrame)
                assert isinstance(request.body, Echo)
                answer = ResponseFrame(
                    kind="response",
                    id=request.id,
                    body=Echoed(kind="echoed", text=request.body.text),
                )
                self.reader.feed_data(encode_frame(encode_message(answer)))
                self.transport.size = 10**9  # and now the queue is over any cap, for good

            async def drain(self) -> None:
                await asyncio.Event().wait()  # never drains

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        async def scenario() -> float:
            reader = asyncio.StreamReader()
            connection = Connection(
                reader,
                AnsweringWriter(reader),  # type: ignore[arg-type]  # a stand-in writer
                limits=IpcLimits(),
                handler=None,
            )
            try:
                started = monotonic()
                answer = await connection.call(
                    echo("hello"), Echoed, request_timeout=timedelta(seconds=5)
                )
                assert answer.text == "hello"
                return monotonic() - started
            finally:
                await connection.close()

        assert run(scenario) < 2  # at once, not at the five-second timeout

    def test_calls_whose_answers_arrived_succeed_though_the_peer_then_stops_reading(
        self, sock_path: SocketPath
    ) -> None:
        answered: list[str] = []

        async def answers_some_then_stops(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await shake_hands(reader, writer)
            for _ in range(20):
                payload = await read_frame(
                    reader, max_bytes=MAX_FRAME_BYTES, frame_timeout=timedelta(seconds=5)
                )
                assert payload is not None
                request = decode_message(payload)
                assert isinstance(request, RequestFrame)
                assert isinstance(request.body, Echo)
                answered.append(request.body.text)
                reply = ResponseFrame(
                    kind="response",
                    id=request.id,
                    body=Echoed(kind="echoed", text=request.body.text),
                )
                writer.write(encode_frame(encode_message(reply)))
                await writer.drain()
            while not writer.transport.is_closing():  # noqa: ASYNC110  (it reads no more)
                await asyncio.sleep(0.05)

        async def scenario() -> None:
            limits = limits_for_a_stalled_peer(write_timeout=timedelta(seconds=60))
            async with fake_server(sock_path, answers_some_then_stops):
                client = await connect(sock_path, limits=limits, answer_diagnostics=False)
                try:
                    texts = [f"{i:04d}" + "€" * 990 for i in range(150)]
                    outcomes = await asyncio.gather(
                        *(
                            client.call(echo(t), Echoed, request_timeout=timedelta(seconds=1.5))
                            for t in texts
                        ),
                        return_exceptions=True,
                    )
                finally:
                    await client.close()

            for text, outcome in zip(texts, outcomes, strict=True):
                if text in answered:
                    assert isinstance(outcome, Echoed), (text[:4], outcome)
                else:
                    assert isinstance(outcome, RequestTimeoutError), (text[:4], outcome)

        run(scenario, deadline=60)

    def test_a_handler_slot_is_free_once_its_reply_is_written_whatever_the_queue(
        self, sock_path: SocketPath
    ) -> None:
        # With the queue under the cap there is nothing to wait for after the write: slots turn
        # over at the speed of the handlers, with a peer that has read nothing at all.
        async def scenario() -> None:
            limits = replace(SMALL_FRAMES, max_inflight_handlers=1, write_timeout=SHORT * 60)
            async with (
                serving(sock_path, limits=limits) as (_server, handler),
                connected(sock_path) as client,
            ):
                for i in range(20):
                    assert (await client.call(echo(f"n{i}"), Echoed)).text == f"n{i}"

                assert handler.peak_tasks == 1

        run(scenario, deadline=60)
