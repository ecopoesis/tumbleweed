"""Fixtures for the IPC tests: a private socket directory with a path short enough for a socket."""

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tumbleweed.core.types import CanonicalPath
from tumbleweed.ipc.sockdir import SocketDirectory, SocketPath


@pytest.fixture
def base() -> Iterator[CanonicalPath]:
    """A fresh 0700 directory of ours to act as `$XDG_RUNTIME_DIR`.

    Made by `mkdtemp` under `/tmp`, not by pytest's `tmp_path` or the system's temporary
    directory: on macOS those are over 60 bytes long, which leaves too little of the 104 a Unix
    socket address can hold. (`/tmp` there is `/private/tmp`, 23 bytes with our prefix.)
    """
    directory = Path(tempfile.mkdtemp(prefix="twipc-", dir="/tmp"))
    try:
        yield CanonicalPath.resolve(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def socket_dir(base: CanonicalPath) -> SocketDirectory:
    return SocketDirectory(base, create=True)


@pytest.fixture
def sock_path(socket_dir: SocketDirectory) -> SocketPath:
    return socket_dir.socket_path("broker")
