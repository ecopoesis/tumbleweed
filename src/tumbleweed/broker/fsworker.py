"""File system work the broker must be able to abandon.

A decision opens and walks paths (`ResolvedPath.resolve`, `DenyRules.build`, `StoredPath.open`,
reads), and a path on a hung network or FUSE mount can block a thread for as long as the mount
takes to answer, which can be never. The broker's event loop must not wait on that, and neither
may a request: `FsWorker.call` runs the work on a small pool of threads of its own (so a hung
mount cannot starve any other user of the default executor) and gives up after a deadline,
raising `FsTimeoutError`. The request fails closed; the thread is abandoned until the call
returns, if it ever does, and what it produced then is thrown away, closing a descriptor if it
opened one (`discard`).

The pool is small on purpose: if every thread is stuck on a dead mount, further work queues and
times out, which is a refusal of the requests that need the file system, and the broker goes
on answering the ones that do not. Work that has not started when its caller gives up is
cancelled and never runs.
"""

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from typing import Final, final

from tumbleweed.broker.errors import FsTimeoutError

_log = logging.getLogger(__name__)

DEFAULT_THREADS: Final = 4


def _retrieved[T](future: "asyncio.Future[T]") -> None:
    """Retrieve an exception nobody awaits any more, so that asyncio does not log it as lost."""
    if not future.cancelled():
        future.exception()


@final
class FsWorker:
    """A thread pool with a deadline per call. Use from the event loop's thread only."""

    def __init__(self, *, timeout: timedelta, threads: int = DEFAULT_THREADS) -> None:
        self._timeout = timeout
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="broker-fs")

    async def call[R](
        self,
        work: Callable[[], R],
        *,
        deadline: timedelta | None = None,
        discard: Callable[[R], None] | None = None,
    ) -> R:
        """Run `work` on a worker thread and return what it returns.

        Raises `FsTimeoutError` if it has not finished within `deadline` (the worker's default if
        not given). A call whose caller is cancelled is abandoned the same way. `discard` is
        called with the result of an abandoned call if it ever finishes, to release what it holds.
        """
        limit = (self._timeout if deadline is None else deadline).total_seconds()
        future = self._pool.submit(work)
        awaitable = asyncio.wrap_future(future)
        awaitable.add_done_callback(_retrieved)
        try:
            async with asyncio.timeout(limit):
                return await asyncio.shield(awaitable)
        except TimeoutError:
            self._abandon(future, discard)
            _log.warning("a file system operation did not finish within %g s", limit)
            raise FsTimeoutError("the file system did not answer in time") from None
        except asyncio.CancelledError:
            self._abandon(future, discard)
            raise

    @staticmethod
    def _abandon[R](future: "Future[R]", discard: Callable[[R], None] | None) -> None:
        if future.cancel():
            return  # it never started
        if discard is None:
            return

        def release(done: "Future[R]") -> None:
            if done.cancelled() or done.exception() is not None:
                return
            discard(done.result())

        future.add_done_callback(release)

    def close(self) -> None:
        """Stop taking work. Threads stuck in the kernel are not waited for."""
        self._pool.shutdown(wait=False, cancel_futures=True)
