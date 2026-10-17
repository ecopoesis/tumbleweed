"""The limits hold, and nothing outlives a run: memory, time, CPU, files, output, processes.

Every limit test has its control: the same program that is stopped by the limit succeeds without
it, so that a program failing for another reason cannot pass for a limit that worked.
"""

import asyncio
import contextlib
import os
import resource
import shutil
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from tests.sandbox.fake_systemd import ALL_LIMITS, FakeSystemd
from tests.sandbox.procs import (
    children_of_this_process,
    open_descriptors,
    pids_with,
    wait_until_none,
)
from tests.sandbox.support import PYTHON, Opened, grant, opened, policy, run_sandbox
from tumbleweed.core.types import ByteCount
from tumbleweed.sandbox.errors import InvalidPolicyError, SandboxLaunchError
from tumbleweed.sandbox.limits import kernel_counts_processes_per_namespace
from tumbleweed.sandbox.policy import GrantAccess, SandboxLimits
from tumbleweed.sandbox.result import LimitKind, LimitsBackend, LimitsReport, Termination
from tumbleweed.sandbox.runner import SandboxRunner

pytestmark = pytest.mark.linux

MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def need_bwrap() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) is not installed")


def token() -> str:
    """Digits no other command line has, to find a sandboxed program among the host's processes."""
    return str(time.time_ns())[-12:]


class TestWallClock:
    def test_a_program_that_outlives_the_wall_clock_is_killed_and_reported(
        self, workspace: Opened
    ) -> None:
        mark = token()
        limits = SandboxLimits(wall_time=timedelta(seconds=1))
        started = time.monotonic()

        result = run_sandbox(policy(workspace, "/usr/bin/sleep", f"60.{mark}", limits=limits))

        assert result.termination is Termination.TIMED_OUT
        assert result.exit_code is None
        assert 1.0 <= time.monotonic() - started < 3.0  # on time: the wall clock and 2 s of slack
        assert 1.0 <= result.wall_time.total_seconds() < 3.0
        assert wait_until_none(mark, 3.0) == []

    def test_the_control_the_same_program_finishes_inside_the_wall_clock(
        self, workspace: Opened
    ) -> None:
        limits = SandboxLimits(wall_time=timedelta(seconds=10))

        result = run_sandbox(policy(workspace, "/usr/bin/sleep", "0.2", limits=limits))

        assert result.termination is Termination.EXITED
        assert result.exit_code == 0

    def test_every_process_of_the_sandbox_dies_with_it_including_daemons(
        self, workspace: Opened, workspace_dir: Path
    ) -> None:
        mark = token()
        script = (
            f"(sleep 70.{mark} &); (setsid sleep 71.{mark} &); sleep 72.{mark} & "
            f"(nohup sleep 73.{mark} >/dev/null 2>&1 &); "
            f"ps_count=$(grep -l {mark} /proc/[0-9]*/cmdline | wc -l); "
            f"echo $ps_count > /workspace/count; wait"
        )
        limits = SandboxLimits(wall_time=timedelta(seconds=2))

        result = run_sandbox(policy(workspace, "/bin/sh", "-c", script, limits=limits))

        assert result.termination is Termination.TIMED_OUT
        assert int((workspace_dir / "count").read_text()) >= 4, "the tree never grew"
        assert wait_until_none(mark, 3.0) == []

    def test_nothing_is_left_to_reap_after_a_kill(self, workspace: Opened) -> None:
        limits = SandboxLimits(wall_time=timedelta(seconds=1))

        run_sandbox(policy(workspace, "/usr/bin/sleep", "60", limits=limits))

        assert children_of_this_process() == []

    def test_a_program_that_ignores_sigterm_and_hangs_up_is_killed_all_the_same(
        self, workspace: Opened
    ) -> None:
        mark = token()
        code = (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"time.sleep(60.{mark})"
        )
        limits = SandboxLimits(wall_time=timedelta(seconds=1))

        started = time.monotonic()

        result = run_sandbox(policy(workspace, PYTHON, "-c", code, limits=limits))

        assert result.termination is Termination.TIMED_OUT
        assert time.monotonic() - started < 3.0
        assert wait_until_none(mark, 3.0) == []


