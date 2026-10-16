"""Framing: the length prefix, the size limit, and what a hostile byte stream can do to a reader.

The reader is driven with an `asyncio.StreamReader` that the test feeds by hand, so every case is
about bytes and time only, with no socket in between. Property tests use hypothesis with a fixed
seed (`derandomize`), so that a run in CI tests exactly what a run on a laptop did; a failure
prints the example that broke it.
"""

import asyncio
import contextlib
import struct
from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.ipc.bounded import run
from tumbleweed.core.types import ByteCount
from tumbleweed.ipc.errors import (
    FrameTimeoutError,
    FrameTooLargeError,
    FramingError,
    IdleTimeoutError,
    TruncatedFrameError,
)
from tumbleweed.ipc.framing import HEADER_BYTES, close_writer, encode_frame, read_frame
from tumbleweed.ipc.limits import MAX_FRAME_BYTES, MIN_FRAME_BYTES

PROPERTY = settings(derandomize=True, deadline=None, database=None, max_examples=200)
LIMIT = ByteCount(4096)
PATIENT = timedelta(seconds=30)  # a frame timeout that never fires in these tests


async def read_all(reader: asyncio.StreamReader, *, max_bytes: ByteCount = LIMIT) -> list[bytes]:
    frames = []
    while (
        payload := await read_frame(reader, max_bytes=max_bytes, frame_timeout=PATIENT)
    ) is not None:
        frames.append(payload)
    return frames


class TestEncoding:
    def test_the_prefix_is_four_bytes_big_endian_then_the_payload(self) -> None:
        payload = b"x" * 258

        assert encode_frame(payload) == b"\x00\x00\x01\x02" + payload
        assert HEADER_BYTES == 4

    def test_the_limit_is_the_largest_payload_and_one_more_is_refused(self) -> None:
        assert len(encode_frame(b"x" * LIMIT, max_bytes=LIMIT)) == LIMIT + HEADER_BYTES
        with pytest.raises(FrameTooLargeError, match="4097 bytes"):
            encode_frame(b"x" * (LIMIT + 1), max_bytes=LIMIT)

    def test_the_default_limit_is_the_hard_maximum_of_one_mebibyte(self) -> None:
        assert MAX_FRAME_BYTES == 1024 * 1024
        assert len(encode_frame(b"x" * MAX_FRAME_BYTES)) == MAX_FRAME_BYTES + HEADER_BYTES
        with pytest.raises(FrameTooLargeError):
            encode_frame(b"x" * (MAX_FRAME_BYTES + 1))

    def test_an_empty_payload_is_refused(self) -> None:
        with pytest.raises(FramingError, match="empty"):
            encode_frame(b"")

    def test_the_floor_of_the_limit_leaves_room_for_every_control_frame(self) -> None:
        assert MIN_FRAME_BYTES == 4096


class TestReading:
    def test_frames_come_out_in_order_and_the_end_between_frames_is_none(self) -> None:
        async def scenario() -> list[bytes]:
            reader = asyncio.StreamReader()
            reader.feed_data(encode_frame(b"one") + encode_frame(b"two"))
            reader.feed_eof()
            return await read_all(reader)

        assert run(scenario) == [b"one", b"two"]

    def test_a_frame_of_exactly_the_limit_is_accepted(self) -> None:
        async def scenario() -> list[bytes]:
            reader = asyncio.StreamReader()
            reader.feed_data(encode_frame(b"x" * LIMIT, max_bytes=LIMIT))
            reader.feed_eof()
            return await read_all(reader)

        assert run(scenario) == [b"x" * LIMIT]

    @pytest.mark.parametrize("bytes_sent", [1, 2, 3])
    def test_the_stream_ending_inside_the_header_is_truncation(self, bytes_sent: int) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(encode_frame(b"abc")[:bytes_sent])
            reader.feed_eof()
            await read_all(reader)

        with pytest.raises(TruncatedFrameError):
            run(scenario)

    @pytest.mark.parametrize("bytes_of_body", [0, 1, 4])
    def test_the_stream_ending_inside_the_body_is_truncation(self, bytes_of_body: int) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(encode_frame(b"abcde")[: HEADER_BYTES + bytes_of_body])
            reader.feed_eof()
            await read_all(reader)

        with pytest.raises(TruncatedFrameError):
            run(scenario)

    def test_a_zero_length_frame_is_refused_without_reading_further(self) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(
                struct.pack(">I", 0)
            )  # no EOF: nothing follows, and nothing is awaited
            await read_frame(reader, max_bytes=LIMIT, frame_timeout=PATIENT)

        with pytest.raises(FramingError, match="empty"):
            run(scenario, deadline=5)

    def test_one_byte_over_the_limit_is_refused_before_any_of_the_body_arrives(self) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(struct.pack(">I", LIMIT + 1))  # the body never comes
            await read_frame(reader, max_bytes=LIMIT, frame_timeout=PATIENT)

        # A reader that waited for the body would sit here until the deadline, not raise.
        with pytest.raises(FrameTooLargeError, match=str(LIMIT + 1)):
            run(scenario, deadline=5)

    def test_a_four_gibibyte_announcement_is_refused_just_the_same(self) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(b"\xff\xff\xff\xff")
            await read_frame(reader, max_bytes=MAX_FRAME_BYTES, frame_timeout=PATIENT)

        with pytest.raises(FrameTooLargeError):
            run(scenario, deadline=5)


