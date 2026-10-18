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
from collections.abc import Callable, Sequence
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
    """`stop_groups` for one process."""
    return stop_groups([(pid, gone)], first=first, grace=grace, patience=patience)


def stop_groups(
    targets: Sequence[tuple[int, Callable[[], bool]]],
    *,
    first: signal.Signals = signal.SIGTERM,
    grace: float = GRACE_SECONDS,
    patience: float = KILL_SECONDS,
) -> bool:
    """Signal the process group of every `pid` (its own: the leader of a session of its own) with
    `first`, wait up to `grace` seconds for every `gone()`, then SIGKILL the ones left and wait up
    to `patience` more. The waits share one deadline each, so the total is at most
    `grace + patience` however many there are. True if every process went. A `pid` must not have
    been reaped, or the number may be reused.

    A group that is not led by `pid` is not signalled by group, only the process itself is.
    """
    pending = list(targets)
    for number, limit in ((first, grace), (signal.SIGKILL, patience)):
        for pid, _gone in pending:
            _signal(pid, number)
        deadline = time.monotonic() + limit
        while True:
            pending = [(pid, gone) for pid, gone in pending if not gone()]
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(_POLL_SECONDS)
        if not pending:
            return True
    for pid, _gone in pending:
        logger.warning("process %d did not end after SIGKILL", pid)
    return all(gone() for _pid, gone in pending)


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
        # Every group is signalled first and the wait is shared: closing the window waits at most
        # `GRACE_SECONDS + KILL_SECONDS` in all, not that much for each.
        stop_groups(
            [
                (process.pid, functools.partial(_exited, process))
                for process in running
                if process.poll() is None
            ]
        )
