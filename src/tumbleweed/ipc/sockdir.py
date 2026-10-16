"""The private directory for IPC sockets, and the claim on one socket path inside it.

ARCHITECTURE section 9: sockets live in `$XDG_RUNTIME_DIR/tumbleweed/`, mode 0700.

Where the base is. `$XDG_RUNTIME_DIR` when it is set (systemd-logind makes `/run/user/<uid>`,
0700). macOS has no such variable, so there the base is the per-user temporary directory the
system provides, `confstr(_CS_DARWIN_USER_TEMP_DIR)` (`/var/folders/.../T/`, owned by the user,
0700, cleared at reboot, and the directory `$TMPDIR` normally names). Anywhere else an unset
`XDG_RUNTIME_DIR` is an error: there is no place we know to be private.

What is refused, never repaired. The base must be a directory of ours with no group or other
permission bit set at all (0740 is refused). The `tumbleweed` directory in it is created 0700 if
missing; if it exists it must be a real directory (not a symlink, even to a good one) owned by
us with mode exactly 0700. On macOS neither may have an extended ACL, which can let others in
whatever the mode says. Otherwise `SocketError` says what is wrong and nothing is touched. Both
are opened with
`O_NOFOLLOW` and judged by `fstat` on the opened descriptor, so there is no gap between a `stat`
and an `open` for anyone to slip a symlink into. (The socket itself is later bound and connected
to by path. Nobody but the user can change a private directory of the user's; a process of the
same user can, and is out of scope: ARCHITECTURE section 8.)

One server per socket. A server takes an exclusive `flock` on `<name>.sock.lock` for as long as
it runs. The kernel drops the lock when the holder dies, however it dies, so holding it proves
that any socket file left at the path is stale: it is then removed, but only if it is a socket
we own. A regular file, a directory, a symlink or somebody else's socket is never removed, and
a second server fails with `AddressInUseError` without touching the first one's socket. The
lock file itself stays: removing it would let two servers each lock a different file.

Staying put. macOS's `dirhelper` empties `/var/folders/.../T` of files not accessed for three
days, and neither a socket nor a lock file is accessed while a server idles, so a long-running
server would lose both, and with them its claim: a second server could then start on the same
path. `SocketClaim.touch`, which `IpcServer` calls every six hours by default (by the wall
clock, which keeps running while a Mac sleeps), refreshes the timestamps of the socket, the lock
file and the directory, without following a link. That keeps a running server's files young; it
cannot bring back files a purge took anyway, and then `touch` returns `False` and the server
closes itself, so that whatever supervises it starts a new one: two servers must never share a
store.
"""

import contextlib
import ctypes
import errno
import fcntl
import os
import re
import socket
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Self, final

from tumbleweed.core.errors import excerpt
from tumbleweed.core.types import CanonicalPath
from tumbleweed.ipc.errors import AddressInUseError, SocketError
from tumbleweed.ipc.peercred import Uid, current_uid

DIRECTORY_NAME: Final = "tumbleweed"
# At most 24 characters, so that every name fits next to macOS's longest per-user temporary
# directory (56 bytes once canonical, `/private/var/folders/xx/<30>/T`) within the 103 below.
_NAME: Final = re.compile(r"[a-z][a-z0-9-]{0,23}")
_CS_DARWIN_USER_TEMP_DIR: Final = 65537  # <unistd.h>; Python has no name for it in confstr_names
# sun_path holds 104 bytes on macOS and 108 on Linux, with the terminating NUL.
_MAX_SOCKET_PATH_BYTES: Final = 103
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

_ACL_TYPE_EXTENDED: Final = 0x100  # <sys/acl.h> on macOS
_MINT: Final = object()  # what only this module holds, so that only it can make a SocketPath


