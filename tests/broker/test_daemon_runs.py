"""`run_in_sandbox` over IPC with a fake sandbox: what the broker composes, and when it refuses."""

import asyncio
import base64
import dataclasses
from datetime import timedelta

import pytest

from tests.broker.rig import FakeSandbox, Rig, canned_result, running
from tests.ipc.bounded import run
from tumbleweed.broker.audit import GrantDecision, SandboxRun, SandboxRunResult
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.ipc.broker_messages import EnvPair
from tumbleweed.sandbox.errors import (
    InvalidPolicyError,
    SandboxLaunchError,
    SandboxTeardownError,
    SandboxUnavailable,
    SandboxUnsupported,
)
from tumbleweed.sandbox.limits import LimitsPlan
from tumbleweed.sandbox.policy import GrantAccess
from tumbleweed.sandbox.result import LandlockState, LimitKind

COMMAND = ("/usr/bin/python3", "-c", "print(1)")


def replace(config: BrokerConfig, **changes: object) -> BrokerConfig:
    return dataclasses.replace(config, **changes)  # type: ignore[arg-type]


async def with_grants(
    rig: Rig,
    client: BrokerClient,
    *relatives: str,
    scope: str = "task",
    run_id: RunId | None = None,
) -> TaskId:
    task = (await client.start_task()).task
    for relative in relatives:
        rig.prompts.allow(scope)
        outcome = await client.request_read(
            task, run_id or RunId.new(), str(rig.home / relative), "x"
        )
        assert outcome.granted, outcome.message
    return task


