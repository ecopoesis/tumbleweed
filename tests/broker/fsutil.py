"""File-system helpers for the broker tests: special files and permission checks."""

import contextlib
import os
import socket
from pathlib import Path


def make_fifo(path: Path) -> Path:
    os.mkfifo(path)
    return path


def make_socket(path: Path) -> Path:
    """A unix socket file at `path`. It is bound by its bare name from its own directory, as
    `sun_path` is too short (104 bytes on macOS) for the temporary directories of a test run."""
    with contextlib.chdir(path.parent), socket.socket(socket.AF_UNIX) as sock:
        sock.bind(path.name)
    return path


def can_search_denied_directories() -> bool:
    """False for root, which ignores permission bits: tests that rely on EACCES skip then."""
    return os.geteuid() != 0