class TestTimeouts:
    def test_a_header_that_stalls_after_one_byte_is_a_frame_timeout(self) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(b"\x00")  # then silence, and no EOF
            await read_frame(reader, max_bytes=LIMIT, frame_timeout=timedelta(seconds=0.1))

        with pytest.raises(FrameTimeoutError):
            run(scenario, deadline=5)

    def test_a_body_that_stalls_is_a_frame_timeout(self) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(encode_frame(b"abcdef")[:-1])
            await read_frame(reader, max_bytes=LIMIT, frame_timeout=timedelta(seconds=0.1))

        with pytest.raises(FrameTimeoutError):
            run(scenario, deadline=5)

    def test_a_frame_trickled_one_byte_at_a_time_runs_out_of_one_total_budget(self) -> None:
        # Slowloris: each byte arrives well inside the timeout, but the frame as a whole does not.
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            frame = encode_frame(b"x" * 200)

            async def trickle() -> None:
                for byte in frame:
                    reader.feed_data(bytes([byte]))
                    await asyncio.sleep(0.02)

            feeder = asyncio.create_task(trickle())
            try:
                await read_frame(reader, max_bytes=LIMIT, frame_timeout=timedelta(seconds=0.3))
            finally:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)

        with pytest.raises(FrameTimeoutError):
            run(scenario, deadline=10)

    def test_a_quiet_connection_may_stay_quiet_longer_than_the_frame_timeout(self) -> None:
        # The frame timeout starts at the first byte of a frame, not before it.
        async def scenario() -> bytes | None:
            reader = asyncio.StreamReader()
            waiting = asyncio.create_task(
                read_frame(reader, max_bytes=LIMIT, frame_timeout=timedelta(seconds=0.1))
            )
            await asyncio.sleep(0.4)
            reader.feed_data(encode_frame(b"late"))
            return await waiting

        assert run(scenario) == b"late"

    def test_an_idle_timeout_ends_the_wait_for_the_next_frame(self) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            await read_frame(
                reader,
                max_bytes=LIMIT,
                frame_timeout=PATIENT,
                idle_timeout=timedelta(seconds=0.1),
            )

        with pytest.raises(IdleTimeoutError):
            run(scenario, deadline=5)


