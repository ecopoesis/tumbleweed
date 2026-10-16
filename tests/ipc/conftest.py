"""Fixtures for the IPC tests: a private socket directory, and a guard against lost errors."""

import asyncio
import gc
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


@pytest.fixture(autouse=True)
def no_error_is_lost_to_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Fail a test during which asyncio had to report an error nobody handled.

    asyncio's last resort for an exception that no one awaited (a task that failed alone, a
    future whose exception was never retrieved, a callback that raised, a task destroyed while
    pending) is the loop's exception handler, which only logs. In these tests that is always a
    bug: something in the IPC code lost an error.
    """
    lost: list[str] = []
    original = asyncio.BaseEventLoop.call_exception_handler

    def record(self: asyncio.BaseEventLoop, context: dict[str, object]) -> None:
        lost.append(f"{context.get('message')}: {context.get('exception')!r}")
        original(self, context)

    monkeypatch.setattr(asyncio.BaseEventLoop, "call_exception_handler", record)
    yield lost  # a test of this guard may read it, and clear it when it lost an error on purpose
    gc.collect()  # a lost future or task reports itself when it is collected
    assert not lost, "asyncio reported errors that nothing handled: " + "; ".join(lost)
