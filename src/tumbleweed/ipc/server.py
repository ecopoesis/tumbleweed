"""The IPC server: listens on a claimed socket path and hands out handshaken connections."""

import asyncio
import contextlib
import logging
import time
from datetime import timedelta
from types import TracebackType
from typing import Self, final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.ipc.connection import Connection, RequestHandler
from tumbleweed.ipc.errors import IpcError
from tumbleweed.ipc.framing import close_writer
from tumbleweed.ipc.handshake import server_handshake
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.peercred import (
    PeerCredentialsProvider,
    SystemPeerCredentials,
    current_uid,
    require_same_user,
)
from tumbleweed.ipc.sockdir import SocketClaim, SocketPath, claim_socket

_logger = logging.getLogger(__name__)

_CLEANUP = timedelta(seconds=2)
_MAX_CONNECTIONS_LIMIT = 1024
DEFAULT_REFRESH_INTERVAL = timedelta(hours=6)
"""How often a server refreshes the timestamps of its socket, lock file and directory.

Well inside the three days after which macOS purges untouched files from its per-user temporary
directory (see `sockdir`).
"""
_WAKE_INTERVAL = timedelta(minutes=10)
"""How often the refresher wakes to look at the wall clock.

The event loop's clock does not run while a Mac sleeps, so a sleep of six hours on it can last
days of wall-clock time; waking often and asking `time.time()` cannot.
"""
_wall_clock = time.time