class TestProperties:
    @PROPERTY
    @given(
        payloads=st.lists(st.binary(min_size=1, max_size=300), max_size=6),
        cuts=st.lists(st.integers(min_value=0), max_size=12),
    )
    def test_frames_survive_arrival_in_any_fragments(
        self, payloads: list[bytes], cuts: list[int]
    ) -> None:
        stream = b"".join(encode_frame(payload) for payload in payloads)
        points = sorted({cut % (len(stream) + 1) for cut in cuts})
        pieces = [stream[a:b] for a, b in zip([0, *points], [*points, len(stream)], strict=True)]

        async def scenario() -> list[bytes]:
            reader = asyncio.StreamReader()

            async def feed() -> None:
                for piece in pieces:
                    reader.feed_data(piece)
                    await asyncio.sleep(0)  # the reader gets to run between the pieces
                reader.feed_eof()

            feeder = asyncio.create_task(feed())
            try:
                return await read_all(reader)
            finally:
                await feeder

        assert run(scenario) == payloads

    @PROPERTY
    @given(data=st.binary(max_size=2048))
    def test_garbage_ends_in_frames_or_a_framing_error_and_never_hangs(self, data: bytes) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(data)
            reader.feed_eof()
            with contextlib.suppress(FramingError):  # the only way a reader may refuse
                await read_all(reader)

        run(scenario, deadline=5)

    @settings(derandomize=True, deadline=None, database=None, max_examples=30)
    @given(
        length=st.integers(min_value=LIMIT + 1, max_value=2**32 - 1),
        trailing=st.binary(max_size=40),
    )
    def test_any_announced_length_over_the_limit_is_refused_whatever_follows(
        self, length: int, trailing: bytes
    ) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(struct.pack(">I", length) + trailing)  # never completes the body
            await read_frame(reader, max_bytes=LIMIT, frame_timeout=PATIENT)

        with pytest.raises(FrameTooLargeError):
            run(scenario, deadline=5)

    @PROPERTY
    @given(
        payloads=st.lists(st.binary(min_size=1, max_size=200), min_size=1, max_size=6),
        data=st.data(),
    )
    def test_a_stream_cut_inside_a_frame_gives_the_frames_before_it_then_truncation(
        self, payloads: list[bytes], data: st.DataObject
    ) -> None:
        frames = [encode_frame(payload) for payload in payloads]
        index = data.draw(st.integers(min_value=0, max_value=len(frames) - 1), label="frame")
        start = sum(len(frame) for frame in frames[:index])
        # Strictly inside the frame: after at least one of its bytes, before its last one. A frame
        # is at least five bytes (four of header, one of payload), so there is always a place.
        inside = data.draw(st.integers(min_value=1, max_value=len(frames[index]) - 1), label="cut")
        stream = b"".join(frames)[: start + inside]

        async def scenario() -> tuple[list[bytes], bool]:
            reader = asyncio.StreamReader()
            reader.feed_data(stream)
            reader.feed_eof()
            complete: list[bytes] = []
            try:
                while (
                    payload := await read_frame(reader, max_bytes=LIMIT, frame_timeout=PATIENT)
                ) is not None:
                    complete.append(payload)
            except TruncatedFrameError:
                return complete, True
            return complete, False

        complete, truncated = run(scenario)

        assert truncated  # the cut was inside a frame, so the end of the stream is no clean end
        assert complete == payloads[:index]  # and every frame before it came out whole

    @PROPERTY
    @given(payload=st.binary(min_size=1, max_size=500))
    def test_what_is_encoded_is_what_is_read(self, payload: bytes) -> None:
        async def scenario() -> list[bytes]:
            reader = asyncio.StreamReader()
            reader.feed_data(encode_frame(payload))
            reader.feed_eof()
            return await read_all(reader)

        assert run(scenario) == [payload]


class StubTransport:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


class StubWriter:
    """Stands in for a `StreamWriter` whose transport finishes closing as `closing` says."""

    def __init__(self, closing: str) -> None:
        self.transport = StubTransport()
        self.closing = closing
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        if self.closing == "never":
            await asyncio.Event().wait()
        if self.closing == "fails":
            raise ConnectionResetError("the peer reset the connection while closing")


class TestClosingAWriter:
    @pytest.mark.parametrize("closing", ["never", "fails"])
    def test_a_transport_that_does_not_close_is_aborted(self, closing: str) -> None:
        writer = StubWriter(closing)

        async def scenario() -> None:
            await close_writer(writer, within=timedelta(seconds=0.1))  # type: ignore[arg-type]  # a stand-in for a writer that will not close

        run(scenario, deadline=5)

        assert writer.closed
        assert writer.transport.aborted

    def test_a_transport_that_closes_is_not_aborted(self) -> None:
        writer = StubWriter("promptly")

        async def scenario() -> None:
            await close_writer(writer, within=timedelta(seconds=5))  # type: ignore[arg-type]  # a stand-in for a writer that closes at once

        run(scenario, deadline=5)

        assert writer.closed
        assert not writer.transport.aborted
