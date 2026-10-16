"""Opening files and directories without following a symlink, and knowing what was opened.

Everything here works on descriptors. A path is a name that something else can re-point, so the
only way to use a place safely is to walk to it one component at a time, never following a
symlink, and to keep the descriptor: what the descriptor refers to cannot change. The walk is
done in one of two ways, which refuse the same things:

- `COMPONENTWISE`: one `open(name, O_NOFOLLOW | O_DIRECTORY, dir_fd=...)` per directory, with
  `O_PATH` where there is one (Linux), so that a directory on the way needs search permission
  only, as for `openat2`. Works on every host (macOS included, where there is no `O_PATH` and a
  directory on the way must also be readable: a difference of the host, not of the strategy).
- `OPENAT2` (Linux 5.6 and later, x86_64 and aarch64): one `openat2(2)` for all the directories
  with `RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_BENEATH`, so the kernel refuses any
  symlink on the way. `AUTO`, the default, uses it where the host has it. When the kernel
  refuses (a symlink, or a file where a directory should be) the same walk as above runs to say
  which, so the fault reported is the same either way.

Either way the last component is then looked at with `fstatat(AT_SYMLINK_NOFOLLOW)` before it is
opened, so that a socket, FIFO or device is refused without being opened, and opened with
`O_NOFOLLOW | O_NOCTTY` (and `O_NONBLOCK` for a file). The result is checked again with `fstat` on
the descriptor (a regular file or a directory, nothing else) because the name could have been
swapped between the two calls. What can be opened is a regular file or a directory; for a file
system that is not local (FUSE, NFS) a read can still block, and nothing here puts a timeout on
it.

`open_absolute` walks from `/`, `open_below` from an open directory (the workspace safe open
of I11 is built on it), and `identity_chain` collects the identity of each directory on a path
for the decision-time checks in `paths`.
"""

import ctypes
import errno
import functools
import os
import platform
import stat
import warnings
from collections.abc import Sequence
from enum import StrEnum
from types import TracebackType
from typing import Final, NamedTuple, Self, assert_never, final

from tumbleweed.broker.errors import PathError, PathFault
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.core.types import ByteCount

MAX_PATH_BYTES: Final = 4096  # Linux PATH_MAX
MAX_NAME_BYTES: Final = 255  # Linux NAME_MAX
MAX_COMPONENTS: Final = MAX_PATH_BYTES // 2  # "/a/a/a/...": the most a path of that size has
MAX_READ_BYTES: Final = 64 * 1024 * 1024  # the largest read a caller may ask for (I9)
_CHUNK: Final = 1024 * 1024

_COMMON: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NOCTTY
_DIRECTORY_FLAGS: Final = _COMMON | os.O_DIRECTORY
# The directories on the way to a file are only searched, never read. O_PATH (Linux) says so, and
# makes the walk need what `openat2` needs, search permission and nothing more, so that the two
# strategies refuse the same things. Where there is no O_PATH (macOS) a directory on the way must
# be readable too: a host difference, not one between the strategies.
# openat2 refuses O_PATH with any flag but these three (open(2) would ignore them).
_PARENT_FLAGS: Final = (
    os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    if hasattr(os, "O_PATH")
    else _DIRECTORY_FLAGS
)
# O_NONBLOCK so that opening a FIFO that slipped in after the check does not wait for a writer.
_FILE_FLAGS: Final = _COMMON | os.O_NONBLOCK

_SYS_OPENAT2: Final = 437  # the same number on x86_64 and aarch64
_OPENAT2_MACHINES: Final = frozenset({"x86_64", "aarch64"})
_RESOLVE_NO_MAGICLINKS: Final = 0x02
_RESOLVE_NO_SYMLINKS: Final = 0x04
_RESOLVE_BENEATH: Final = 0x08
_RESOLVE_FLAGS: Final = _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_SYMLINKS | _RESOLVE_BENEATH
_NEEDS_DIAGNOSIS: Final = frozenset({errno.ELOOP, errno.ENOTDIR})


