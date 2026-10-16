"""Looking at the host's processes and descriptors from a test (Linux)."""

import os
import time
from pathlib import Path


def pids_with(token: str) -> list[int]:
    """Processes of the host whose command line contains `token` (a sandboxed program's too)."""
    found: list[int] = []
    needle = token.encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue  # it ended while we looked
        if needle in command:
            found.append(int(entry.name))
    return found


def wait_until_none(token: str, seconds: float) -> list[int]:
    """The processes with `token` still alive after waiting up to `seconds` for them to go."""
    deadline = time.monotonic() + seconds
    while True:
        alive = pids_with(token)
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.02)


def children_of_this_process() -> list[tuple[int, str]]:
    """(pid, state) of every child of the test process: `Z` is a zombie nobody reaped."""
    mine = os.getpid()
    found: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # "pid (comm) state ppid ...": the command may hold spaces and parentheses.
        state, ppid = stat.rpartition(")")[2].split()[:2]
        if int(ppid) == mine:
            found.append((int(entry.name), state))
    return found


def open_descriptors() -> set[int]:
    return {int(entry.name) for entry in Path("/proc/self/fd").iterdir()}