class TestOutput:
    def test_a_flood_on_stdout_is_cut_at_the_cap_and_the_sandbox_killed(
        self, workspace: Opened
    ) -> None:
        mark = token()
        limits = SandboxLimits(max_output_bytes=ByteCount(10_000), wall_time=timedelta(seconds=30))
        started = time.monotonic()

        result = run_sandbox(policy(workspace, "/usr/bin/yes", mark, limits=limits))

        assert result.termination is Termination.OUTPUT_LIMIT
        assert result.exit_code is None
        assert len(result.stdout) == 10_000
        assert result.stdout_truncated
        assert not result.stderr_truncated
        assert result.stdout.startswith(f"{mark}\n".encode())
        assert time.monotonic() - started < 8.0, "waited for the wall clock instead of the cap"
        assert wait_until_none(mark, 3.0) == []

    def test_a_flood_on_stderr_is_cut_too(self, workspace: Opened) -> None:
        limits = SandboxLimits(max_output_bytes=ByteCount(4096))

        result = run_sandbox(policy(workspace, "/bin/sh", "-c", "yes >&2", limits=limits))

        assert result.termination is Termination.OUTPUT_LIMIT
        assert len(result.stderr) == 4096
        assert result.stderr_truncated
        assert result.stdout == b""

    def test_each_stream_has_its_own_cap(self, workspace: Opened) -> None:
        limits = SandboxLimits(max_output_bytes=ByteCount(1000))
        code = "import sys; sys.stdout.write('o' * 900); sys.stderr.write('e' * 900)"

        result = run_sandbox(policy(workspace, PYTHON, "-c", code, limits=limits))

        assert result.termination is Termination.EXITED
        assert (len(result.stdout), len(result.stderr)) == (900, 900)
        assert not result.stdout_truncated
        assert not result.stderr_truncated

    def test_output_of_exactly_the_cap_is_complete_and_one_byte_more_is_cut(
        self, workspace: Opened
    ) -> None:
        limits = SandboxLimits(max_output_bytes=ByteCount(100))

        exact = run_sandbox(
            policy(workspace, PYTHON, "-c", "print('x' * 99)", limits=limits)  # 99 and a newline
        )
        over = run_sandbox(policy(workspace, PYTHON, "-c", "print('x' * 100)", limits=limits))

        assert exact.termination is Termination.EXITED
        assert exact.stdout == b"x" * 99 + b"\n"
        assert not exact.stdout_truncated
        assert over.stdout == b"x" * 100
        assert over.stdout_truncated
        assert over.termination in {Termination.EXITED, Termination.OUTPUT_LIMIT}

    def test_a_child_that_fills_one_pipe_while_we_read_the_other_does_not_deadlock(
        self, workspace: Opened
    ) -> None:
        # 3 MiB on stderr before a byte on stdout: a launcher that read stdout to its end first
        # would wait forever with the child blocked on a full stderr pipe.
        code = (
            "import os, sys\n"
            "os.write(2, b'e' * (3 * 1024 * 1024))\n"
            "os.write(1, b'o' * (3 * 1024 * 1024))\n"
        )
        limits = SandboxLimits(max_output_bytes=ByteCount(4 * MIB), wall_time=timedelta(seconds=30))

        result = run_sandbox(policy(workspace, PYTHON, "-c", code, limits=limits))

        assert result.termination is Termination.EXITED
        assert (len(result.stdout), len(result.stderr)) == (3 * MIB, 3 * MIB)

    def test_output_is_bytes_whatever_they_are(self, workspace: Opened) -> None:
        code = "import os; os.write(1, bytes(range(256))); os.write(2, b'\\xff\\xfe')"

        result = run_sandbox(policy(workspace, PYTHON, "-c", code))

        assert result.stdout == bytes(range(256))
        assert result.stderr == b"\xff\xfe"

    def test_the_launchers_memory_stays_bounded_while_a_child_floods_it(
        self, workspace: Opened
    ) -> None:
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        limits = SandboxLimits(max_output_bytes=ByteCount(MIB))

        run_sandbox(policy(workspace, "/usr/bin/yes", limits=limits))

        grown_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
        assert grown_kib < 30 * 1024, f"the launcher's peak memory grew by {grown_kib} KiB"


