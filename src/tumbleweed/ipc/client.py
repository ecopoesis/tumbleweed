"""Connecting to an IPC server: socket, same-uid check, handshake."""

import asyncio
import os

from tumbleweed.ipc.connection import Connection, RequestHandler
from tumbleweed.ipc.errors import IpcConnectError
from tumbleweed.ipc.framing import close_writer
from tumbleweed.ipc.handshake import client_handshake
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.peercred import (
    PeerCredentialsProvider,
    SystemPeerCredentials,
    current_uid,
    require_same_user,
)
from tumbleweed.ipc.sockdir import SocketPath


async def connect(
    path: SocketPath,
    handler: RequestHandler | None = None,
    *,
    limits: IpcLimits | None = None,
    credentials: PeerCredentialsProvider | None = None,
    answer_diagnostics: bool = True,
) -> Connection:
    """Connect to the server at `path` and return the handshaken connection.

    The server must run as the same user as this process: the check is made before a byte is
    sent, so a socket another user put at `path` never sees anything. `handler` answers the
    server's requests (never `ping` or `echo`: the connection answers those itself unless
    `answer_diagnostics` is `False`, which is for tests); without a handler every other request
    from the server is refused with `unsupported_request`. The connect and the handshake each get
    `limits.handshake_timeout`.

    Raises `IpcConnectError` if there is nobody to connect to, `PeerAuthenticationError` if the
    server is another user's, and `HandshakeError` (`IncompatibleProtocolError` for a version
    mismatch) if the handshake fails.
    """
    limits = IpcLimits() if limits is None else limits
    provider = SystemPeerCredentials() if credentials is None else credentials
    uid = current_uid()
    try:
        async with asyncio.timeout(limits.handshake_timeout.total_seconds()):
            reader, writer = await asyncio.open_unix_connection(os.fspath(path))
    except TimeoutError:
        raise IpcConnectError(f"connecting to {path} timed out") from None
    except OSError as exc:
        raise IpcConnectError(f"cannot connect to {path}: {exc.strerror}") from None
    try:
        require_same_user(writer, provider, uid)
        await client_handshake(reader, writer, limits=limits)
    except BaseException:
        await close_writer(writer)
        raise
    return Connection(
        reader, writer, limits=limits, handler=handler, answer_diagnostics=answer_diagnostics
    )