class OpenStrategy(StrEnum):
    """How the directories on the way to a file are opened."""

    AUTO = "auto"  # OPENAT2 where the host has it, else COMPONENTWISE
    COMPONENTWISE = "componentwise"
    OPENAT2 = "openat2"  # refused with `UNSUPPORTED` where the host lacks it


class FileKind(StrEnum):
    """The only two kinds of object that can be opened."""

    DIRECTORY = "directory"
    REGULAR = "regular"


class _OpenHow(ctypes.Structure):
    """`struct open_how` of openat2(2)."""

    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


@functools.cache
def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.syscall.argtypes = [
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    return libc


def _openat2(dir_fd: int, relative: bytes, flags: int, resolve: int) -> int:
    """`openat2(dir_fd, relative, {flags, 0, resolve})`; raises `OSError` on failure."""
    how = _OpenHow(flags, 0, resolve)
    result = int(
        _libc().syscall(_SYS_OPENAT2, dir_fd, relative, ctypes.byref(how), ctypes.sizeof(how))
    )
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


@functools.cache
def openat2_available() -> bool:
    """Whether `openat2` with `RESOLVE_NO_SYMLINKS` works here (probed once, then remembered).

    False off Linux, on other CPUs than x86_64 and aarch64 (where the system call number is not
    known to be 437), on kernels before 5.6 and under a seccomp filter that refuses it.
    """
    # platform.system(), not sys.platform: mypy would treat the code below as unreachable on a Mac
    if platform.system() != "Linux" or platform.machine() not in _OPENAT2_MACHINES:
        return False
    try:
        root = os.open("/", _DIRECTORY_FLAGS)
    except OSError:
        return False
    try:
        probe = _openat2(root, b".", _DIRECTORY_FLAGS, _RESOLVE_FLAGS)
    except OSError:
        return False
    else:
        os.close(probe)
        return True
    finally:
        os.close(root)


def _chosen(strategy: OpenStrategy) -> OpenStrategy:
    """`strategy` with `AUTO` decided; refuses `OPENAT2` where the host lacks it."""
    match strategy:
        case OpenStrategy.AUTO:
            return OpenStrategy.OPENAT2 if openat2_available() else OpenStrategy.COMPONENTWISE
        case OpenStrategy.COMPONENTWISE:
            return OpenStrategy.COMPONENTWISE
        case OpenStrategy.OPENAT2:
            if not openat2_available():
                raise PathError(PathFault.UNSUPPORTED)
            return OpenStrategy.OPENAT2
        case _ as unreachable:
            assert_never(unreachable)


def _encode(names: Sequence[str]) -> list[bytes]:
    """The names as bytes, after checking them: the caller's list is never trusted.

    A name is a non-empty `str` of at most `NAME_MAX` bytes of UTF-8 (a lone surrogate, which is
    how Python holds a byte that is not UTF-8, is refused) with no `/`, no NUL and not `.` or
    `..`; there are at most `MAX_COMPONENTS` of them and, with their separators, at most
    `PATH_MAX` bytes. Encoding is explicit, so that it does not depend on the locale.
    """
    if len(names) > MAX_COMPONENTS:
        raise PathError(PathFault.INVALID)
    encoded: list[bytes] = []
    total = 0
    for item in names:
        candidate: object = item  # the annotation is for the checker; callers can pass anything
        if type(candidate) is not str:
            raise PathError(PathFault.INVALID)
        name = candidate
        if not name or name in {".", ".."} or "/" in name or "\0" in name:
            raise PathError(PathFault.INVALID)
        try:
            raw = name.encode("utf-8")
        except UnicodeEncodeError:
            raise PathError(PathFault.INVALID) from None
        if len(raw) > MAX_NAME_BYTES:
            raise PathError(PathFault.INVALID)
        total += len(raw) + 1
        encoded.append(raw)
    if total > MAX_PATH_BYTES:
        raise PathError(PathFault.INVALID)
    return encoded


def _fault_of(exc: OSError, parent_fd: int | None, name: bytes) -> PathError:
    """The `PathError` for an error from opening or looking at `name` in `parent_fd`.

    `parent_fd` is None when the failing call resolved several components at once.
    """
    code = exc.errno
    if code == errno.ELOOP:
        return PathError(PathFault.SYMLINK, code)
    if code == errno.ENOENT:
        return PathError(PathFault.MISSING, code)
    if code in {errno.EACCES, errno.EPERM}:
        return PathError(PathFault.DENIED, code)
    if code == errno.ENXIO:  # a socket, or a FIFO nobody has open
        return PathError(PathFault.SPECIAL_FILE, code)
    if code in {errno.ENAMETOOLONG, errno.EXDEV}:
        return PathError(PathFault.INVALID, code)
    if code == errno.ENOTDIR:
        # Linux says ENOTDIR, not ELOOP, for a symlink opened with O_DIRECTORY | O_NOFOLLOW, so
        # look at what the name is before calling it a file where a directory should be.
        if parent_fd is not None:
            try:
                mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
            except OSError:
                return PathError(PathFault.WRONG_KIND, code)
            if stat.S_ISLNK(mode):
                return PathError(PathFault.SYMLINK, code)
        return PathError(PathFault.WRONG_KIND, code)
    return PathError(PathFault.IO, code)


@final
class OpenedPath:
    """A descriptor for a regular file or a directory, with the identity it had when opened.

    The identity and the kind are read from the descriptor itself, so they cannot disagree with
    it. Whoever holds the object closes it (`close()`, or `with`); one that is garbage
    collected open is closed with a `ResourceWarning`. The descriptor is close-on-exec: to give
    it to a child (`--bind-fd`), pass it in `subprocess`'s `pass_fds`.
    """

    __slots__ = ("_fd", "_identity", "_kind")

    def __init__(self, fd: int) -> None:
        """Take ownership of `fd`. It is closed, and `PathError` raised, unless it is a regular
        file or a directory."""
        self._fd = -1
        try:
            mode = os.fstat(fd).st_mode
            identity = FsIdentity.of_fd(fd)
        except OSError as exc:
            os.close(fd)
            raise PathError(PathFault.IO, exc.errno) from None
        if stat.S_ISDIR(mode):
            kind = FileKind.DIRECTORY
        elif stat.S_ISREG(mode):
            kind = FileKind.REGULAR
        else:
            os.close(fd)
            raise PathError(PathFault.SPECIAL_FILE)
        self._fd = fd
        self._identity = identity
        self._kind = kind

    @property
    def fd(self) -> int:
        """The descriptor. Valid until `close()`."""
        if self._fd < 0:
            raise ValueError("the OpenedPath is closed")
        return self._fd

    @property
    def identity(self) -> FsIdentity:
        return self._identity

    @property
    def kind(self) -> FileKind:
        return self._kind

    @property
    def closed(self) -> bool:
        return self._fd < 0

    def close(self) -> None:
        """Close the descriptor. Closing twice is fine."""
        fd, self._fd = self._fd, -1
        if fd >= 0:
            os.close(fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        fd = getattr(self, "_fd", -1)
        if fd >= 0:
            warnings.warn(f"unclosed {self!r}", ResourceWarning, source=self, stacklevel=2)
            self.close()

    def __repr__(self) -> str:
        state = "closed" if self._fd < 0 else f"fd={self._fd}"
        return f"OpenedPath({getattr(self, '_kind', '?')}, {state})"


def _open_directories(start_fd: int, names: list[bytes], how: OpenStrategy) -> int:
    """Open the directory that `names` lead to from `start_fd`; the new descriptor is owned."""
    if not names:
        return os.dup(start_fd)
    if how is OpenStrategy.OPENAT2:
        try:
            return _openat2(start_fd, b"/".join(names), _PARENT_FLAGS, _RESOLVE_FLAGS)
        except OSError as exc:
            if exc.errno not in _NEEDS_DIAGNOSIS:
                raise _fault_of(exc, None, names[-1]) from None
        # The kernel refused a symlink or a non-directory, but one call cannot say which (a
        # symlink that is the last directory gives ENOTDIR): the walk below does, and refuses
        # the same way, since it follows no symlink either.
    current = os.dup(start_fd)
    try:
        for name in names:
            try:
                following = os.open(name, _PARENT_FLAGS, dir_fd=current)
            except OSError as exc:
                raise _fault_of(exc, current, name) from None
            os.close(current)
            current = following
    except BaseException:
        os.close(current)
        raise
    return current


def _open_last(parent_fd: int, name: bytes) -> int:
    """Open the last component of a path: a regular file or a directory, never a symlink."""
    try:
        mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
    except OSError as exc:
        raise _fault_of(exc, parent_fd, name) from None
    if stat.S_ISLNK(mode):
        raise PathError(PathFault.SYMLINK)
    if stat.S_ISDIR(mode):
        flags = _DIRECTORY_FLAGS
    elif stat.S_ISREG(mode):
        flags = _FILE_FLAGS
    else:
        raise PathError(PathFault.SPECIAL_FILE)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _fault_of(exc, parent_fd, name) from None


def _open_in(
    start_fd: int, names: list[bytes], want: FileKind | None, how: OpenStrategy
) -> OpenedPath:
    parent = _open_directories(start_fd, names[:-1], how)
    try:
        fd = _open_last(parent, names[-1])
    finally:
        os.close(parent)
    opened = OpenedPath(fd)  # looks at the descriptor, not at the name: the name may have moved
    if want is not None and opened.kind is not want:
        opened.close()
        raise PathError(PathFault.WRONG_KIND)
    return opened


def open_absolute(
    names: Sequence[str],
    *,
    want: FileKind | None = None,
    strategy: OpenStrategy = OpenStrategy.AUTO,
) -> OpenedPath:
    """Open the file or directory at `/` + `names`, component by component, never through a symlink.

    `names` are the components of an absolute path, in order (none for `/` itself). `want`, if
    given, is the kind it must be. Raises `PathError`; on any doubt it refuses.
    """
    encoded = _encode(names)
    how = _chosen(strategy)
    try:
        root = os.open("/", _DIRECTORY_FLAGS)
    except OSError as exc:
        raise _fault_of(exc, None, b"/") from None
    try:
        if not encoded:
            opened = OpenedPath(os.dup(root))
            if want is not None and opened.kind is not want:
                opened.close()
                raise PathError(PathFault.WRONG_KIND)
            return opened
        return _open_in(root, encoded, want, how)
    finally:
        os.close(root)


def open_below(
    root: OpenedPath,
    names: Sequence[str],
    *,
    want: FileKind | None = None,
    expect: FsIdentity | None = None,
    strategy: OpenStrategy = OpenStrategy.AUTO,
) -> OpenedPath:
    """Open the file or directory at `names` below the directory `root`, never through a symlink.

    Nothing can leave `root`: `names` cannot hold `..` (see `_encode`), and no symlink is
    followed. At least one name is required. If `expect` is given, what was opened must have
    that identity (`IDENTITY_MISMATCH` otherwise): the identity a decision saw. Raises
    `PathError`.
    """
    if root.kind is not FileKind.DIRECTORY:
        raise PathError(PathFault.WRONG_KIND)
    if not names:
        raise PathError(PathFault.INVALID)
    opened = _open_in(root.fd, _encode(names), want, _chosen(strategy))
    if expect is not None and opened.identity != expect:
        opened.close()
        raise PathError(PathFault.IDENTITY_MISMATCH)
    return opened


class Walk(NamedTuple):
    """What `walk` found: the identity chain, and whether the target is a shared directory."""

    chain: tuple[FsIdentity, ...]
    shared_directory: bool


def _identity_of(fd: int) -> FsIdentity:
    try:
        return FsIdentity.of_fd(fd)
    except OSError as exc:
        raise PathError(PathFault.IO, exc.errno) from None


def identity_chain(names: Sequence[str]) -> tuple[FsIdentity, ...]:
    """The identities `walk` finds, without the rest."""
    return walk(names).chain


def walk(names: Sequence[str]) -> Walk:
    """The identity of `/` and of each prefix of `names` that exists, from `/` downwards, and
    whether the target is a directory that anyone can write to and that is sticky (`/tmp`).

    Walks without following symlinks, like `open_absolute`, and stops without error at the first
    component that does not exist, is not a directory (when more follows), is a socket, FIFO or
    device, or cannot be opened for lack of permission. Those are "not there for our purposes":
    the chain is then shorter than `len(names) + 1`, and the caller treats the target as one
    that has no identity. A symlink is an error (the caller canonicalised the path first, so
    one now means it changed under us), as is anything the operating system reports otherwise
    (always a `PathError`, never a bare `OSError`).

    Like every open here this can wait for as long as the file system takes to answer: on a hung
    network or FUSE mount it blocks, and nothing in this module puts a deadline on it. The
    broker daemon decides on a worker it can abandon (S17).
    """
    encoded = _encode(names)
    try:
        current = os.open("/", _DIRECTORY_FLAGS)
    except OSError as exc:
        raise _fault_of(exc, None, b"/") from None
    chain: list[FsIdentity] = []
    shared = False
    try:
        chain.append(_identity_of(current))
        for index, name in enumerate(encoded):
            following = _open_for_chain(current, name, last=index == len(encoded) - 1)
            if following is None:
                break
            os.close(current)
            current = following
            chain.append(_identity_of(current))
        if len(chain) == len(encoded) + 1:
            try:
                mode = os.fstat(current).st_mode
            except OSError as exc:
                raise PathError(PathFault.IO, exc.errno) from None
            shared = stat.S_ISDIR(mode) and bool(mode & stat.S_ISVTX) and bool(mode & stat.S_IWOTH)
    finally:
        os.close(current)
    return Walk(tuple(chain), shared)


def _open_for_chain(parent_fd: int, name: bytes, *, last: bool) -> int | None:
    """Open `name` for `walk`, or return None where the walk should stop."""
    try:
        if last:
            mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                raise PathError(PathFault.SYMLINK)
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                return None
            flags = _DIRECTORY_FLAGS if stat.S_ISDIR(mode) else _FILE_FLAGS
        else:
            flags = _DIRECTORY_FLAGS
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        error = _fault_of(exc, parent_fd, name)
        if error.fault in {PathFault.MISSING, PathFault.WRONG_KIND, PathFault.DENIED}:
            return None
        raise error from None
    if last:
        try:
            opened_mode = os.fstat(fd).st_mode
        except OSError as exc:
            os.close(fd)
            raise PathError(PathFault.IO, exc.errno) from None
        if not (stat.S_ISDIR(opened_mode) or stat.S_ISREG(opened_mode)):
            os.close(fd)  # swapped for a special file between the check and the open
            return None
    return fd


def read_capped(file: OpenedPath, max_bytes: ByteCount) -> bytes:
    """Read a whole regular file, or refuse if it is larger than `max_bytes`.

    The size is checked before reading and again while reading (the file may grow), so at most
    `max_bytes + 1` bytes are ever held. `max_bytes` may not exceed `MAX_READ_BYTES`. Reads from
    offset 0, and leaves the descriptor open.
    """
    raw: object = max_bytes  # the annotation is for the checker; callers can pass anything
    if type(raw) is not ByteCount or raw > MAX_READ_BYTES:
        raise PathError(PathFault.INVALID)
    if file.kind is not FileKind.REGULAR:
        raise PathError(PathFault.WRONG_KIND)
    limit = int(max_bytes)
    try:
        if os.fstat(file.fd).st_size > limit:
            raise PathError(PathFault.TOO_LARGE)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.pread(file.fd, min(_CHUNK, limit + 1 - total), total)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise PathError(PathFault.TOO_LARGE)
            chunks.append(chunk)
    except OSError as exc:
        raise PathError(PathFault.IO, exc.errno) from None