class TestMemory:
    ALLOCATE = (
        "x = bytearray({mib} * 1024 * 1024); x[::4096] = b'x' * len(x[::4096]); print('allocated')"
    )

    def test_a_program_that_wants_more_memory_than_its_limit_is_stopped(
        self, workspace: Opened
    ) -> None:
        limits = SandboxLimits(memory=ByteCount(256 * MIB))

        result = run_sandbox(
            policy(workspace, PYTHON, "-c", self.ALLOCATE.format(mib=900), limits=limits)
        )

        assert result.exit_code != 0
        assert b"allocated" not in result.stdout
        assert b"MemoryError" in result.stderr

    def test_the_control_the_same_program_under_a_larger_limit_succeeds(
        self, workspace: Opened
    ) -> None:
        limits = SandboxLimits(memory=ByteCount(2048 * MIB))

        result = run_sandbox(
            policy(workspace, PYTHON, "-c", self.ALLOCATE.format(mib=900), limits=limits)
        )

        assert result.exit_code == 0
        assert result.stdout == b"allocated\n"

    def test_a_modest_program_is_not_troubled_by_a_modest_limit(self, workspace: Opened) -> None:
        limits = SandboxLimits(memory=ByteCount(256 * MIB))

        result = run_sandbox(
            policy(workspace, PYTHON, "-c", self.ALLOCATE.format(mib=32), limits=limits)
        )

        assert result.stdout == b"allocated\n"

    def test_memory_is_limited_in_every_process_the_program_starts(self, workspace: Opened) -> None:
        limits = SandboxLimits(memory=ByteCount(256 * MIB))
        child = self.ALLOCATE.format(mib=900)
        code = (
            f"import subprocess, sys; sys.exit(subprocess.call([sys.executable, '-c', {child!r}]))"
        )

        result = run_sandbox(policy(workspace, PYTHON, "-c", code, limits=limits))

        assert result.exit_code != 0
        assert b"MemoryError" in result.stderr


class TestCpuAndFiles:
    def test_a_program_that_spins_is_killed_by_the_cpu_limit(self, workspace: Opened) -> None:
        limits = SandboxLimits(cpu_time=timedelta(seconds=1), wall_time=timedelta(seconds=30))
        started = time.monotonic()

        result = run_sandbox(policy(workspace, PYTHON, "-c", "while True: pass", limits=limits))

        assert result.termination is Termination.EXITED
        assert result.exit_code == 137  # SIGKILL: the hard limit, which cannot be caught
        assert time.monotonic() - started < 15.0

    def test_waiting_costs_no_cpu_time(self, workspace: Opened) -> None:
        limits = SandboxLimits(cpu_time=timedelta(seconds=1), wall_time=timedelta(seconds=30))

        result = run_sandbox(policy(workspace, "/usr/bin/sleep", "2.5", limits=limits))

        assert result.exit_code == 0

    def test_a_file_larger_than_the_limit_cannot_be_written(
        self, workspace: Opened, workspace_dir: Path
    ) -> None:
        limits = SandboxLimits(file_size=ByteCount(MIB))
        code = (
            "import errno\n"
            "for name, size in (('small', 512 * 1024), ('big', 4 * 1024 * 1024)):\n"
            "    try:\n"
            "        open('/workspace/' + name, 'wb').write(b'x' * size)\n"
            "        print(name, 'ok')\n"
            "    except OSError as exc:\n"
            "        print(name, errno.errorcode[exc.errno])\n"
        )

        result = run_sandbox(policy(workspace, PYTHON, "-c", code, limits=limits))

        assert result.stdout == b"small ok\nbig EFBIG\n"
        assert (workspace_dir / "small").stat().st_size == 512 * 1024
        assert (workspace_dir / "big").stat().st_size <= MIB

    def test_the_sandboxs_tmpfs_mounts_have_the_size_limit(self, workspace: Opened) -> None:
        limits = SandboxLimits(tmp_size=ByteCount(MIB), file_size=ByteCount(64 * MIB))
        code = (
            "import errno\n"
            "for directory in ('/tmp', '/dev/shm'):\n"
            "    total = 0\n"
            "    try:\n"
            "        for number in range(100):\n"
            "            with open(f'{directory}/f{number}', 'wb') as handle:\n"
            "                handle.write(b'x' * 200_000)\n"
            "            total += 200_000\n"
            "        print(directory, 'filled', total)\n"
            "    except OSError as exc:\n"
            "        print(directory, errno.errorcode[exc.errno], total <= 1024 * 1024)\n"
        )

        result = run_sandbox(policy(workspace, PYTHON, "-c", code, limits=limits))

        assert result.stdout == b"/tmp ENOSPC True\n/dev/shm ENOSPC True\n"


