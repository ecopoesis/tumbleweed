"""A socket path that no `SocketDirectory` vouches for, for tests of what surrounds one."""

from pathlib import Path

from tumbleweed.ipc.sockdir import _MINT, SocketPath


def unchecked_socket_path(path: Path) -> SocketPath:
    """A `SocketPath` for `path`, which is not in a verified directory and may not fit a socket.

    Production code cannot make one of these (`SocketPath` refuses a constructor call from
    outside `sockdir`). The tests of the claim and of the peer check need sockets in places a
    private directory would not be: one that another user can write in, or one that does not
    exist.
    """
    return SocketPath(_MINT, path)
