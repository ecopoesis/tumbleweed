"""Killing a child and waiting for it, bounded: what happens when the kernel does not answer."""

import asyncio
import shutil
from datetime import timedelta

import pytest

from tests.sandbox.support import Opened, policy
from tumbleweed.sandbox import process, runner
from tumbleweed.sandbox.errors import SandboxTeardownError
from tumbleweed.sandbox.policy import SandboxLimits
from tumbleweed.sandbox.process import kill_and_reap
from tumbleweed.sandbox.runner import SandboxRunner


class Stuck:
    """A process that never ends as far as `wait` can tell (a task in uninterruptible sleep)."""

    returncode: int | None = None

    def __init__(self) -> None:
        self.killed = 0

    def kill(self) -> None:
        self.killed += 1

    async def wait(self) -> int:
        await asyncio.sleep(3600)
        return 0


class Gone:
    """A process that ended between our check and our kill."""

    returncode: int | None = None

    def kill(self) -> None:
        raise ProcessLookupError

    async def wait(self) -> int:
        return 0


def test_a_process_that_does_not_end_is_given_up_on_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "REAP_TIMEOUT_SECONDS", 0.1)
    stuck = Stuck()

    reaped = asyncio.run(kill_and_reap(stuck))  # type: ignore[arg-type]  # a stand-in with kill and wait

    assert reaped is False
    assert stuck.killed == 1


def test_a_process_that_ended_before_the_kill_is_reaped_without_an_error() -> None:
    assert asyncio.run(kill_and_reap(Gone())) is True  # type: ignore[arg-type]  # a stand-in


@pytest.mark.linux
def test_a_sandbox_that_cannot_be_reaped_is_a_teardown_error_not_a_hang(
    workspace: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) is not installed")
    real = process.kill_and_reap
    calls = 0

    async def refusing(proc: asyncio.subprocess.Process) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            await real(proc)  # really kill it, so that the test leaves nothing behind
            return False  # but report that the kernel did not answer
        return await real(proc)

    monkeypatch.setattr(runner, "kill_and_reap", refusing)
    limits = SandboxLimits(wall_time=timedelta(seconds=1))

    with pytest.raises(SandboxTeardownError, match="did not end after SIGKILL"):
        asyncio.run(
            SandboxRunner(use_systemd_scope=False).run(
                policy(workspace, "/usr/bin/sleep", "60", limits=limits)
            )
        )