class TestProcessesAndFiles:
    FORK = (
        "import os, time\n"
        "kids = []\n"
        "try:\n"
        "    for _ in range(300):\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            time.sleep(10)\n"
        "            os._exit(0)\n"
        "        kids.append(pid)\n"
        "    print('forked', len(kids))\n"
        "except OSError as exc:\n"
        "    print('stopped', len(kids), exc.errno)\n"
        "for kid in kids:\n"
        "    os.kill(kid, 9)\n"
    )

    @pytest.fixture(autouse=True)
    def need_a_kernel_with_per_namespace_process_counts(self) -> None:
        if not kernel_counts_processes_per_namespace(os.uname().release):
            pytest.skip("RLIMIT_NPROC counts the user's other processes before Linux 5.14")

    def test_without_a_scope_a_fork_bomb_stops_near_the_pids_limit(self, workspace: Opened) -> None:
        limits = SandboxLimits(pids=20, wall_time=timedelta(seconds=30))

        result = run_sandbox(policy(workspace, PYTHON, "-c", self.FORK, limits=limits))

        word, count, errno_ = result.stdout.decode().split()
        assert (word, errno_) == ("stopped", "11")  # EAGAIN
        assert 20 <= int(count) < 60

    def test_the_control_a_larger_limit_lets_the_same_program_fork_them_all(
        self, workspace: Opened
    ) -> None:
        limits = SandboxLimits(pids=1000, wall_time=timedelta(seconds=30))

        result = run_sandbox(policy(workspace, PYTHON, "-c", self.FORK, limits=limits))

        assert result.stdout == b"forked 300\n"

    def test_the_hosts_own_processes_are_not_counted_against_the_sandbox(
        self, workspace: Opened
    ) -> None:
        # The limit is per user namespace, so a busy desktop does not stop a small sandbox.
        limits = SandboxLimits(pids=8)

        result = run_sandbox(policy(workspace, "/bin/sh", "-c", "echo a | cat", limits=limits))

        assert result.stdout == b"a\n"

    def test_open_files_are_limited_per_process(self, workspace: Opened) -> None:
        code = (
            "import os\n"
            "fds = []\n"
            "try:\n"
            "    for _ in range(1000):\n"
            "        fds.append(os.open('/dev/null', os.O_RDONLY))\n"
            "    print('opened', len(fds))\n"
            "except OSError as exc:\n"
            "    print('stopped', len(fds), exc.errno)\n"
        )

        result = run_sandbox(policy(workspace, PYTHON, "-c", code))

        assert result.stdout.startswith(b"stopped 25")  # 256 less the three standard streams
        assert result.stdout.endswith(b" 24\n")  # EMFILE


class TestReport:
    def test_without_a_scope_the_result_says_which_limits_are_missing(
        self, workspace: Opened
    ) -> None:
        result = run_sandbox(policy(workspace, "/bin/true"))

        assert result.limits.backend is LimitsBackend.RLIMIT
        assert result.limits.unenforced == (
            LimitKind.MEMORY_TOTAL,
            LimitKind.PIDS,
            LimitKind.CPU_RATE,
        )
        assert result.limits.detail == "the systemd scope is switched off"

    def test_the_default_runner_reports_what_this_host_can_do(self, workspace: Opened) -> None:
        runner = SandboxRunner()

        async def go() -> tuple[LimitsReport, LimitsReport]:
            result = await runner.run(policy(workspace, "/bin/true"))
            return result.limits, (await runner.limits_plan()).report()

        reported, planned = asyncio.run(go())

        assert reported == planned
        # In the container there is no systemd user manager; on a host with one the scope is used.
        # Either way a limit that is not enforced is named, never silently absent.
        if planned.backend is LimitsBackend.RLIMIT:
            assert LimitKind.PIDS in planned.unenforced
            assert planned.detail


