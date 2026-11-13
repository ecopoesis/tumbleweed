"""The shell's one connection to the broker, on a loop of its own (S19, D26 (8), #89).

`BrokerClient` is built on the asyncio loop it will run on (its heartbeat is a task there, and so
are the connection's reader and the handler that shows the broker's prompts), and the shell has no
such loop to spare: the agent's worker thread has its own, the GTK main thread has none, and the
image decoder's workers have neither. So `BrokerLink` owns a daemon thread with a loop, the
`BrokerConnector` and the one `BrokerClient` that every request of the shell goes through, and other
threads reach them with:

- `call`, awaited from the agent's loop (or any loop), and `call_blocking`, from a thread that may
  wait (the decode worker, #109), each with a deadline of its own. Neither waits past it: a call
  that ran out is cancelled, and the caller gets `BrokerUnavailableError`.
- `submit`, which starts work and returns at once (the main thread ending a task).

**It connects on first use**, so a shell whose responder never asks for the broker (echo, no key)
never starts one. The connector starts `tumbleweed-broker` on demand in development and backs off
while it comes up; after a failed attempt the link refuses calls for a pause that doubles from
`RETRY_FLOOR` to `RETRY_CEILING` seconds, so a broker that is not there does not cost every tool
call the connector's whole patience. A connection that ends is noticed by a watcher, which clears
the client and tells the listeners (the task session: the broker ended the task with the
connection); the next call connects again.

The connection's request handler (`BrokerPrompts`, which shows the broker's prompts) is attached
by the window after it exists; a request that arrives before that is refused `busy`, which the
broker treats as a denial.

This module imports no GTK. Everything that touches the connection runs on the link's loop.
"""

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Final, final

from tumbleweed.broker.client import (
    BrokerClient,
    BrokerConnector,
    BrokerRefused,
    spawn_broker_process,
    terminate,
)
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.ipc.broker_messages import Refused
from tumbleweed.ipc.connection import Connection, RequestHandler
from tumbleweed.ipc.errors import IpcError
from tumbleweed.ipc.messages import RequestBody, ResponseBody
from tumbleweed.ipc.sockdir import SocketDirectory

logger = logging.getLogger(__name__)

RETRY_FLOOR: Final = 1.0
RETRY_CEILING: Final = 30.0
SPAWN_PATIENCE: Final = 60.0
CONNECT_SECONDS: Final = 8.0
"""The most one attempt to connect may take: below the deadline of every call, so that a call
waiting for a broker that is not there fails by this and starts the pause, not by its own."""
_START_TIMEOUT: Final = 5.0
_STOP_TIMEOUT: Final = 3.0
_MARGIN: Final = 2.0  # seconds a blocking caller waits beyond the deadline of the work itself
_DEV_FLAGS: Final = {
    "no-landlock": "--no-landlock",
    "allow-unenforced-limits": "--allow-unenforced-limits",
}

type ClientWork[R] = Callable[[BrokerClient], Awaitable[R]]
type ConnectorFactory = Callable[[RequestHandler], BrokerConnector]


class BrokerUnavailableError(TumbleweedError):
    """The broker could not be reached, or the connection was lost or did not answer in time.

    The message is fixed text, safe to give to the model."""


class BrokerNotReachedError(BrokerUnavailableError):
    """The broker could not be connected to: nothing was sent, so nothing can have happened."""


@final
class _Relay:
    """The connection's request handler: whatever the window attached, or a refusal."""

    def __init__(self) -> None:
        self.target: RequestHandler | None = None

    async def __call__(self, request: RequestBody, connection: Connection, /) -> ResponseBody:
        target = self.target
        if target is None:
            return Refused(kind="refused", reason="busy", message="the shell cannot ask yet")
        return await target(request, connection)


