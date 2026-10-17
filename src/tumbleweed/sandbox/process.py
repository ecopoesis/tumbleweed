"""Killing and reaping a child process, with a bound on the wait (every wait has a timeout)."""

import asyncio
import contextlib
from typing import Final

REAP_TIMEOUT_SECONDS: Final = 5.0


async def kill_and_reap(proc: asyncio.subprocess.Process) -> bool:
    """`SIGKILL` the process and wait for the kernel to report it. False if that took too long.

    A killed process normally ends at once; one stuck in the kernel (an uninterruptible wait on a
    broken filesystem) does not, and the caller must not wait for it forever.
    """
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):  # it ended between the check and the kill
            proc.kill()
    try:
        async with asyncio.timeout(REAP_TIMEOUT_SECONDS):
            await proc.wait()
    except TimeoutError:
        return False
    return True
