"""Writing to a peer that reads slowly or not at all: the queue is bounded and the peer is dropped.

The peers here are the hostile case: they take a connection and then never read. What must hold
is that memory stays bounded (no more than about twice the frame limit waits in the transport,
however many calls are made and however they end), that a sender waits within its own timeout,
that the reader never waits for the peer (so it can always take in what the peer sends), and
that a peer which takes nothing for the write timeout loses its connection, even when every
call that wrote to it has long given up.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from time import monotonic

import pytest

from tests.ipc.bounded import run
from tests.ipc.helpers import SMALL_FRAMES, fake_server, next_connection, raw_peer, serving
from tests.ipc.stalled import (
    CAP,
    FRAME,
    limits_for_a_stalled_peer,
    never_reads,
    round_of_calls,
    shake_hands,
)
from tumbleweed.core.ids import IpcRequestId
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.errors import ConnectionClosedError, RequestTimeoutError
from tumbleweed.ipc.framing import CLOSE_TIMEOUT, encode_frame
from tumbleweed.ipc.messages import Echo, Echoed, Ping
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.wire import RequestFrame, encode_message


class TestASenderIsBoundedByItsOwnTimeout:
    def test_calls_to_a_peer_that_does_not_read_give_up_in_time_and_queue_nothing_more(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            # A long write timeout: only the queue bound is under test here, not the stall watch.
            limits = limits_for_a_stalled_peer(write_timeout=timedelta(seconds=60))
            async with fake_server(sock_path, never_reads):
                client = await connect(sock_path, limits=limits, answer_diagnostics=False)
                try:
                    peak = 0
                    stuck_rounds = 0
                    for _ in range(400):
                        started = monotonic()
                        results = await round_of_calls(client, 50)
                        peak = max(peak, client.buffered_bytes)
                        assert monotonic() - started < 3  # every call kept to its 0.05 s
                        assert all(isinstance(r, RequestTimeoutError) for r in results), results
                        if client.buffered_bytes > FRAME:  # the transport has started to fill up
                            stuck_rounds += 1
                        if stuck_rounds == 10:  # ten more rounds against a full queue
                            break

                    assert stuck_rounds == 10, "the transport never filled up: raise the rounds"
                    assert peak <= CAP + FRAME  # over the cap by at most the frame that crossed it
                    assert not client.is_closed
                finally:
                    await client.close()

        run(scenario, deadline=60)

    def test_a_call_that_is_stuck_writing_still_ends_at_its_own_timeout(
        self, sock_path: SocketPath
    ) -> None:
        # With the queue full, `call` waits for room, not for an answer. Its timeout covers that.
        async def scenario() -> None:
            limits = limits_for_a_stalled_peer(write_timeout=timedelta(seconds=60))
            async with fake_server(sock_path, never_reads):
                client = await connect(sock_path, limits=limits, answer_diagnostics=False)
                try:
                    while client.buffered_bytes <= CAP:  # fill the queue (calls give up at once)
                        await round_of_calls(client, 50)

                    started = monotonic()
                    with pytest.raises(RequestTimeoutError):
                        await client.call(
                            Echo(kind="echo", text="z"),
                            Echoed,
                            request_timeout=timedelta(seconds=0.2),
                        )

                    assert monotonic() - started < 3
                finally:
                    await client.close()

        run(scenario, deadline=60)


class TestClosingWhileWritersAreStuck:
    def test_closing_wakes_a_call_stuck_behind_a_full_queue_at_once(
        self, sock_path: SocketPath
    ) -> None:
        # The call waits for room, not for an answer, so failing its answer does not free it. It
        # has to be woken by the transport going, and a transport that is flushing to a peer which
        # reads nothing goes only after the close grace period, unless it is aborted.
        async def scenario() -> None:
            limits = limits_for_a_stalled_peer(write_timeout=timedelta(seconds=60))
            async with fake_server(sock_path, never_reads):
                client = await connect(sock_path, limits=limits, answer_diagnostics=False)
                try:
                    while client.buffered_bytes <= CAP:
                        await round_of_calls(client, 50)
                    stuck = asyncio.create_task(
                        client.call(
                            Echo(kind="echo", text="z"),
                            Echoed,
                            request_timeout=timedelta(seconds=30),
                        )
                    )
                    await asyncio.sleep(0.1)  # it is waiting for room by now
                    assert not stuck.done()

                    started = monotonic()
                    await client.close()
                    with pytest.raises(ConnectionClosedError):
                        await stuck

                    assert monotonic() - started < CLOSE_TIMEOUT.total_seconds() * 0.75
                finally:
                    await client.close()

        run(scenario, deadline=60)


class TestAPeerThatStopsReadingIsDropped:
    def test_even_when_every_call_that_wrote_to_it_has_given_up(
        self, sock_path: SocketPath
    ) -> None:
        # The calls that wrote are cancelled by their timeouts, so no drain is waiting on the
        # peer any more. Something other than a waiting writer has to notice that it is stuck.
        async def scenario() -> None:
            async with fake_server(sock_path, never_reads):
                client = await connect(
                    sock_path, limits=limits_for_a_stalled_peer(), answer_diagnostics=False
                )
                try:
                    dropped = None
                    for _ in range(400):
                        results = await round_of_calls(client, 50)
                        dropped = next(
                            (r for r in results if isinstance(r, ConnectionClosedError)), None
                        )
                        if dropped is not None:
                            break

                    assert dropped is not None, "the peer was never dropped"
                    await client.wait_closed()
                    assert "not reading" in str(client.close_reason)
                    assert client.buffered_bytes == 0  # and nothing is held for it any more
                finally:
                    await client.close()

        run(scenario, deadline=60)

    def test_a_peer_that_reads_slowly_but_surely_is_not(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            async def slow_reader(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                await shake_hands(reader, writer)
                # Takes a little every so often, always inside the write timeout: far less than
                # the callers send, so the queue is full, yet the peer is reading.
                while await reader.read(65536):  # noqa: ASYNC110  (pacing a slow reader)
                    await asyncio.sleep(0.1)

            async with fake_server(sock_path, slow_reader):
                client = await connect(
                    sock_path, limits=limits_for_a_stalled_peer(), answer_diagnostics=False
                )
                try:
                    for _ in range(40):  # about 2 s of calls, twice the write timeout
                        results = await round_of_calls(client, 30)
                        assert not any(isinstance(r, ConnectionClosedError) for r in results)
                    assert not client.is_closed
                finally:
                    await client.close()

        run(scenario, deadline=60)


class TestTheReaderNeverWaitsForThePeer:
    @pytest.mark.parametrize("diagnostics", [False, True], ids=["refusals", "diagnostics"])
    def test_a_flood_that_is_answered_but_not_read_ends_the_connection_by_the_queue_bound(
        self, sock_path: SocketPath, diagnostics: bool
    ) -> None:
        # Refusals (and diagnostics) are written by the reader. A reader that waited for the
        # peer to read them would stop reading, and would sit in that wait until the write
        # timeout, here a minute. Instead the queue bound ends the connection at once.
        async def scenario() -> None:
            limits = replace(
                SMALL_FRAMES, max_inflight_handlers=1, write_timeout=timedelta(seconds=60)
            )
            async with serving(sock_path, limits=limits, diagnostics=diagnostics) as (
                server,
                handler,
            ):
                async with raw_peer(sock_path) as peer:
                    server_side = await next_connection(server)
                    if not diagnostics:  # hold the one slot, so that every other request is refused
                        held = RequestFrame(
                            kind="request",
                            id=IpcRequestId.new(),
                            body=Echo(kind="echo", text="hold:x"),
                        )
                        peer.send_bytes(encode_frame(encode_message(held)))
                        await handler.started["x"].wait()
                    peer.send_bytes(
                        b"".join(
                            encode_frame(
                                encode_message(
                                    RequestFrame(
                                        kind="request",
                                        id=IpcRequestId.new(),
                                        body=Ping(kind="ping"),
                                    )
                                )
                            )
                            for _ in range(30000)
                        )
                    )

                    started = monotonic()
                    await server_side.wait_closed()

                assert monotonic() - started < 30
                assert "pile up" in str(server_side.close_reason)

        run(scenario, deadline=60)