class TestScopeWrapper:
    """The systemd path, with a stand-in for `systemd-run` (the container has no user manager)."""

    @pytest.fixture
    def fake(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSystemd:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        directory = tmp_path / "fake-bin"
        directory.mkdir()
        return FakeSystemd(directory)

    def test_the_run_is_wrapped_in_a_scope_with_the_limits_the_host_applies(
        self, workspace: Opened, fake: FakeSystemd
    ) -> None:
        fake.canned(ALL_LIMITS)
        runner = SandboxRunner(systemd_run=str(fake.path))
        limits = SandboxLimits(memory=ByteCount(300 * MIB), pids=10, cpu_percent=150)

        result = asyncio.run(runner.run(policy(workspace, "/bin/echo", "in scope", limits=limits)))

        assert result.stdout == b"in scope\n"
        assert result.limits.backend is LimitsBackend.SYSTEMD_SCOPE
        assert result.limits.unenforced == ()
        probe, run = fake.calls()
        assert any(arg.startswith("--property=MemoryMax=") for arg in probe)
        scope, _, rest = run[: run.index("--")], run[run.index("--")], run[run.index("--") + 1 :]
        assert scope == [
            "--user",
            "--scope",
            "--quiet",
            f"--property=MemoryMax={300 * MIB}",
            "--property=MemorySwapMax=0",
            "--property=TasksMax=12",
            "--property=CPUQuota=150%",
        ]
        assert rest[0].endswith("prlimit")
        assert not any(arg.startswith("--as") for arg in rest), "the scope limits memory"
        assert rest[rest.index("--") + 1].endswith("bwrap")
        assert "--unshare-all" in rest

    def test_a_default_runner_runs_the_systemd_run_it_found_on_its_path_by_that_path(
        self, workspace: Opened, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The scoped child has no PATH: it must be given the absolute path the probe used, not a
        # bare name it would look up in the default one (where there may be no systemd-run).
        monkeypatch.setenv("PATH", f"{fake.directory}:{os.environ['PATH']}")
        fake.canned(ALL_LIMITS)

        result = asyncio.run(SandboxRunner().run(policy(workspace, "/bin/echo", "scoped")))

        assert result.stdout == b"scoped\n"
        assert result.limits.backend is LimitsBackend.SYSTEMD_SCOPE
        probe, run = fake.calls()
        assert any(arg.startswith("--property=MemoryMax=") for arg in probe)
        assert "--scope" in run
        assert "/bin/echo" in run
        log = (fake.directory / "calls.log").read_text()
        assert log.count("--call--") == 2, "the probe and the run both reached the stand-in"

    def test_a_scope_without_memory_still_limits_address_space(
        self, workspace: Opened, fake: FakeSystemd
    ) -> None:
        fake.canned("pids.max 64\nmemory.max max\ncpu.max 50000 100000\n")
        runner = SandboxRunner(systemd_run=str(fake.path))

        result = asyncio.run(runner.run(policy(workspace, "/bin/true")))

        assert result.limits.backend is LimitsBackend.SYSTEMD_SCOPE
        assert result.limits.unenforced == (LimitKind.MEMORY_TOTAL,)
        assert any(arg.startswith("--as=") for arg in fake.calls()[1])

    def test_the_descriptors_reach_bwrap_through_the_wrapper(
        self, workspace: Opened, fake: FakeSystemd, tmp_path: Path
    ) -> None:
        fake.canned(ALL_LIMITS)
        granted = tmp_path / "granted"
        granted.mkdir()
        (granted / "f.txt").write_text("through systemd-run")
        with opened(granted) as handle:
            made = policy(
                workspace,
                "/bin/cat",
                f"{granted}/f.txt",
                grants=(grant(granted, GrantAccess.READ, handle),),
            )

            result = asyncio.run(SandboxRunner(systemd_run=str(fake.path)).run(made))

        assert result.stdout == b"through systemd-run"

    def test_a_scope_that_fails_to_start_is_an_error_never_an_unconfined_run(
        self, workspace: Opened, fake: FakeSystemd, workspace_dir: Path
    ) -> None:
        fake.canned(ALL_LIMITS)
        runner = SandboxRunner(systemd_run=str(fake.path))
        asyncio.run(runner.limits_plan())  # the probe passes ...
        fake.fail()  # ... and then the user manager goes away

        with pytest.raises(SandboxLaunchError, match="No medium found"):
            asyncio.run(runner.run(policy(workspace, "/bin/sh", "-c", "touch /workspace/ran")))

        assert not (workspace_dir / "ran").exists()


class TestLeaks:
    def test_the_program_dies_with_its_parent(self, workspace_dir: Path) -> None:
        mark = token()
        helper = subprocess.Popen(  # noqa: S603  (our own interpreter and module)
            [sys.executable, "-m", "tests.sandbox.helper_orphan", str(workspace_dir), mark],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert helper.stdout is not None
            assert helper.stdout.readline().startswith("ready")
            assert pids_with(mark), "the sandboxed program is not running: nothing to prove"

            helper.send_signal(signal.SIGKILL)  # no cleanup of any kind runs in the parent
            helper.wait(timeout=10)

            assert wait_until_none(mark, 5.0) == [], "the program outlived its parent"
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait(timeout=10)
            if helper.stdout is not None:
                helper.stdout.close()

    def test_cancelling_a_run_kills_the_sandbox(self, workspace: Opened) -> None:
        mark = token()
        limits = SandboxLimits(wall_time=timedelta(seconds=120), cpu_time=timedelta(seconds=120))

        async def scenario() -> None:
            run = asyncio.create_task(
                SandboxRunner(use_systemd_scope=False).run(
                    policy(workspace, "/usr/bin/sleep", f"100.{mark}", limits=limits)
                )
            )
            for _ in range(100):
                await asyncio.sleep(0.05)
                if pids_with(mark):
                    break
            assert pids_with(mark), "the sandbox did not start"
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run

        asyncio.run(scenario())

        assert wait_until_none(mark, 5.0) == []
        assert children_of_this_process() == []

    def test_no_descriptor_is_left_open_by_any_way_a_run_can_end(
        self, workspace: Opened, workspace_dir: Path
    ) -> None:
        before = open_descriptors()
        short = SandboxLimits(wall_time=timedelta(seconds=1))
        small = SandboxLimits(max_output_bytes=ByteCount(100))
        runs = [
            policy(workspace, "/bin/true"),
            policy(workspace, "/bin/sh", "-c", "exit 4"),
            policy(workspace, "/usr/bin/sleep", "30", limits=short),
            policy(workspace, "/usr/bin/yes", limits=small),
            policy(workspace, "/no/such/program"),
        ]

        for made in runs:
            with contextlib.suppress(SandboxLaunchError):  # one run is meant to fail to start
                run_sandbox(made)

        assert open_descriptors() == before
        assert children_of_this_process() == []

    def test_a_policy_that_is_refused_leaves_nothing_open(self, workspace: Opened) -> None:
        class NotOpen:
            @property
            def fd(self) -> int:
                return 900

        before = open_descriptors()
        made = policy(
            workspace, "/bin/true", grants=(grant(Path("/d"), GrantAccess.READ, NotOpen()),)
        )

        with pytest.raises(InvalidPolicyError):
            run_sandbox(made)

        assert open_descriptors() == before

    def test_concurrent_runs_do_not_disturb_each_other(self, workspace: Opened) -> None:
        async def scenario() -> list[bytes]:
            runner = SandboxRunner(use_systemd_scope=False)
            runs = [
                runner.run(policy(workspace, "/bin/sh", "-c", f"sleep 0.{n}; echo run-{n}"))
                for n in range(1, 9)
            ]
            return [result.stdout for result in await asyncio.gather(*runs)]

        outputs = asyncio.run(scenario())

        assert outputs == [f"run-{n}\n".encode() for n in range(1, 9)]
        assert children_of_this_process() == []