@final
class SocketPath:
    """The path of one Unix socket, made only by `SocketDirectory.socket_path`.

    It is inside a verified private directory, has a validated name and is short enough for a
    socket address, so code that is given one need not check any of that again. There is no
    other way to get one: the constructor refuses. It is a path-like object (`os.fspath` and
    `str` work); `path` is the `pathlib.Path`.
    """

    __slots__ = ("_path",)

    def __init__(self, mint: object, path: Path) -> None:
        if mint is not _MINT:
            raise TypeError("a SocketPath comes from SocketDirectory.socket_path()")
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    @property
    def lock_path(self) -> Path:
        """The lock file of the server on this path."""
        return Path(f"{self._path}.lock")

    def __fspath__(self) -> str:
        return os.fspath(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def __repr__(self) -> str:
        return f"SocketPath({str(self._path)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SocketPath) and self._path == other._path

    def __hash__(self) -> int:
        return hash(self._path)


def _darwin_user_temp_dir() -> str:
    value = os.confstr(_CS_DARWIN_USER_TEMP_DIR)
    if not value:
        raise SocketError("macOS did not name a per-user temporary directory")
    return value


def runtime_base(
    env: Mapping[str, str],
    *,
    platform: str = sys.platform,
    darwin_user_temp_dir: Callable[[], str] = _darwin_user_temp_dir,
) -> str:
    """The directory the `tumbleweed` socket directory goes in, as text (see the module doc)."""
    configured = env.get("XDG_RUNTIME_DIR", "")
    if configured:
        return configured
    if platform == "darwin":
        return darwin_user_temp_dir()
    raise SocketError(
        "XDG_RUNTIME_DIR is not set: log in through a session that provides it "
        "(systemd-logind), or set it to a directory of yours that nobody else can enter"
    )


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def _mode(info: os.stat_result) -> str:
    return f"{stat.S_IMODE(info.st_mode):04o}"


def _require_no_acl(fd: int, what: str) -> None:
    """On macOS, refuse a directory with an extended ACL, which can let others in whatever its mode.

    A mode of 0700 says nothing about an ACL entry such as `everyone allow search`, and macOS
    keeps those apart from the mode bits. (Linux folds the permissions of a POSIX ACL into the
    group bits of the mode, which are already required to be clear.)
    """
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.acl_get_fd_np.restype = ctypes.c_void_p
        libc.acl_free.argtypes = [ctypes.c_void_p]
        ctypes.set_errno(0)
        acl: int | None = libc.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
        if acl is not None:
            libc.acl_free(acl)
            raise SocketError(f"{what} has an access control list: refusing to use it")
        if ctypes.get_errno() not in {0, errno.ENOENT}:  # ENOENT: there is none
            raise SocketError(f"cannot check {what} for an access control list")


def _open_directory(name: str, *, dir_fd: int | None, what: str) -> int:
    """Open a directory without following a symlink in its last component."""
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        try:
            found = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            raise SocketError(f"cannot open {what}: {_reason(exc)}") from None
        if stat.S_ISLNK(found.st_mode):
            raise SocketError(f"{what} is a symlink; refusing to follow it") from None
        if not stat.S_ISDIR(found.st_mode):
            raise SocketError(f"{what} is not a directory") from None
        raise SocketError(f"cannot open {what}: {_reason(exc)}") from None


def _verify_leaf(base_fd: int, path: Path, *, create: bool, owner: Uid) -> None:
    """Create (if asked) and check the `tumbleweed` directory inside the open `base_fd`."""
    created = False
    if create:
        # The umask may only remove bits; with 077 in place the mode below is exact.
        previous = os.umask(0o077)
        try:
            os.mkdir(DIRECTORY_NAME, 0o700, dir_fd=base_fd)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise SocketError(f"cannot create {path}: {_reason(exc)}") from None
        finally:
            os.umask(previous)
    fd = _open_directory(DIRECTORY_NAME, dir_fd=base_fd, what=f"the socket directory {path}")
    try:
        if created:
            os.fchmod(fd, 0o700)  # a setgid parent would have passed its bit on
        info = os.fstat(fd)
        _require_no_acl(fd, f"the socket directory {path}")
        if info.st_uid != owner:
            raise SocketError(f"the socket directory {path} is owned by uid {info.st_uid}")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise SocketError(f"the socket directory {path} has mode {_mode(info)}, not 0700")
    finally:
        os.close(fd)


@final
class SocketDirectory:
    """A verified private directory for sockets: it exists only if every check passed.

    `SocketDirectory(base, create=True)` makes `base/tumbleweed` (mode 0700) if it is missing,
    for a server. `create=False` only checks it, for a client. Both raise `SocketError` for a
    base or a directory that is not ours and private (see the module doc). `owner` is the uid
    that must own them: this process's uid, unless a test says otherwise.
    """

    __slots__ = ("_path",)

    def __init__(self, base: CanonicalPath, *, create: bool, owner: Uid | None = None) -> None:
        uid = current_uid() if owner is None else owner
        base_fd = _open_directory(str(base), dir_fd=None, what=f"the runtime directory {base}")
        try:
            info = os.fstat(base_fd)
            _require_no_acl(base_fd, f"the runtime directory {base}")
            if info.st_uid != uid:
                raise SocketError(f"the runtime directory {base} is owned by uid {info.st_uid}")
            if info.st_mode & 0o077:
                raise SocketError(
                    f"the runtime directory {base} has mode {_mode(info)}: group and others "
                    "must not be able to enter it"
                )
            path = Path(str(base)) / DIRECTORY_NAME
            _verify_leaf(base_fd, path, create=create, owner=uid)
        finally:
            os.close(base_fd)
        self._path = path

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str], *, create: bool, owner: Uid | None = None
    ) -> Self:
        """The directory under `$XDG_RUNTIME_DIR`, or macOS's per-user temporary directory."""
        text = runtime_base(env)
        try:
            base = CanonicalPath.resolve(text)
        except ValueError as exc:
            raise SocketError(f"unusable runtime directory {excerpt(text)}: {exc}") from None
        return cls(base, create=create, owner=owner)

    @property
    def path(self) -> Path:
        return self._path

    def socket_path(self, name: str) -> SocketPath:
        """The path of the socket called `name` in this directory (`<name>.sock`)."""
        if _NAME.fullmatch(name) is None:
            raise SocketError(
                f"invalid socket name {excerpt(name)}: use 1 to 24 characters from a-z, 0-9 "
                "and '-', starting with a letter"
            )
        path = self._path / f"{name}.sock"
        if len(os.fsencode(path)) > _MAX_SOCKET_PATH_BYTES:
            raise SocketError(
                f"the socket path {path} is over {_MAX_SOCKET_PATH_BYTES} bytes, more than a "
                "Unix socket address can hold; use a shorter XDG_RUNTIME_DIR"
            )
        return SocketPath(_MINT, path)


