"""Who is on the other end of a Unix socket: the same-uid rule of D4.

The kernel records the peer's credentials when the connection is made, and neither side can
forge them: `SO_PEERCRED` (a `struct ucred`) on Linux, and on macOS `LOCAL_PEERCRED` (a
`struct xucred`), which is what `getpeereid(3)` is built on. Python exposes `getpeereid` on
neither, so this module calls `getsockopt` itself. Both ends check each other: the server so
that no other user can talk to the broker, the client so that it never talks to a socket
another user put where it expected the broker's.

The check fails closed: if the credentials cannot be read, the peer is refused. Other platforms
are unsupported and refused rather than waved through.
"""

import asyncio
import os
import socket
import struct
import sys
from asyncio.trsock import TransportSocket
from typing import Final, NewType, Protocol, final

from tumbleweed.ipc.errors import PeerCredentialsError, PeerRejectedError

Uid = NewType("Uid", int)

_UCRED: Final = struct.Struct("=iII")  # Linux struct ucred: pid, uid, gid
_XUCRED_HEAD: Final = struct.Struct("=IIh")  # macOS struct xucred: version, uid, group count, ...
_XUCRED_SIZE: Final = 76  # sizeof(struct xucred): the head and 16 gids
_XUCRED_VERSION: Final = 0  # XUCRED_VERSION
_SOL_LOCAL: Final = 0  # macOS; Python does not export it


class SupportsGetsockopt(Protocol):
    """The part of a socket that reads credentials (a `socket.socket` or a transport's)."""

    def getsockopt(self, level: int, optname: int, buflen: int, /) -> bytes: ...


class PeerCredentialsProvider(Protocol):
    """Tells the uid of the process at the other end of a connected Unix socket."""

    def peer_uid(self, sock: SupportsGetsockopt, /) -> Uid:
        """The peer's effective uid at `connect`; raises `PeerCredentialsError` if unknown."""
        ...


def current_uid() -> Uid:
    """The uid this process runs as, the only one a peer may have."""
    return Uid(os.geteuid())


def parse_linux_ucred(data: bytes) -> Uid:
    """The uid in the `struct ucred` that `SO_PEERCRED` returns."""
    if len(data) != _UCRED.size:
        raise PeerCredentialsError(f"SO_PEERCRED returned {len(data)} bytes, not {_UCRED.size}")
    _pid, uid, _gid = _UCRED.unpack(data)
    return Uid(uid)


def parse_darwin_xucred(data: bytes) -> Uid:
    """The uid in the `struct xucred` that `LOCAL_PEERCRED` returns."""
    if len(data) < _XUCRED_HEAD.size:
        raise PeerCredentialsError(f"LOCAL_PEERCRED returned only {len(data)} bytes")
    version, uid, _group_count = _XUCRED_HEAD.unpack_from(data)
    if version != _XUCRED_VERSION:
        raise PeerCredentialsError(f"LOCAL_PEERCRED returned xucred version {version}")
    return Uid(uid)


@final
class SystemPeerCredentials:
    """Reads the peer's uid from the kernel: `SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS."""

    def peer_uid(self, sock: SupportsGetsockopt, /) -> Uid:
        # One chain, so that the branch for the other platform is dropped by the type checker
        # instead of being reported as unreachable.
        try:
            if sys.platform.startswith("linux"):
                return parse_linux_ucred(
                    sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
                )
            elif sys.platform == "darwin":
                return parse_darwin_xucred(
                    sock.getsockopt(_SOL_LOCAL, socket.LOCAL_PEERCRED, _XUCRED_SIZE)
                )
            else:
                raise PeerCredentialsError(f"peer credentials are not supported on {sys.platform}")
        except OSError as exc:
            raise PeerCredentialsError(
                f"cannot read the peer credentials: {exc.strerror or type(exc).__name__}"
            ) from None


def require_same_user(
    writer: asyncio.StreamWriter, provider: PeerCredentialsProvider, owner: Uid
) -> None:
    """Raise unless the process at the other end of `writer`'s connection runs as `owner`.

    Raises `PeerRejectedError` for another uid and `PeerCredentialsError` if the uid cannot be
    established. Call it before reading a single byte from the peer.
    """
    sock: object = writer.get_extra_info("socket")
    if not isinstance(sock, TransportSocket | socket.socket):
        raise PeerCredentialsError("the connection has no socket to read credentials from")
    uid = provider.peer_uid(sock)
    if uid != owner:
        raise PeerRejectedError(f"the peer runs as uid {uid}, not as uid {owner}")
