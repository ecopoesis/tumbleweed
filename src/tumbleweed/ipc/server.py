"""The IPC server: listens on a claimed socket path and hands out handshaken connections."""

import asyncio
import logging
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


@final
class IpcServer:
    """Serves one socket path: `await server.start()`, `await server.close()`, or `async with`.

    Every connection is checked before a byte is read from it: the peer must run as the same
    user as this process, otherwise it is closed and logged. Then it must complete
    the handshake within the limits' handshake timeout. At most `max_connections` connections
    exist at once, handshakes included; further ones are closed at once. Every request on every
    connection goes to `handler`, which is given the connection the request came from.
    """

    def __init__(
        self,
        path: SocketPath,
        handler: RequestHandler,
        *,
        limits: IpcLimits | None = None,
        max_connections: int = 16,
        credentials: PeerCredentialsProvider | None = None,
    ) -> None:
        if type(max_connections) is not int or not 1 <= max_connections <= _MAX_CONNECTIONS_LIMIT:
            raise InvalidValueError(
                f"max_connections must be an int from 1 to {_MAX_CONNECTIONS_LIMIT}"
            )
        self._path = path
        self._handler = handler
        self._limits = IpcLimits() if limits is None else limits
        self._max_connections = max_connections
        self._credentials = SystemPeerCredentials() if credentials is None else credentials
        self._owner = current_uid()
        self._claim: SocketClaim | None = None
        self._server: asyncio.Server | None = None
        self._closing = False
        self._handshaking: dict[asyncio.Task[None], asyncio.StreamWriter] = {}
        self._connections: set[Connection] = set()

    @property
    def connections(self) -> frozenset[Connection]:
        """The connections that are open now."""
        return frozenset(c for c in self._connections if not c.is_closed)

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

    async def close(self) -> None:
        """Stop accepting, close every connection and remove the socket file. Idempotent."""
        claim, server = self._claim, self._server
        self._closing = True
        if claim is None or server is None:
            return
        self._claim = self._server = None
        server.close()
        # A peer in its handshake is dropped by closing its transport, which ends the handshake
        # by itself. (Cancelling the task is no good: before Python 3.12.2 asyncio logs an error
        # for a cancelled connection callback.)
        handshaking = dict(self._handshaking)
        for writer in handshaking.values():
            writer.transport.abort()
        if handshaking:
            await asyncio.wait(handshaking, timeout=_CLEANUP.total_seconds())
        await asyncio.gather(*(connection.close() for connection in list(self._connections)))
        try:
            async with asyncio.timeout(_CLEANUP.total_seconds()):
                await server.wait_closed()
        except TimeoutError:
            _logger.warning("the IPC server's listener did not close in time")
        claim.release()

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

    def _is_closing(self) -> bool:
        return self._closing

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None or self._is_closing():
            await close_writer(writer)
            return
        self._handshaking[task] = writer
        admitted = False
        try:
            self._connections = {c for c in self._connections if not c.is_closed}
            if len(self._connections) + len(self._handshaking) > self._max_connections:
                _logger.warning("refused an IPC connection: %d are open", self._max_connections)
                return
            require_same_user(writer, self._credentials, self._owner)
            await server_handshake(reader, writer, limits=self._limits)
            self._connections.add(
                Connection(reader, writer, limits=self._limits, handler=self._handler)
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
                await close_writer(writer)
