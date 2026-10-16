"""Runs one long sandbox and waits to be killed: the parent in "killed with its parent".

    python -m tests.sandbox.helper_orphan WORKSPACE TOKEN

Prints `ready` when the sandbox has been started. The test then kills this process with SIGKILL
and looks for the sandboxed program, whose command line holds TOKEN.
"""

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

from tests.sandbox.support import Opened, policy
from tumbleweed.sandbox.policy import SandboxLimits
from tumbleweed.sandbox.runner import SandboxRunner


async def main(workspace: Path, token: str) -> None:
    handle = Opened(workspace)
    limits = SandboxLimits(wall_time=timedelta(seconds=120), cpu_time=timedelta(seconds=120))
    made = policy(handle, "/usr/bin/sleep", f"100.{token}", limits=limits)
    run = asyncio.create_task(SandboxRunner(use_systemd_scope=False).run(made))
    await asyncio.sleep(1.0)  # the sandbox is up long before this
    sys.stdout.write(f"ready {os.getpid()}\n")
    sys.stdout.flush()
    await run


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