@final
class SocketClaim:
    """The exclusive right to serve on one socket path: its lock, and the bound socket.

    Made by `claim_socket`. The socket is bound but not listening yet; pass it to the event
    loop. `release` closes it, removes the socket file if it is still the one we bound, and
    drops the lock. It is safe to call twice.
    """

    def __init__(
        self, path: SocketPath, sock: socket.socket, lock_fd: int, identity: tuple[int, int]
    ) -> None:
        self._path = path
        self._sock = sock
        self._lock_fd = lock_fd
        self._identity = identity
        self._released = False

    @property
    def sock(self) -> socket.socket:
        return self._sock

    @property
    def path(self) -> SocketPath:
        return self._path

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._sock.close()
        try:
            info = self._path.path.lstat()
            if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self._identity:
                self._path.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            os.close(self._lock_fd)

    def touch(self) -> bool:
        """Refresh the timestamps of the socket, its lock file and its directory.

        Cleaners of old files in temporary directories go by age: macOS's `dirhelper` removes
        what in `/var/folders/.../T` was not accessed for three days, and a server that runs for
        weeks touches neither its socket nor its lock file. A server calls this periodically.

        Nothing here follows a symlink. The lock file is touched through the descriptor that
        holds the lock, after checking that the path still names that very file; the socket and
        the directory are touched without following a link in their last component.

        Returns `False` if the socket file or the lock file is no longer the one this claim
        holds (removed or replaced): clients cannot reach this server any more, or another
        server could claim the path, so the server must stop. A purge that already happened
        cannot be undone.
        """
        try:
            socket_info = self._path.path.lstat()
            if not stat.S_ISSOCK(socket_info.st_mode):
                return False
            if (socket_info.st_dev, socket_info.st_ino) != self._identity:
                return False
            held = os.fstat(self._lock_fd)
            named = self._path.lock_path.lstat()
            if (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
                return False
            os.utime(self._lock_fd)
            os.utime(self._path.path, follow_symlinks=False)
            os.utime(self._path.path.parent, follow_symlinks=False)
        except OSError:
            return False
        return True


def _lock(path: SocketPath, owner: Uid) -> int:
    lock_path = path.lock_path
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise SocketError(f"cannot open the lock file {lock_path}: {_reason(exc)}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
            raise SocketError(f"the lock file {lock_path} is not a regular file of ours")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AddressInUseError(f"another process is already serving {path}") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _remove_stale(path: SocketPath, owner: Uid) -> None:
    """Remove a socket file left by a dead server. Refuse, untouched, anything else."""
    try:
        info = path.path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SocketError(f"cannot inspect {path}: {_reason(exc)}") from None
    if not stat.S_ISSOCK(info.st_mode):
        raise SocketError(
            f"{path} exists and is not a socket (mode {stat.filemode(info.st_mode)}); "
            "refusing to remove it"
        )
    if info.st_uid != owner:
        raise SocketError(f"{path} is a socket of uid {info.st_uid}; refusing to remove it")
    path.path.unlink()


def claim_socket(path: SocketPath, *, owner: Uid | None = None) -> SocketClaim:
    """Take the lock for `path`, clear a stale socket file there, and bind a socket to it.

    Raises `AddressInUseError` if another server holds the lock, and `SocketError` if the path
    holds something that is not a stale socket of ours.
    """
    uid = current_uid() if owner is None else owner
    lock_fd = _lock(path, uid)
    try:
        _remove_stale(path, uid)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(os.fspath(path))
        except OSError as exc:
            sock.close()
            raise SocketError(f"cannot bind {path}: {_reason(exc)}") from None
        try:
            path.path.chmod(0o600)
            info = path.path.lstat()
        except OSError as exc:
            sock.close()
            with contextlib.suppress(OSError):
                path.path.unlink()
            raise SocketError(f"cannot secure {path}: {_reason(exc)}") from None
    except BaseException:
        os.close(lock_fd)
        raise
    return SocketClaim(path, sock, lock_fd, (info.st_dev, info.st_ino))
