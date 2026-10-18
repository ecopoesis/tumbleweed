"""`Utterance` and `BoundedPcm`: audio is bounded where it arrives (I9)."""

import threading

import pytest

from tests.voice.support import max_bytes, pcm
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import ByteCount
from tumbleweed.voice.audio import BoundedPcm, Utterance


class TestUtterance:
    def test_it_holds_whole_samples_and_knows_its_length(self) -> None:
        utterance = Utterance(pcm(1.5), max_bytes=max_bytes(2))

        assert utterance.byte_count == 48_000
        assert utterance.duration.total_seconds() == pytest.approx(1.5)
        assert utterance.pcm == pcm(1.5)

    def test_an_empty_one_is_valid(self) -> None:
        assert Utterance(b"", max_bytes=max_bytes(1)).duration.total_seconds() == 0

    def test_exactly_the_cap_is_fine_and_one_sample_over_is_not(self) -> None:
        Utterance(b"\0\0" * 100, max_bytes=ByteCount(200))

        with pytest.raises(InvalidValueError, match="at most 200 bytes"):
            Utterance(b"\0\0" * 101, max_bytes=ByteCount(200))

    def test_half_a_sample_is_refused(self) -> None:
        with pytest.raises(InvalidValueError, match="whole 16-bit samples"):
            Utterance(b"\0\0\0", max_bytes=ByteCount(10))

    @pytest.mark.parametrize("bad", ["text", bytearray(b"\0\0"), memoryview(b"\0\0"), None, 5])
    def test_only_bytes_are_accepted(self, bad: object) -> None:
        with pytest.raises(InvalidValueError, match="is bytes"):
            Utterance(bad, max_bytes=ByteCount(10))  # type: ignore[arg-type]  # the point


class TestBoundedPcm:
    def test_it_keeps_what_fits_and_reports_full_exactly_once(self) -> None:
        buffer = BoundedPcm(ByteCount(10))

        assert buffer.append(b"\1\0" * 3) is False
        assert buffer.append(b"\2\0" * 3) is True  # 12 bytes offered, 10 kept
        assert buffer.append(b"\3\0" * 3) is False
        assert len(buffer) == 10
        assert buffer.snapshot() == b"\1\0" * 3 + b"\2\0" * 2

    def test_a_source_that_never_stops_cannot_grow_it(self) -> None:
        buffer = BoundedPcm(ByteCount(1000))
        reports = [buffer.append(b"\0\1" * 512) for _ in range(10_000)]

        assert len(buffer) == 1000
        assert reports.count(True) == 1

    def test_a_chunk_that_ends_mid_sample_never_leaves_half_a_sample(self) -> None:
        buffer = BoundedPcm(ByteCount(11))  # an odd cap rounds down to whole samples

        buffer.append(b"\0\1" * 20)

        assert len(buffer) == 10

    def test_odd_chunks_keep_only_whole_samples(self) -> None:
        buffer = BoundedPcm(ByteCount(100))

        buffer.append(b"\1\2\3")

        assert buffer.snapshot() == b"\1\2"

    def test_a_snapshot_is_immutable_and_later_audio_does_not_change_it(self) -> None:
        buffer = BoundedPcm(ByteCount(100))
        buffer.append(b"\1\0")
        taken = buffer.snapshot()

        buffer.append(b"\2\0")

        assert taken == b"\1\0"
        assert type(taken) is bytes

    def test_threads_appending_at_once_never_pass_the_cap(self) -> None:
        buffer = BoundedPcm(ByteCount(64 * 1024))
        fulls: list[bool] = []

        def feed() -> None:
            for _ in range(200):
                fulls.append(buffer.append(b"\0\0" * 256))

        threads = [threading.Thread(target=feed) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert len(buffer) == 64 * 1024
        assert fulls.count(True) == 1

    def test_a_zero_cap_is_full_at_the_first_chunk(self) -> None:
        buffer = BoundedPcm(ByteCount(0))

        assert buffer.append(b"\0\0") is True
        assert len(buffer) == 0