@final
class BrokerLink:
    """See the module docstring."""

    def __init__(
        self,
        connector: ConnectorFactory,
        *,
        retry_floor: float = RETRY_FLOOR,
        retry_ceiling: float = RETRY_CEILING,
        connect_seconds: float = CONNECT_SECONDS,
    ) -> None:
        self._connect_seconds = connect_seconds
        self._make_connector = connector
        self._retry_floor = retry_floor
        self._retry_ceiling = retry_ceiling
        self._relay = _Relay()
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._closed = False
        self._lost: list[Callable[[], None]] = []
        # Used only on the link's loop.
        self._connector: BrokerConnector | None = None
        self._client: BrokerClient | None = None
        self._connecting: asyncio.Lock | None = None  # made on the loop, in _main
        self._failures = 0
        self._retry_at = 0.0
        self._watchers: set[asyncio.Task[None]] = set()

    # --- wiring (any thread) ---------------------------------------------------------------

    def attach_handler(self, handler: RequestHandler) -> None:
        """Answer the broker's requests (its prompts) with `handler` from now on."""
        self._relay.target = handler

    def on_lost(self, listener: Callable[[], None]) -> None:
        """Call `listener`, on the link's thread, each time an established connection ends."""
        self._lost.append(listener)

    # --- calls (any thread but the link's) -------------------------------------------------

    async def call[R](self, work: ClientWork[R], *, deadline: float) -> R:
        """Run `work` with the connected client, connecting if need be; at most `deadline`
        seconds. Raises `BrokerRefused` if the broker answered `Refused`, and
        `BrokerUnavailableError` for anything that kept it from answering."""
        future = self._start(work, deadline)
        return await asyncio.wrap_future(future)

    def call_blocking[R](self, work: ClientWork[R], *, deadline: float) -> R:
        """`call` for a thread that may block (never the GTK main thread, never the link's)."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("a blocking call from the broker link's own thread would deadlock")
        future = self._start(work, deadline)
        try:
            return future.result(deadline + _MARGIN)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise BrokerUnavailableError("The broker did not answer in time.") from None

    def submit[R](self, work: ClientWork[R], *, deadline: float) -> "concurrent.futures.Future[R]":
        """Start `work` and return at once; failures are in the future."""
        return self._start(work, deadline)

    def _start[R](self, work: ClientWork[R], deadline: float) -> "concurrent.futures.Future[R]":
        loop = self._ensure_started()
        return asyncio.run_coroutine_threadsafe(self._run(work, deadline), loop)

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._guard:
            if self._closed:
                raise BrokerUnavailableError("The connection to the broker is closed.")
            if self._thread is None:
                thread = threading.Thread(
                    target=self._main, name="tumbleweed-broker-link", daemon=True
                )
                try:
                    thread.start()
                except RuntimeError:  # the process cannot make another thread
                    raise BrokerUnavailableError("The broker link could not start.") from None
                self._thread = thread
            thread = self._thread
        if not self._ready.wait(_START_TIMEOUT) or self._loop is None:
            raise BrokerUnavailableError("The broker link could not start.")
        return self._loop

    def _main(self) -> None:
        async def serve() -> None:
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            self._connecting = asyncio.Lock()
            self._ready.set()
            await self._stop.wait()
            await self._shutdown()

        try:
            asyncio.run(serve())
        finally:
            self._ready.set()

    # --- on the link's loop ----------------------------------------------------------------

    async def _run[R](self, work: ClientWork[R], deadline: float) -> R:
        try:
            async with asyncio.timeout(deadline):
                client = await self._connected()
                return await work(client)
        except BrokerRefused:
            raise
        except TimeoutError:
            raise BrokerUnavailableError("The broker did not answer in time.") from None
        except IpcError:
            raise BrokerUnavailableError(
                "The connection to the broker was lost or the broker did not answer."
            ) from None

    async def _connected(self) -> BrokerClient:
        lock = self._connecting
        if lock is None:  # made with the loop, before any call can reach it
            raise BrokerUnavailableError("The broker link is not running.")
        async with lock:
            client = self._client
            if client is not None and not client.is_closed:
                return client
            loop = asyncio.get_running_loop()
            if loop.time() < self._retry_at:
                raise BrokerNotReachedError("The broker is not reachable right now.")
            if self._connector is None:
                try:
                    self._connector = self._make_connector(self._relay)
                except (TumbleweedError, OSError):
                    self._fail(loop)
                    raise BrokerNotReachedError("The broker's socket cannot be used.") from None
            try:
                async with asyncio.timeout(self._connect_seconds):
                    client = await self._connector.connect()
            except (IpcError, OSError, TimeoutError):
                self._fail(loop)
                raise BrokerNotReachedError("The broker is not reachable right now.") from None
            except asyncio.CancelledError:
                self._fail(loop)  # the caller's own deadline ended the attempt: still a failure
                raise
            self._failures = 0
            self._client = client
            watcher = loop.create_task(self._watch(client), name="broker-link-watch")
            self._watchers.add(watcher)
            watcher.add_done_callback(self._watchers.discard)
            return client

    def _fail(self, loop: asyncio.AbstractEventLoop) -> None:
        self._failures += 1
        pause = min(self._retry_floor * 2 ** (self._failures - 1), self._retry_ceiling)
        self._retry_at = loop.time() + pause

    async def _watch(self, client: BrokerClient) -> None:
        await client.connection.wait_closed()
        if self._client is client:
            self._client = None
        with contextlib.suppress(Exception):
            await client.close()  # stops the heartbeat
        if self._stop is not None and self._stop.is_set():
            return
        for listener in tuple(self._lost):
            try:
                listener()
            except Exception:
                logger.exception("a listener of the broker connection's end failed")

    async def _shutdown(self) -> None:
        client, self._client = self._client, None
        for watcher in tuple(self._watchers):
            watcher.cancel()
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    # --- closing (any thread) --------------------------------------------------------------

    def close(self) -> None:
        """Close the connection and stop the thread. Idempotent."""
        with self._guard:
            if self._closed:
                return
            self._closed = True
            thread, loop, stop = self._thread, self._loop, self._stop
        if thread is None or loop is None or stop is None:
            return
        with contextlib.suppress(RuntimeError):  # the loop is gone already
            loop.call_soon_threadsafe(stop.set)
        thread.join(_STOP_TIMEOUT)
        if thread.is_alive():
            logger.warning("the broker link's thread did not stop in time")


@final
class _Spawner:
    """Starts the broker for the connector: one process at a time (D26 (8)).

    A broker that was started and is still coming up is waited for, not started again by the next
    call. One that has run for `SPAWN_PATIENCE` seconds and still cannot be connected to never
    bound its socket: it is stopped and replaced. Only the newest process is kept. The process is
    recorded when it exists, even if the call that started it was cancelled meanwhile, so a second
    one is not started on top of it.
    """

    def __init__(
        self,
        environ: Mapping[str, str],
        flags: tuple[str, ...],
        *,
        patience: float = SPAWN_PATIENCE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._environ = environ
        self._flags = flags
        self._patience = patience
        self._clock = clock
        self._process: asyncio.subprocess.Process | None = None
        self._since = 0.0

    async def __call__(self) -> object:
        process = self._process
        if process is not None and process.returncode is None:
            if self._clock() - self._since < self._patience:
                return process
            await terminate(process, wait=timedelta(seconds=_STOP_TIMEOUT))
        starting = asyncio.ensure_future(
            spawn_broker_process(self._environ, extra_args=self._flags)
        )
        starting.add_done_callback(self._record)
        return await asyncio.shield(starting)

    def _record(self, starting: "asyncio.Future[asyncio.subprocess.Process]") -> None:
        if not starting.cancelled() and starting.exception() is None:
            self._process, self._since = starting.result(), self._clock()


def connector_from_environment(environ: Mapping[str, str]) -> ConnectorFactory:
    """The connector a shell uses: the broker's socket under the user's runtime directory, and in
    development (`TUMBLEWEED_BROKER_SPAWN` not `0`) a broker started on demand.

    `TUMBLEWEED_BROKER_DEV_FLAGS` is a comma-separated list of `no-landlock` and
    `allow-unenforced-limits`, the two development settings of the broker, for a host without
    Landlock or a systemd scope. Anything else in it is ignored. The environment is read once,
    here: a connector is made lazily, on the link's thread, from this copy.
    """
    spawn_enabled = environ.get("TUMBLEWEED_BROKER_SPAWN", "1") != "0"
    flags = tuple(
        _DEV_FLAGS[name]
        for name in (
            part.strip() for part in environ.get("TUMBLEWEED_BROKER_DEV_FLAGS", "").split(",")
        )
        if name in _DEV_FLAGS
    )
    spawner = _Spawner(dict(environ), flags)
    snapshot = dict(environ)

    def make(handler: RequestHandler) -> BrokerConnector:
        from tumbleweed.broker.daemon import SOCKET_NAME  # lazily: the daemon's module is large

        path = SocketDirectory.from_environment(snapshot, create=True).socket_path(SOCKET_NAME)

        return BrokerConnector(path, spawn=spawner if spawn_enabled else None, handler=handler)

    return make
