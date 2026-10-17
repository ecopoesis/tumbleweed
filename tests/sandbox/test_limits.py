"""How limits are applied and reported, on every OS: command lines, probe, platform check."""

import asyncio
import dataclasses
import time
from datetime import timedelta
from pathlib import Path

import pytest

from tests.sandbox.fake_systemd import ALL_LIMITS, FakeSystemd
from tests.sandbox.support import Opened, policy
from tumbleweed.core.types import ByteCount
from tumbleweed.sandbox.errors import SandboxUnavailable, SandboxUnsupported
from tumbleweed.sandbox.limits import (
    LimitsPlan,
    parse_probe,
    prlimit_argv,
    probe_scope,
    scope_argv,
)
from tumbleweed.sandbox.policy import SandboxLimits
from tumbleweed.sandbox.result import LimitKind, LimitsBackend
from tumbleweed.sandbox.runner import REPROBE_SECONDS, SandboxRunner
from tumbleweed.sandbox.runner import _exit_code as exit_code

FULL = LimitsPlan(
    scope=True,
    scope_memory=True,
    scope_pids=True,
    scope_cpu=True,
    systemd_run="/usr/bin/systemd-run",
)
LIMITS = SandboxLimits(
    cpu_time=timedelta(seconds=7),
    memory=ByteCount(300 * 1024 * 1024),
    pids=10,
    cpu_percent=150,
    file_size=ByteCount(5 * 1024 * 1024),
)


class TestPrlimit:
    def test_soft_and_hard_limits_are_equal_so_that_cpu_overrun_is_a_kill(self) -> None:
        argv = prlimit_argv(FULL, LIMITS, prlimit="/usr/bin/prlimit")

        assert argv == [
            "/usr/bin/prlimit",
            "--cpu=7:7",
            f"--fsize={5 * 1024 * 1024}:{5 * 1024 * 1024}",
            "--core=0:0",
            "--nofile=256:256",
            "--",
        ]

    def test_address_space_is_limited_when_no_scope_limits_memory(self) -> None:
        argv = prlimit_argv(LimitsPlan(detail="no systemd"), LIMITS, prlimit="prlimit")

        assert f"--as={300 * 1024 * 1024}:{300 * 1024 * 1024}" in argv

    def test_address_space_is_limited_when_the_scope_does_not_limit_memory(self) -> None:
        plan = LimitsPlan(scope=True, scope_pids=True)

        assert "--as=314572800:314572800" in prlimit_argv(plan, LIMITS, prlimit="prlimit")

    def test_address_space_is_left_to_the_scope_when_it_limits_memory(self) -> None:
        assert not any(arg.startswith("--as") for arg in prlimit_argv(FULL, LIMITS, prlimit="p"))

    def test_the_open_file_limit_is_always_set(self) -> None:
        assert "--nofile=256:256" in prlimit_argv(LimitsPlan(), LIMITS, prlimit="prlimit")

    def test_there_is_no_process_rlimit_because_it_would_count_the_users_other_processes(
        self,
    ) -> None:
        # Measured (S14): bwrap's first fork is counted against every process of the uid in the
        # parent namespace, so a limit low enough to stop a fork bomb also stops the sandbox from
        # starting on a busy desktop. Without a scope, processes are bounded by the wall clock.
        for plan in (LimitsPlan(), FULL):
            argv = prlimit_argv(plan, LIMITS, prlimit="prlimit")
            assert not any("nproc" in arg for arg in argv)

    def test_cpu_time_is_rounded_up(self) -> None:
        limits = SandboxLimits(cpu_time=timedelta(milliseconds=1500))

        assert "--cpu=2:2" in prlimit_argv(FULL, limits, prlimit="prlimit")


class TestScope:
    def test_no_scope_means_no_wrapper(self) -> None:
        assert scope_argv(LimitsPlan(), LIMITS) == []

    def test_every_limit_the_scope_enforces_is_a_property(self) -> None:
        argv = scope_argv(FULL, LIMITS)

        assert argv == [
            "/usr/bin/systemd-run",
            "--user",
            "--scope",
            "--quiet",
            f"--property=MemoryMax={300 * 1024 * 1024}",
            "--property=MemorySwapMax=0",
            "--property=TasksMax=12",  # 10 plus bwrap and the sandbox's init
            "--property=CPUQuota=150%",
            "--",
        ]

    def test_a_limit_the_probe_did_not_see_applied_is_not_asked_for(self) -> None:
        plan = LimitsPlan(scope=True, scope_pids=True, systemd_run="/bin/systemd-run")

        argv = scope_argv(plan, LIMITS)

        assert argv[4:-1] == ["--property=TasksMax=12"]


