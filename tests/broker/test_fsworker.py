"""The file system worker abandons what hangs, and cleans up after it."""

import asyncio
import threading
from datetime import timedelta

import pytest

from tests.ipc.bounded import run
from tumbleweed.broker.errors import FsTimeoutError
from tumbleweed.broker.fsworker import FsWorker

SHORT = timedelta(milliseconds=200)


def test_it_returns_what_the_work_returns() -> None:
    async def scenario() -> int:
        worker = FsWorker(timeout=timedelta(seconds=5))
        try:
            return await worker.call(lambda: 6 * 7)
        finally:
            worker.close()

    assert run(scenario) == 42


def test_what_the_work_raises_is_raised() -> None:
    def boom() -> int:
        raise ValueError("x")

    async def scenario() -> None:
        worker = FsWorker(timeout=timedelta(seconds=5))
        try:
            await worker.call(boom)
        finally:
            worker.close()

    with pytest.raises(ValueError, match="x"):
        run(scenario)


def test_a_call_that_hangs_times_out_and_the_loop_stays_free() -> None:
    release = threading.Event()

    async def scenario() -> float:
        worker = FsWorker(timeout=SHORT)
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            with pytest.raises(FsTimeoutError):
                await worker.call(lambda: release.wait(30))
            return loop.time() - started
        finally:
            release.set()
            worker.close()

    assert run(scenario) < 3.0


def test_a_result_that_arrives_after_the_timeout_is_discarded_through_the_hook() -> None:
    release = threading.Event()
    discarded: list[str] = []
    done = threading.Event()

    def slow() -> str:
        release.wait(30)
        return "late"

    def discard(value: str) -> None:
        discarded.append(value)
        done.set()

    async def scenario() -> None:
        worker = FsWorker(timeout=SHORT)
        try:
            with pytest.raises(FsTimeoutError):
                await worker.call(slow, discard=discard)
            release.set()
            assert await asyncio.to_thread(done.wait, 10)
        finally:
            release.set()
            worker.close()

    run(scenario)

    assert discarded == ["late"]


def test_work_that_never_started_is_cancelled_and_never_runs() -> None:
    """All threads stuck: the next call queues, times out, and its work must not run later."""
    release = threading.Event()
    ran: list[str] = []

    async def scenario() -> None:
        worker = FsWorker(timeout=SHORT, threads=1)
        try:
            with pytest.raises(FsTimeoutError):
                await worker.call(lambda: release.wait(30))  # holds the only thread
            with pytest.raises(FsTimeoutError):
                await worker.call(lambda: ran.append("queued"))
            release.set()
            await asyncio.to_thread(worker.close)
        finally:
            release.set()

    run(scenario)

    assert ran == []


def test_a_caller_that_is_cancelled_abandons_the_work_the_same_way() -> None:
    release = threading.Event()
    discarded = threading.Event()

    async def scenario() -> None:
        worker = FsWorker(timeout=timedelta(seconds=30))
        try:
            task = asyncio.ensure_future(
                worker.call(lambda: release.wait(30) and "x", discard=lambda _: discarded.set())
            )
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            assert await asyncio.to_thread(discarded.wait, 10)
        finally:
            release.set()
            worker.close()

    run(scenario)
