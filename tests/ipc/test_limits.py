"""The limits of a connection: their defaults are the documented ones and bad values are refused."""

import dataclasses
from datetime import timedelta

import pytest

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import ByteCount
from tumbleweed.ipc.errors import ErrorCode, RemoteError
from tumbleweed.ipc.limits import MAX_FRAME_BYTES, MIN_FRAME_BYTES, IpcLimits

SECONDS = timedelta(seconds=1)
DURATIONS = [
    "handshake_timeout",
    "frame_timeout",
    "write_timeout",
    "request_timeout",
    "handler_timeout",
]


def test_the_defaults_are_the_ones_the_architecture_document_states() -> None:
    limits = IpcLimits()

    assert limits.max_frame_bytes == MAX_FRAME_BYTES == 1024 * 1024
    assert limits.handshake_timeout == timedelta(seconds=5)
    assert limits.frame_timeout == timedelta(seconds=5)
    assert limits.write_timeout == timedelta(seconds=10)
    assert limits.request_timeout == timedelta(seconds=30)
    assert limits.handler_timeout == timedelta(seconds=30)
    assert limits.idle_timeout is None
    assert limits.max_inflight_calls == 32
    assert limits.max_inflight_handlers == 32


def test_the_ends_of_every_range_are_accepted() -> None:
    IpcLimits(max_frame_bytes=MIN_FRAME_BYTES, max_inflight_calls=1, max_inflight_handlers=1)
    IpcLimits(max_frame_bytes=MAX_FRAME_BYTES, max_inflight_calls=1024, max_inflight_handlers=1024)
    IpcLimits(idle_timeout=timedelta(microseconds=1))


@pytest.mark.parametrize(
    "size", [0, MIN_FRAME_BYTES - 1, MAX_FRAME_BYTES + 1, 2**32], ids=lambda n: str(n)
)
def test_a_frame_limit_outside_the_floor_and_the_hard_maximum_is_refused(size: int) -> None:
    with pytest.raises(InvalidValueError, match="max_frame_bytes"):
        IpcLimits(max_frame_bytes=ByteCount(size))


@pytest.mark.parametrize("size", [8192, True, 8192.0, "8192"])
def test_a_frame_limit_that_is_not_a_byte_count_is_refused(size: object) -> None:
    with pytest.raises(InvalidValueError, match="max_frame_bytes"):
        IpcLimits(max_frame_bytes=size)  # type: ignore[arg-type]  # the point


@pytest.mark.parametrize("name", [*DURATIONS, "idle_timeout"])
@pytest.mark.parametrize("bad", [timedelta(0), -SECONDS, 5, 5.0, True, "5"])
def test_a_duration_that_is_not_a_positive_timedelta_is_refused(name: str, bad: object) -> None:
    with pytest.raises(InvalidValueError, match=name):
        IpcLimits(**{name: bad})  # type: ignore[arg-type]  # the point


@pytest.mark.parametrize("name", DURATIONS)
def test_only_the_idle_timeout_may_be_none(name: str) -> None:
    with pytest.raises(InvalidValueError, match=name):
        IpcLimits(**{name: None})  # type: ignore[arg-type]  # the point


@pytest.mark.parametrize("name", ["max_inflight_calls", "max_inflight_handlers"])
@pytest.mark.parametrize("bad", [0, -1, 1025, True, 1.5, "3", None])
def test_an_in_flight_cap_that_is_not_an_int_from_1_to_1024_is_refused(
    name: str, bad: object
) -> None:
    with pytest.raises(InvalidValueError, match=name):
        IpcLimits(**{name: bad})  # type: ignore[arg-type]  # the point


def test_limits_cannot_be_changed_and_a_copy_is_validated_again() -> None:
    limits = IpcLimits()

    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.max_inflight_calls = 5  # type: ignore[misc]  # the point
    assert dataclasses.replace(limits, max_inflight_calls=5).max_inflight_calls == 5
    with pytest.raises(InvalidValueError):
        dataclasses.replace(limits, max_inflight_calls=0)


def test_a_remote_error_never_repeats_more_of_the_peers_text_than_an_excerpt() -> None:
    error = RemoteError(ErrorCode.INTERNAL, "x" * 5000)

    assert len(str(error)) < 200
    assert error.message == "x" * 5000  # the full text is kept for code, not for messages