class TestWhatTheBrokerComposes:
    def test_the_workspace_and_the_granted_paths_are_mounted_by_descriptor_read_only(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await with_grants(rig, client, "Photos", "Documents/notes.txt")

                result = await client.run_in_sandbox(task, RunId.new(), COMMAND)

                (policy,) = rig.sandbox.policies
                assert policy.command == COMMAND
                assert {(str(g.target), g.access) for g in policy.grants} == {
                    (str(rig.home / "Photos"), GrantAccess.READ),
                    (str(rig.home / "Documents" / "notes.txt"), GrantAccess.READ),
                }
                assert result.mounted_grants == 2
                assert policy.runtime == rig.config.runtime
                # The descriptors were the very directories and files, and are closed after.
                workspace, grants = rig.sandbox.inodes[0]
                assert workspace == (rig.workspaces / str(task)).stat().st_ino
                assert set(grants) == {
                    (rig.home / "Photos").stat().st_ino,
                    (rig.home / "Documents" / "notes.txt").stat().st_ino,
                }
                with pytest.raises(ValueError, match="closed"):
                    _ = policy.workspace.fd

        run(scenario)

    def test_nested_grants_are_mounted_once_as_the_outer_one(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                # Grants of a file and a directory inside a directory that is granted later.
                for relative in ("Photos/2026", "Photos/a.jpg", "Photos"):
                    rig.prompts.allow("task")
                    await client.request_read(task, RunId.new(), str(rig.home / relative), "x")

                result = await client.run_in_sandbox(task, RunId.new(), COMMAND)

                (policy,) = rig.sandbox.policies
                assert [str(g.target) for g in policy.grants] == [str(rig.home / "Photos")]
                assert result.mounted_grants == 1
                (authorised,) = rig.audit_of(SandboxRun)
                assert len(authorised.mounted) == 1

        run(scenario)

    def test_a_run_with_no_grants_sees_only_its_workspace(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                result = await client.run_in_sandbox(task, RunId.new(), COMMAND)

                (policy,) = rig.sandbox.policies
                assert policy.grants == ()
                assert result.mounted_grants == 0

        run(scenario)

    def test_the_environment_is_the_brokers_and_a_request_may_set_only_the_locale(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                await client.run_in_sandbox(
                    task, RunId.new(), COMMAND, env=(EnvPair(name="LANG", value="de_DE.UTF-8"),)
                )

                (policy,) = rig.sandbox.policies
                assert {str(v.name): v.value for v in policy.env} == {
                    "PATH": "/usr/bin:/bin",
                    "HOME": "/workspace",
                    "TMPDIR": "/tmp",
                    "LANG": "de_DE.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                assert {str(n) for n in policy.env_allowlist} == {str(v.name) for v in policy.env}

        run(scenario)

    @pytest.mark.parametrize(
        "name", ["ANTHROPIC_API_KEY", "LD_PRELOAD", "PATH", "HOME", "SSH_AUTH_SOCK", "PYTHONPATH"]
    )
    def test_any_other_variable_is_refused_and_nothing_runs(self, name: str) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(
                        task, RunId.new(), COMMAND, env=(EnvPair(name=name, value="x"),)
                    )

                assert caught.value.reason == "invalid_request"
                assert rig.sandbox.policies == []
                assert rig.audit_of(SandboxRun) == []

        run(scenario)

    def test_the_limits_are_the_requests_within_the_brokers_bounds(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                await client.run_in_sandbox(
                    task,
                    RunId.new(),
                    COMMAND,
                    wall_seconds=7,
                    cpu_seconds=5,
                    max_output_bytes=1234,
                )

                (policy,) = rig.sandbox.policies
                assert policy.limits.wall_time == timedelta(seconds=7)
                assert policy.limits.cpu_time == timedelta(seconds=5)
                assert int(policy.limits.max_output_bytes) == 1234
                assert policy.limits.memory == rig.config.limits.memory

        run(scenario)

    @pytest.mark.parametrize(
        ("wall", "output"), [(121, 1024), (0, 1024), (30, 256 * 1024 + 1), (30, 0)]
    )
    def test_a_limit_over_the_brokers_ceiling_never_leaves_the_client(
        self, wall: int, output: int
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                with pytest.raises(ValueError, match="validation error"):
                    await client.run_in_sandbox(
                        task, RunId.new(), COMMAND, wall_seconds=wall, max_output_bytes=output
                    )

        run(scenario)

    def test_landlock_is_required_by_the_daemon_never_by_a_request(self) -> None:
        async def scenario() -> None:
            async with running(config=lambda c: replace(c, require_landlock=True)) as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                (policy,) = rig.sandbox.policies
                assert policy.require_landlock is True

        run(scenario)

    def test_the_relaxed_setting_is_recorded_for_every_run_and_at_the_start(self) -> None:
        async def scenario() -> None:
            async with running() as rig:  # the rig runs a development broker
                client = await rig.client()
                task = (await client.start_task()).task

                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                (policy,) = rig.sandbox.policies
                assert policy.require_landlock is False
                (authorised,) = rig.audit_of(SandboxRun)
                assert authorised.require_landlock is False

        run(scenario)

    def test_what_the_launcher_reported_comes_back(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.result = canned_result(
                stdout=b"hello\x00world",
                exit_code=3,
                unenforced=(LimitKind.PIDS, LimitKind.CPU_RATE),
                landlock=LandlockState.UNAVAILABLE,
            )
            async with running(sandbox=sandbox) as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                result = await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert base64.b64decode(result.stdout) == b"hello\x00world"
                assert (result.exit_code, result.termination) == (3, "exited")
                assert result.unenforced == ("pids", "cpu_rate")
                assert result.landlock == "unavailable"

        run(scenario)


class TestTheLargestAnswerFitsAFrame:
    def test_the_most_output_a_request_may_ask_for_on_both_streams(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.result = canned_result(stdout=bytes(range(256)) * 1024, stderr=b"\xff" * 262144)
            async with running(sandbox=sandbox) as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                result = await client.run_in_sandbox(
                    task, RunId.new(), COMMAND, max_output_bytes=256 * 1024
                )

                assert base64.b64decode(result.stdout) == bytes(range(256)) * 1024
                assert base64.b64decode(result.stderr) == b"\xff" * 262144

        run(scenario)


class TestOnceGrants:
    def test_a_once_grant_is_mounted_in_its_run_and_used_up_by_it(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                bound = RunId.new()
                task = await with_grants(rig, client, "Photos", scope="once", run_id=bound)

                await client.run_in_sandbox(task, bound, COMMAND)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                first, second = rig.sandbox.policies
                assert [str(g.target) for g in first.grants] == [str(rig.home / "Photos")]
                assert second.grants == ()
                authorised = rig.audit_of(SandboxRun)
                assert [len(a.consumed) for a in authorised] == [1, 0]

        run(scenario)

    def test_a_once_grant_of_another_run_is_not_mounted(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await with_grants(rig, client, "Photos", scope="once")

                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                (policy,) = rig.sandbox.policies
                assert policy.grants == ()

        run(scenario)

    def test_a_run_id_cannot_be_used_twice(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                used = RunId.new()
                await client.run_in_sandbox(task, used, COMMAND)

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, used, COMMAND)

                assert caught.value.reason == "invalid_request"
                assert len(rig.sandbox.policies) == 1

        run(scenario)


class TestTaint:
    def test_a_run_never_raises_the_taint_of_a_task_beyond_its_grants(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await with_grants(rig, client, "Photos")

                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert (await client.start_task(previous=task)).taint == ("fs",)

        run(scenario)


class TestTheHostMustBeAbleToEnforceTheLimits:
    def test_on_the_target_a_host_without_a_pids_limit_is_refused_before_anything_runs(
        self,
    ) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.plan = LimitsPlan(detail="no user manager")
            async with running(sandbox=sandbox, require_scope_limits=True) as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert caught.value.reason == "unavailable"
                assert "process and memory limits" in caught.value.message
                assert sandbox.policies == []
                (refused,) = rig.audit_of(SandboxRun)
                assert (refused.outcome, refused.reason) == ("refused", "limits_unenforced")

        run(scenario)

    @pytest.mark.parametrize("missing", [LimitKind.PIDS, LimitKind.MEMORY_TOTAL])
    def test_either_missing_limit_is_enough(self, missing: LimitKind) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.plan = LimitsPlan(
                scope=True,
                scope_memory=missing is not LimitKind.MEMORY_TOTAL,
                scope_pids=missing is not LimitKind.PIDS,
                scope_cpu=True,
                systemd_run="/x",
            )
            async with running(sandbox=sandbox, require_scope_limits=True) as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert caught.value.reason == "unavailable"
                assert sandbox.policies == []

        run(scenario)

    def test_a_check_of_the_host_that_never_finishes_is_a_timeout_and_nothing_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tumbleweed.broker.service._PROBE_WAIT", timedelta(milliseconds=200))

        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.hang_plan = True
            async with running(sandbox=sandbox, require_scope_limits=True) as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert caught.value.reason == "timeout"
                assert sandbox.policies == []
                (refused,) = rig.audit_of(SandboxRun)
                assert refused.reason == "limits_probe_timeout"

        run(scenario)

    def test_a_host_that_enforces_both_runs_even_without_a_cpu_quota(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.plan = LimitsPlan(
                scope=True, scope_memory=True, scope_pids=True, scope_cpu=False, systemd_run="/x"
            )
            async with running(sandbox=sandbox, require_scope_limits=True) as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert len(sandbox.policies) == 1

        run(scenario)

    def test_a_development_host_runs_and_every_result_says_what_was_not_enforced(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.plan = LimitsPlan(detail="no user manager")
            sandbox.result = canned_result(
                unenforced=(LimitKind.MEMORY_TOTAL, LimitKind.PIDS, LimitKind.CPU_RATE)
            )
            async with running(sandbox=sandbox, require_scope_limits=False) as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                result = await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert result.unenforced == ("memory_total", "pids", "cpu_rate")
                (ended,) = rig.audit_of(SandboxRunResult)
                assert ended.unenforced == ("memory_total", "pids", "cpu_rate")
                assert sandbox.plans_asked == 0  # not asked for when it is not required

        run(scenario)


class TestWhenTheGrantsAreNoLongerWhatWasGranted:
    def test_a_granted_directory_that_became_a_symlink_refuses_the_run(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await with_grants(rig, client, "Photos")
                (rig.home / "Photos").rename(rig.home / "Photos.old")
                (rig.home / "Photos").symlink_to(rig.home / ".ssh")

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert caught.value.reason == "unsafe"
                assert rig.sandbox.policies == []
                assert rig.audit_of(SandboxRun) == []  # not authorised, so not recorded as such

        run(scenario)

    def test_a_granted_directory_that_was_replaced_refuses_the_run(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await with_grants(rig, client, "Photos")
                (rig.home / "Photos").rename(rig.home / "Photos.old")
                (rig.home / "Photos").mkdir()

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert caught.value.reason == "unsafe"
                assert rig.sandbox.policies == []

        run(scenario)

    def test_a_granted_directory_that_was_removed_refuses_the_run(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await with_grants(rig, client, "Photos/2026")
                (rig.home / "Photos" / "2026").rmdir()

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert caught.value.reason == "not_found"
                assert rig.sandbox.policies == []

        run(scenario)

    def test_a_refused_run_does_not_consume_a_once_grant(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                bound = RunId.new()
                task = await with_grants(rig, client, "Photos", scope="once", run_id=bound)
                (rig.home / "Photos").rename(rig.home / "Photos.old")
                (rig.home / "Photos").mkdir()
                with pytest.raises(BrokerRefused):
                    await client.run_in_sandbox(task, bound, COMMAND)
                (rig.home / "Photos").rmdir()
                (rig.home / "Photos.old").rename(rig.home / "Photos")

                await client.run_in_sandbox(task, bound, COMMAND)

                (policy,) = rig.sandbox.policies
                assert len(policy.grants) == 1

        run(scenario)


class TestBounds:
    def test_runs_at_once_are_bounded_and_the_excess_is_refused_not_queued(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.hang = True
            async with running(
                sandbox=sandbox, config=lambda c: replace(c, max_concurrent_runs=1)
            ) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                first = asyncio.ensure_future(client.run_in_sandbox(task, RunId.new(), COMMAND))
                await sandbox.started.wait()

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)

                assert caught.value.reason == "busy"

        run(scenario)

    def test_the_runs_of_one_task_are_bounded(self) -> None:
        async def scenario() -> None:
            async with running(config=lambda c: replace(c, max_runs_per_task=2)) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert caught.value.reason == "busy"

        run(scenario)

    def test_a_sandbox_that_outlives_its_deadline_is_cancelled_and_reported(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.hang = True
            async with running(
                sandbox=sandbox, config=lambda c: replace(c, run_grace=timedelta(milliseconds=1))
            ) as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND, wall_seconds=1)

                assert caught.value.reason == "timeout"
                assert sandbox.cancelled
                (ended,) = rig.audit_of(SandboxRunResult)
                assert (ended.outcome, ended.reason) == ("failed", "timeout")

        run(scenario)


class TestWhenTheSandboxFails:
    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (SandboxUnavailable("bwrap secret path"), "unavailable"),
            (SandboxUnsupported("this is macOS"), "unavailable"),
            (SandboxLaunchError("bwrap: Can't find source path /secret"), "failed"),
            (SandboxTeardownError("pid 4"), "failed"),
            (InvalidPolicyError("a grant over /secret"), "invalid_request"),
        ],
    )
    def test_each_failure_is_a_refusal_that_repeats_nothing_of_its_message(
        self, error: Exception, reason: str
    ) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.fail_with = error
            async with running(sandbox=sandbox) as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert caught.value.reason == reason
                assert "secret" not in str(caught.value)
                (authorised,) = rig.audit_of(SandboxRun)
                assert authorised.outcome == "authorised"  # it was, and then it failed
                (ended,) = rig.audit_of(SandboxRunResult)
                assert ended.outcome == "failed"

        run(scenario)


class TestStoppingARun:
    def test_ending_the_task_stops_its_run_and_answers_it(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.hang = True
            async with running(sandbox=sandbox) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                running_call = asyncio.ensure_future(
                    client.run_in_sandbox(task, RunId.new(), COMMAND)
                )
                await sandbox.started.wait()

                await client.end_task(task)

                with pytest.raises(BrokerRefused) as caught:
                    await running_call
                assert caught.value.reason == "unknown_task"
                assert sandbox.cancelled
                (ended,) = rig.audit_of(SandboxRunResult)
                assert ended.outcome == "cancelled"

        run(scenario)

    def test_a_connection_that_ends_stops_its_runs(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.hang = True
            async with running(sandbox=sandbox) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                running_call = asyncio.ensure_future(
                    client.run_in_sandbox(task, RunId.new(), COMMAND)
                )
                await sandbox.started.wait()

                await client.close()
                await asyncio.gather(running_call, return_exceptions=True)
                async with asyncio.timeout(10):
                    while not sandbox.cancelled:  # noqa: ASYNC110  (bounded; the server's side)
                        await asyncio.sleep(0.01)

                assert sandbox.cancelled

        run(scenario)


class TestNetworkGrants:
    def test_a_network_grant_is_not_mounted_and_does_not_stop_the_run(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.prompts.allow("task")
                granted = await client.request_network(task, RunId.new(), "x")
                assert granted.granted

                result = await client.run_in_sandbox(task, RunId.new(), COMMAND)

                (policy,) = rig.sandbox.policies
                assert policy.grants == ()  # S22 gives a sandbox its network
                assert result.mounted_grants == 0
                assert [d.outcome for d in rig.audit_of(GrantDecision)] == ["granted"]

        run(scenario)
