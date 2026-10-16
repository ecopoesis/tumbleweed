"""Exceptions the broker's policy core raises on purpose.

Messages are fixed text: they never repeat a path or any other input, because they end up in
logs and in what the model is told (CLAUDE.md, "bounded error messages"). What went wrong is
the `fault`, a closed set the caller can act on.
"""

import errno
from enum import StrEnum
from typing import assert_never, final

from tumbleweed.core.errors import TumbleweedError


class BrokerError(TumbleweedError):
    """Base class of every exception the broker package raises on purpose."""


class GrantError(BrokerError):
    """A grant was used against its own rules (a once grant consumed by another run, or twice)."""


class PathFault(StrEnum):
    """Why a path could not be resolved, opened or read."""

    INVALID = "invalid"  # the text is not an acceptable path (or a limit argument is out of range)
    SYMLINK = "symlink"  # a component is a symbolic link; none is ever followed
    MISSING = "missing"
    WRONG_KIND = "wrong_kind"  # a directory where a file was wanted, or the other way round
    SPECIAL_FILE = "special_file"  # a socket, FIFO or device: only files and directories open
    IDENTITY_MISMATCH = "identity_mismatch"  # what is there is not what the grant was made for
    DENIED = "denied"  # the operating system refused (permissions)
    PROTECTED = "protected"  # the static deny rules refuse it
    TOO_LARGE = "too_large"
    UNSUPPORTED = "unsupported"  # this host cannot do what was asked (openat2 off Linux)
    IO = "io"  # any other operating-system error


def describe_fault(fault: PathFault) -> str:
    """Fixed text for a fault."""
    match fault:
        case PathFault.INVALID:
            return "not an acceptable path"
        case PathFault.SYMLINK:
            return "a path component is a symbolic link"
        case PathFault.MISSING:
            return "no such file or directory"
        case PathFault.WRONG_KIND:
            return "a file or directory of the wrong kind"
        case PathFault.SPECIAL_FILE:
            return "not a regular file or directory"
        case PathFault.IDENTITY_MISMATCH:
            return "the file system object is not the one that was granted"
        case PathFault.DENIED:
            return "permission denied"
        case PathFault.PROTECTED:
            return "a protected location"
        case PathFault.TOO_LARGE:
            return "larger than the read limit"
        case PathFault.UNSUPPORTED:
            return "not supported on this host"
        case PathFault.IO:
            return "operating system error"
        case _ as unreachable:
            assert_never(unreachable)


@final
class PathError(BrokerError):
    """A path was refused or could not be opened. Fail closed: the caller must not retry by
    other means (such as resolving the text again).

    `fault` says why. `errno_name` is the operating system's name for the error code that
    caused it (`"EACCES"`), when there was one.
    """

    def __init__(self, fault: PathFault, errno_code: int | None = None) -> None:
        self.fault = fault
        self.errno_code = errno_code
        self.errno_name = errno.errorcode.get(errno_code) if errno_code is not None else None
        suffix = f" ({self.errno_name})" if self.errno_name is not None else ""
        super().__init__(f"{fault.value}: {describe_fault(fault)}{suffix}")

    def __reduce__(self) -> tuple[type["PathError"], tuple[PathFault, int | None]]:
        # The default would rebuild from the message, which is not what the constructor takes.
        return (type(self), (self.fault, self.errno_code))