@final
class IpcServer:
    """Serves one socket path: `await server.start()`, `await server.close()`, or `async with`.

    Every connection is checked before a byte is read from it: the peer must run as the same
    user as this process, otherwise it is closed and logged. Then it must complete the
    handshake within the limits' handshake timeout. At most `max_connections` connections exist
    at once, handshakes included and closing ones counted until they are fully closed; further
    ones are closed at once. The cap is an argument of the server, not one of `IpcLimits`,
    which holds what is bounded on one connection. Every request on every connection goes to
    `handler`, which is given the connection the request came from; the diagnostics
    (`ping`, `echo`) are answered by the connection and never reach it, unless
    `answer_diagnostics` is `False` (a seam for tests, which have no other request to give a
    handler). Every `refresh_interval` of wall-clock time the server touches its socket, lock
    file and directory, so that a cleaner of old files in a temporary directory does not take
    them; if they are gone or were replaced anyway it closes itself (see `wait_closed`), so
    that whatever supervises it starts a new one: two servers must never share a store.
    """

    def __init__(
        self,
        path: SocketPath,
        handler: RequestHandler,
        *,
        limits: IpcLimits | None = None,
        max_connections: int = 16,
        credentials: PeerCredentialsProvider | None = None,
        answer_diagnostics: bool = True,
        refresh_interval: timedelta = DEFAULT_REFRESH_INTERVAL,
    ) -> None:
        if type(max_connections) is not int or not 1 <= max_connections <= _MAX_CONNECTIONS_LIMIT:
            raise InvalidValueError(
                f"max_connections must be an int from 1 to {_MAX_CONNECTIONS_LIMIT}"
            )
        if type(refresh_interval) is not timedelta or refresh_interval <= timedelta(0):
            raise InvalidValueError("refresh_interval must be a positive timedelta")
        self._path = path
        self._handler = handler
        self._limits = IpcLimits() if limits is None else limits
        self._max_connections = max_connections
        self._credentials = SystemPeerCredentials() if credentials is None else credentials
        self._diagnostics = answer_diagnostics
        self._refresh_interval = refresh_interval
        self._owner = current_uid()
        self._claim: SocketClaim | None = None
        self._server: asyncio.Server | None = None
        self._refresher: asyncio.Task[None] | None = None
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._handshaking: dict[asyncio.Task[None], asyncio.StreamWriter] = {}
        self._connections: set[Connection] = set()

    @property
    def connections(self) -> frozenset[Connection]:
        """The connections that are open now."""
        return frozenset(c for c in self._connections if not c.is_closed)

    @property
    def is_closed(self) -> bool:
        """Whether the server is closed or closing, for whatever reason."""
        return self._closing

    async def wait_closed(self) -> None:
        """Wait until the server has been closed, by `close` or by itself, and is fully down.

        A server closes itself when its socket file or lock file is gone or replaced. A
        supervisor waits here and starts a new server when this returns.
        """
        await self._closed.wait()

    async def start(self) -> None:
        """Claim the socket path and start accepting connections.

        Raises `AddressInUseError` if another server runs on the path, and `SocketError` if
        the path holds anything but a stale socket of ours.
        """
        if self._claim is not None or self._closing:
            raise RuntimeError("the server was already started, or has been closed")
        claim = claim_socket(self._path, owner=self._owner)
        try:
            self._server = await asyncio.start_unix_server(self._on_connect, sock=claim.sock)
        except BaseException:
            claim.release()
            raise
        self._claim = claim
        self._refresher = asyncio.get_running_loop().create_task(
            self._keep_fresh(claim), name="ipc-server-refresh"
        )

    async def close(self) -> None:
        """Stop accepting, close every connection and remove the socket file.

        Idempotent. Callers that arrive while a close is under way wait for that one.
        """
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.get_running_loop().create_task(
                self._shut_down(), name="ipc-server-close"
            )
        await asyncio.shield(self._close_task)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def _shut_down(self) -> None:
        claim, server, refresher = self._claim, self._server, self._refresher
        self._claim = self._server = self._refresher = None
        if claim is None or server is None:
            self._closed.set()
            return
        try:
            if refresher is not None:
                refresher.cancel()
                await asyncio.gather(refresher, return_exceptions=True)
            server.close()
            # A peer in its handshake is dropped by closing its transport, which ends the
            # handshake by itself. (Cancelling the task is no good: before Python 3.12.2
            # asyncio logs an error for a cancelled connection callback.)
            handshaking = dict(self._handshaking)
            for writer in handshaking.values():
                writer.transport.abort()
            if handshaking:
                await asyncio.wait(handshaking, timeout=_CLEANUP.total_seconds())
            results = await asyncio.gather(
                *(connection.close() for connection in list(self._connections)),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    _logger.error("closing an IPC connection failed", exc_info=result)
            try:
                async with asyncio.timeout(_CLEANUP.total_seconds()):
                    await server.wait_closed()
            except TimeoutError:
                _logger.warning("the IPC server's listener did not close in time")
        finally:
            claim.release()
            self._closed.set()

    async def _keep_fresh(self, claim: SocketClaim) -> None:
        """Touch the claim's files once the refresh interval has passed on the wall clock.

        Wakes every few minutes instead of sleeping the whole interval, because the loop's clock
        stops while a Mac sleeps. If the claim has been lost the server closes itself.
        """
        last = _wall_clock()
        while True:
            await asyncio.sleep(min(self._refresh_interval, _WAKE_INTERVAL).total_seconds())
            if _wall_clock() - last < self._refresh_interval.total_seconds():
                continue
            last = _wall_clock()
            if not claim.touch():
                _logger.error(
                    "the socket file or lock file of the IPC server is gone or was replaced, so "
                    "clients cannot reach it and another server could start: closing %s",
                    claim.path,
                )
                await self.close()  # cancels this task, which is fine: it has nothing left to do
                return

    def _is_closing(self) -> bool:
        return self._closing

    def _live_connections(self) -> int:
        """Connections not yet fully closed: a closing one holds its place until it is gone."""
        self._connections = {c for c in self._connections if not c.is_finished}
        return len(self._connections)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None or self._is_closing():
            await close_writer(writer)
            return
        self._handshaking[task] = writer
        admitted = False
        try:
            if self._live_connections() + len(self._handshaking) > self._max_connections:
                _logger.warning("refused an IPC connection: %d are open", self._max_connections)
                return
            require_same_user(writer, self._credentials, self._owner)
            await server_handshake(reader, writer, limits=self._limits)
            self._connections.add(
                Connection(
                    reader,
                    writer,
                    limits=self._limits,
                    handler=self._handler,
                    answer_diagnostics=self._diagnostics,
                )
            )
            admitted = True
        except IpcError as exc:
            _logger.log(
                logging.DEBUG if self._is_closing() else logging.WARNING,
                "refused an IPC connection: %s",
                exc,
            )
        finally:
            del self._handshaking[task]
            if not admitted:
                with contextlib.suppress(Exception):
                    await close_writer(writer)
