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


def remove_chain(parent: Path, name: str) -> None:
    """Remove a chain of directories of any depth (what a test left that `shutil.rmtree` cannot
    take: it recurses). Independent of the code under test, so a failing test leaves nothing."""
    try:
        top = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        trail: list[tuple[int, str]] = []
        here = top
        current = name
        while True:
            try:
                child = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=here)
            except OSError:
                break
            trail.append((here, current))
            here, current = child, ""
            with os.scandir(here) as scan:
                names = [entry.name for entry in scan]
            if not names:
                break
            current = names[0]
        os.close(here) if trail else None
        for opened, each in reversed(trail):
            os.rmdir(each, dir_fd=opened)
            if opened != top:
                os.close(opened)
    finally:
        os.close(top)