class TestReport:
    def test_a_full_scope_enforces_everything(self) -> None:
        report = FULL.report()

        assert report.backend is LimitsBackend.SYSTEMD_SCOPE
        assert report.unenforced == ()

    def test_without_a_scope_pids_and_cpu_rate_are_reported_missing(self) -> None:
        report = LimitsPlan(detail="systemd-run is not installed").report()

        assert report.backend is LimitsBackend.RLIMIT
        assert report.unenforced == (
            LimitKind.MEMORY_TOTAL,
            LimitKind.PIDS,
            LimitKind.CPU_RATE,
        )
        assert report.detail == "systemd-run is not installed"

    def test_a_scope_that_lacks_a_controller_reports_that_limit(self) -> None:
        report = LimitsPlan(scope=True, scope_memory=True, scope_cpu=True).report()

        assert report.unenforced == (LimitKind.PIDS,)

    def test_the_report_is_what_the_probe_saw_not_what_was_asked(self) -> None:
        plan = LimitsPlan(scope=False, scope_pids=True, scope_memory=True, scope_cpu=True)

        assert plan.unenforced == (
            LimitKind.MEMORY_TOTAL,
            LimitKind.PIDS,
            LimitKind.CPU_RATE,
        )
        assert plan.address_space


class TestParseProbe:
    def test_all_three_limits_applied(self) -> None:
        assert parse_probe(ALL_LIMITS) == dataclasses.replace(FULL, systemd_run=None)

    @pytest.mark.parametrize(
        ("output", "memory", "pids", "cpu"),
        [
            ("pids.max max\nmemory.max max\ncpu.max max 100000\n", False, False, False),
            ("pids.max missing\nmemory.max missing\ncpu.max missing\n", False, False, False),
            ("pids.max 64\nmemory.max max\ncpu.max missing\n", False, True, False),
            ("pids.max max\nmemory.max 268435456\ncpu.max missing\n", True, False, False),
            ("pids.max max\nmemory.max max\ncpu.max 50000 100000\n", False, False, True),
            (
                "pids.max 9999\nmemory.max 999999999999\ncpu.max 900000 100000\n",
                False,
                False,
                False,
            ),
            ("pids.max 0\nmemory.max 0\ncpu.max 0 100000\n", False, False, False),
            ("memory.max 268435456\n", True, False, False),
            ("", False, False, False),
            ("garbage\n\x00\nmemory.max -5\n", False, False, False),
        ],
    )
    def test_only_a_limit_seen_applied_counts(
        self, output: str, memory: bool, pids: bool, cpu: bool
    ) -> None:
        plan = parse_probe(output)

        assert (plan.scope_memory, plan.scope_pids, plan.scope_cpu) == (memory, pids, cpu)

    def test_a_kernel_rounding_the_memory_limit_down_still_counts(self) -> None:
        assert parse_probe("memory.max 268431360\n").scope_memory


@pytest.fixture
def fake(tmp_path: Path) -> FakeSystemd:
    directory = tmp_path / "bin"
    directory.mkdir()
    return FakeSystemd(directory)


