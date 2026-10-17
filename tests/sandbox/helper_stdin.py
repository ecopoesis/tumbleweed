"""Runs a sandbox whose launcher has a pipe on stdin, and prints what the program has on its own.

    python -m tests.sandbox.helper_stdin WORKSPACE

The test feeds the pipe and reads the one line printed: where the program's descriptor 0 leads.
"""

import asyncio
import sys
from pathlib import Path

from tests.sandbox.support import PYTHON, Opened, policy
from tumbleweed.sandbox.runner import SandboxRunner


async def main(workspace: Path) -> None:
    code = "import os, sys; print(os.readlink('/proc/self/fd/0'), repr(sys.stdin.read()))"
    made = policy(Opened(workspace), PYTHON, "-c", code)
    result = await SandboxRunner(use_systemd_scope=False).run(made)
    sys.stdout.write(result.stdout.decode() + result.stderr.decode())


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
