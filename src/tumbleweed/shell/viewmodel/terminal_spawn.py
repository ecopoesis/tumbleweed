"""Starting and stopping the processes of the terminal escape hatch (S05): the real external
spawner, and the bounded signal-and-wait that ends a process group.

Every wait is bounded (`GRACE_SECONDS`, `KILL_SECONDS`), and nothing here sleeps for longer than
a few milliseconds at a time. This module imports no GTK.
"""

import functools
import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable
from typing import Final, final

from tumbleweed.shell.viewmodel.terminal import ExternalCommand, TerminalError

logger = logging.getLogger(__name__)

MAX_RUNNING: Final = 8
GRACE_SECONDS: Final = 1.0
KILL_SECONDS: Final = 1.0
_POLL_SECONDS: Final = 0.01


def stop_group(
    pid: int,
    gone: Callable[[], bool],
    *,
    first: signal.Signals = signal.SIGTERM,
    grace: float = GRACE_SECONDS,
    patience: float = KILL_SECONDS,
) -> bool:
    """Signal the process group of `pid` (its own: the leader of a session of its own) with
    `first`, wait up to `grace` seconds for `gone()`, then SIGKILL it and wait up to `patience`
    more. True if the process went. `pid` must not have been reaped, or the number may be reused.

    A group that is not led by `pid` is not signalled by group, only the process itself is.
    """
    for number, limit in ((first, grace), (signal.SIGKILL, patience)):
        _signal(pid, number)
        deadline = time.monotonic() + limit
        while True:
            if gone():
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_SECONDS)
    logger.warning("process %d did not end after SIGKILL", pid)
    return gone()


def _signal(pid: int, number: signal.Signals) -> None:
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, number)
        else:
            os.kill(pid, number)
    except ProcessLookupError:
        return  # already gone


def _exited(process: subprocess.Popen[bytes]) -> bool:
    return process.poll() is not None


@final
class SubprocessSpawner:
    """Starts external terminals with `subprocess`: argv list, explicit environment, its own
    session, no stdio. What it started and is still running is signalled by `close`.

    Not thread-safe: the shell's main thread is the only caller.
    """

    def __init__(self) -> None:
        self._running: list[subprocess.Popen[bytes]] = []

    def spawn(self, command: ExternalCommand) -> None:
        self._running = [process for process in self._running if process.poll() is None]
        if len(self._running) >= MAX_RUNNING:
            raise TerminalError(f"{MAX_RUNNING} terminals started from here are still open")
        try:
            process = subprocess.Popen(  # noqa: S603  (an argv list from our own table, no shell)
                list(command.argv),
                cwd=command.cwd,
                env=dict(command.env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                shell=False,
            )
        except (OSError, ValueError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else "error"
            logger.warning("starting %s failed: %s", command.argv[0], type(exc).__name__)
            raise TerminalError(f"Could not start {command.argv[0]}: {reason}.") from None
        self._running.append(process)

    def close(self) -> None:
        running, self._running = self._running, []
        for process in running:
            if process.poll() is None:
                stop_group(process.pid, functools.partial(_exited, process))