class TestProbe:
    def test_a_scope_that_shows_the_limits_is_used(self, fake: FakeSystemd) -> None:
        fake.canned(ALL_LIMITS)

        plan = asyncio.run(probe_scope(str(fake.path), {"XDG_RUNTIME_DIR": "/run/user/1"}))

        assert plan == dataclasses.replace(FULL, systemd_run=str(fake.path))
        call = fake.calls()[0]
        assert call[:3] == ["--user", "--scope", "--quiet"]
        assert any(arg.startswith("--property=MemoryMax=") for arg in call)

    def test_a_scope_that_applies_none_of_the_limits_is_not_used(self, fake: FakeSystemd) -> None:
        fake.canned("pids.max max\nmemory.max max\ncpu.max missing\n")

        plan = asyncio.run(probe_scope(str(fake.path), {}))

        assert not plan.scope
        assert plan.detail is not None
        assert "applies none of the limits" in plan.detail
        assert LimitKind.PIDS in plan.unenforced

    def test_a_scope_with_only_some_limits_is_used_for_those(self, fake: FakeSystemd) -> None:
        fake.canned("pids.max 64\nmemory.max max\ncpu.max missing\n")

        plan = asyncio.run(probe_scope(str(fake.path), {}))

        assert plan.scope
        assert plan.unenforced == (LimitKind.MEMORY_TOTAL, LimitKind.CPU_RATE)
        assert plan.address_space

    def test_a_failing_systemd_run_gives_a_plan_with_the_reason(self, fake: FakeSystemd) -> None:
        fake.fail()

        plan = asyncio.run(probe_scope(str(fake.path), {}))

        assert not plan.scope
        assert plan.detail is not None
        assert "failed (1)" in plan.detail
        assert "No medium found" in plan.detail

    def test_a_missing_program_gives_a_plan_with_the_reason(self, tmp_path: Path) -> None:
        plan = asyncio.run(probe_scope(str(tmp_path / "nope"), {}))

        assert not plan.scope
        assert plan.detail is not None
        assert "cannot start systemd-run" in plan.detail

    def test_a_hanging_systemd_run_is_killed_at_the_timeout(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tumbleweed.sandbox.limits._PROBE_TIMEOUT_SECONDS", 0.3)
        fake.hang()

        plan = asyncio.run(probe_scope(str(fake.path), {}))

        assert not plan.scope
        assert plan.detail is not None
        assert "did not finish within 0.3 s" in plan.detail


class TestRunnerPlan:
    def test_the_probe_runs_once_and_its_answer_is_remembered(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        fake.canned(ALL_LIMITS)
        runner = SandboxRunner(systemd_run=str(fake.path))

        async def twice() -> tuple[LimitsPlan, LimitsPlan]:
            return await runner.limits_plan(), await runner.limits_plan()

        first, second = asyncio.run(twice())

        assert first == second
        assert first.scope
        assert first.systemd_run == str(fake.path)
        assert len(fake.calls()) == 1

    def test_concurrent_first_uses_probe_once(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        fake.canned(ALL_LIMITS)
        runner = SandboxRunner(systemd_run=str(fake.path))

        async def together() -> list[LimitsPlan]:
            return list(await asyncio.gather(*(runner.limits_plan() for _ in range(5))))

        asyncio.run(together())

        assert len(fake.calls()) == 1

    def test_a_bare_name_is_found_on_path_and_used_by_its_absolute_path(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        monkeypatch.setenv("PATH", str(fake.directory))
        fake.canned(ALL_LIMITS)
        runner = SandboxRunner(systemd_run="systemd-run")

        plan = asyncio.run(runner.limits_plan())

        assert plan.scope
        assert plan.systemd_run == str(fake.path)

    def test_a_bare_name_is_never_resolved_against_the_current_directory(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.chdir(fake.directory)  # a `systemd-run` is right here, and must not be used
        fake.canned(ALL_LIMITS)
        runner = SandboxRunner(systemd_run="systemd-run")

        plan = asyncio.run(runner.limits_plan())

        assert not plan.scope
        assert plan.detail == "systemd-run is not installed"
        assert fake.calls() == []

    def test_switching_the_scope_off_never_calls_systemd(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        runner = SandboxRunner(systemd_run=str(fake.path), use_systemd_scope=False)

        plan = asyncio.run(runner.limits_plan())

        assert not plan.scope
        assert plan.detail == "the systemd scope is switched off"
        assert fake.calls() == []

    def test_no_user_session_means_no_scope_and_no_probe(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        runner = SandboxRunner(systemd_run=str(fake.path))

        plan = asyncio.run(runner.limits_plan())

        assert not plan.scope
        assert plan.detail is not None
        assert "XDG_RUNTIME_DIR" in plan.detail
        assert fake.calls() == []

    def test_a_host_without_systemd_run_gets_rlimits_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: nothing is installed

        plan = asyncio.run(SandboxRunner().limits_plan())

        assert not plan.scope
        assert plan.detail == "systemd-run is not installed"
        assert plan.report().unenforced == (
            LimitKind.MEMORY_TOTAL,
            LimitKind.PIDS,
            LimitKind.CPU_RATE,
        )


class TestPlatform:
    def test_on_macos_the_runner_raises_sandbox_unsupported(
        self, workspace: Opened, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tumbleweed.sandbox.runner._platform", lambda: "darwin")

        with pytest.raises(SandboxUnsupported, match="darwin"):
            asyncio.run(SandboxRunner().run(policy(workspace, "true")))

    def test_without_bwrap_nothing_is_run(
        self, workspace: Opened, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tumbleweed.sandbox.runner._platform", lambda: "linux")
        monkeypatch.setenv("PATH", str(tmp_path))

        with pytest.raises(SandboxUnavailable, match="bwrap"):
            asyncio.run(SandboxRunner().run(policy(workspace, "true")))

    def test_without_prlimit_nothing_is_run_unlimited(
        self, workspace: Opened, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tumbleweed.sandbox.runner._platform", lambda: "linux")
        monkeypatch.setenv("PATH", str(tmp_path))

        with pytest.raises(SandboxUnavailable, match="prlimit"):
            asyncio.run(SandboxRunner(bwrap="/bin/true").run(policy(workspace, "true")))


class TestResolvedSystemdRun:
    def test_the_probe_and_every_run_use_the_one_found_on_the_launchers_path(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The scoped child has no PATH, so a bare "systemd-run" would be looked up in the default
        # one: a different program, or none (NixOS). The plan carries the absolute path.
        monkeypatch.setenv("PATH", str(fake.directory))
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        fake.canned(ALL_LIMITS)

        plan = asyncio.run(SandboxRunner().limits_plan())

        assert plan.scope
        assert plan.systemd_run == str(fake.path)
        assert scope_argv(plan, LIMITS)[0] == str(fake.path)
        assert Path(plan.systemd_run).is_absolute()

    def test_a_relative_directory_on_the_path_still_gives_an_absolute_program(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(fake.directory.parent)
        monkeypatch.setenv("PATH", fake.directory.name)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        fake.canned(ALL_LIMITS)

        plan = asyncio.run(SandboxRunner().limits_plan())

        assert plan.systemd_run is not None
        assert Path(plan.systemd_run).is_absolute()


class TestReprobe:
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        now = [1000.0]
        monkeypatch.setattr("tumbleweed.sandbox.runner._now", lambda: now[0])
        return now

    def test_a_plan_without_a_scope_is_probed_again_after_the_interval(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        now = self.clock(monkeypatch)
        fake.fail()  # the user manager is not up yet
        runner = SandboxRunner(systemd_run=str(fake.path))

        async def scenario() -> list[bool]:
            seen = [(await runner.limits_plan()).scope]
            (fake.directory / "fail").unlink()  # it is up now
            fake.canned(ALL_LIMITS)
            now[0] += REPROBE_SECONDS - 1
            seen.append((await runner.limits_plan()).scope)  # too soon
            now[0] += 2
            seen.append((await runner.limits_plan()).scope)  # answered by the old plan
            await runner.wait_for_probe()
            seen.append((await runner.limits_plan()).scope)
            return seen

        assert asyncio.run(scenario()) == [False, False, False, True]
        assert len(fake.calls()) == 2

    def test_a_hanging_reprobe_does_not_stall_the_callers(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        now = self.clock(monkeypatch)
        fake.fail()
        runner = SandboxRunner(systemd_run=str(fake.path))

        async def scenario() -> tuple[float, bool]:
            await runner.limits_plan()
            fake.hang()  # the next probe never finishes (until its own 10 s timeout)
            now[0] += REPROBE_SECONDS + 1
            started = time.monotonic()
            plan = await runner.limits_plan()
            took = time.monotonic() - started
            await runner.aclose()  # the probe is cancelled, and its process killed
            return took, plan.scope

        took, scope = asyncio.run(scenario())

        assert took < 2.0, "the caller waited for the probe"
        assert not scope

    def test_one_reprobe_runs_at_a_time(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        now = self.clock(monkeypatch)
        fake.fail()
        runner = SandboxRunner(systemd_run=str(fake.path))

        async def scenario() -> None:
            await runner.limits_plan()
            now[0] += REPROBE_SECONDS + 1
            await asyncio.gather(*(runner.limits_plan() for _ in range(5)))
            await runner.wait_for_probe()

        asyncio.run(scenario())

        assert len(fake.calls()) == 2  # the first probe, and one re-probe for the five callers

    def test_a_plan_with_a_scope_is_never_probed_again(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        now = self.clock(monkeypatch)
        fake.canned(ALL_LIMITS)
        runner = SandboxRunner(systemd_run=str(fake.path))

        async def scenario() -> None:
            await runner.limits_plan()
            now[0] += 10 * REPROBE_SECONDS
            await runner.limits_plan()

        asyncio.run(scenario())

        assert len(fake.calls()) == 1

    def test_a_switched_off_scope_is_not_probed_for_at_all(
        self, fake: FakeSystemd, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        now = self.clock(monkeypatch)
        runner = SandboxRunner(systemd_run=str(fake.path), use_systemd_scope=False)

        async def scenario() -> None:
            await runner.limits_plan()
            now[0] += 10 * REPROBE_SECONDS
            await runner.limits_plan()

        asyncio.run(scenario())

        assert fake.calls() == []


class TestExitCode:
    @pytest.mark.parametrize(
        ("returncode", "shown"), [(0, 0), (3, 3), (255, 255), (-9, 137), (-15, 143)]
    )
    def test_a_fatal_signal_of_bwrap_itself_is_shown_as_a_shell_would(
        self, returncode: int, shown: int
    ) -> None:
        # bwrap can be killed from outside (the OOM killer): asyncio then reports -signal.
        assert exit_code(returncode) == shown
