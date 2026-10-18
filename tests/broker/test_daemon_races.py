"""Requests that overlap: the check and the record of what it implies are one step (I12, I9).

Making a workspace, opening a granted path and asking the user all wait. A decision that is made
before a wait and recorded after it is made on state that may have changed, so each of these
slows the wait down, lets another request through in the middle, and looks at what was recorded.
"""

import asyncio
import threading

import pytest

from tests.broker.rig import FakeSandbox, Rig, replace_config, running, until
from tests.ipc.bounded import run
from tumbleweed.broker.audit import GrantDecision, SandboxRun, TaskEnded, TaskEndReason
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.workspaces import Workspaces
from tumbleweed.core.ids import RunId, TaskId

COMMAND = ("/usr/bin/python3", "-c", "print(1)")


class SlowCreate:
    """Making a workspace waits (on a thread, as it does) until the test lets it go."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        real = Workspaces.create

        def slow(workspaces: Workspaces, task: TaskId) -> object:
            self.calls += 1
            made = real(workspaces, task)  # the directory exists, and nothing records it yet
            self.entered.set()
            self.release.wait(30)
            return made

        monkeypatch.setattr(Workspaces, "create", slow)

    async def until_entered(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, 10)


async def with_task(rig: Rig, client: BrokerClient) -> TaskId:
    return (await client.start_task()).task


class TestTheTaintOfTheTaskThatReplaces:
    def test_a_grant_made_while_the_workspace_is_being_made_is_inherited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The race: `start_task(previous)` read the taint of `previous`, then waited to make the
        workspace; a file grant that completed meanwhile was not in what the new task got, and
        the new task (not `fresh_context`) could then be given the network (I12)."""

        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = await with_task(rig, client)
                slow = SlowCreate(monkeypatch)
                starting = asyncio.ensure_future(client.start_task(previous=first))
                await slow.until_entered()

                rig.prompts.allow("task")
                granted = await client.request_read(
                    first, RunId.new(), str(rig.home / "Photos"), "x"
                )
                slow.release.set()
                second = await starting

                assert granted.granted
                assert second.taint == ("fs",)
                rig.prompts.allow("task")
                outcome = await client.request_network(second.task, RunId.new(), "x")
                assert not outcome.granted  # the new task is fs-tainted: no network
                assert rig.audit_of(GrantDecision)[-1].reason == "taint_conflict"

        run(scenario)

    def test_the_same_for_the_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = await with_task(rig, client)
                slow = SlowCreate(monkeypatch)
                starting = asyncio.ensure_future(client.start_task(previous=first))
                await slow.until_entered()
                rig.prompts.allow("task")
                await client.request_network(first, RunId.new(), "x")
                slow.release.set()
                second = await starting

                assert second.taint == ("net",)
                rig.prompts.allow("task")
                assert not (
                    await client.request_read(
                        second.task, RunId.new(), str(rig.home / "Photos"), "x"
                    )
                ).granted

        run(scenario)

    def test_a_read_under_a_grant_that_completes_meanwhile_counts_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not only a grant: anything that raised the taint of `previous` by the time the
        successor is recorded is inherited. Here the once grant is used by a read while the
        workspace is made; the taint was already on the task, and stays."""

        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = await with_task(rig, client)
                bound = RunId.new()
                rig.prompts.allow("once")
                await client.request_read(first, bound, str(rig.home / "Photos"), "x")
                slow = SlowCreate(monkeypatch)
                starting = asyncio.ensure_future(client.start_task(previous=first))
                await slow.until_entered()
                await client.read_file(first, bound, str(rig.home / "Photos/a.jpg"))
                slow.release.set()

                assert (await starting).taint == ("fs",)

        run(scenario)

    def test_a_previous_task_that_ended_meanwhile_still_passes_on_its_taint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = await with_task(rig, client)
                slow = SlowCreate(monkeypatch)
                starting = asyncio.ensure_future(client.start_task(previous=first))
                await slow.until_entered()
                rig.prompts.allow("task")
                await client.request_read(first, RunId.new(), str(rig.home / "Photos"), "x")
                await client.end_task(first)  # ended by its owner, in the middle of the replacement
                slow.release.set()

                second = await starting

                assert second.taint == ("fs",)
                assert [e.task for e in rig.audit_of(TaskEnded)] == [first]

        run(scenario)

    def test_two_replacements_of_one_task_cannot_both_have_replaced_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = await with_task(rig, client)
                results = await asyncio.gather(
                    client.start_task(previous=first),
                    client.start_task(previous=first),
                    return_exceptions=True,
                )

                # Both are answered; the second sees an ended task and inherits from its record.
                assert all(not isinstance(r, BaseException) for r in results)
                assert len(rig.audit_of(TaskEnded)) == 1
                assert rig.audit_of(TaskEnded)[0].reason is TaskEndReason.REPLACED

        run(scenario)


class TestLimitsHoldWhenRequestsOverlap:
    def test_tasks_started_together_cannot_all_pass_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running(config=lambda c: replace_config(c, max_live_tasks=2)) as rig:
                client = await rig.client()
                slow = SlowCreate(monkeypatch)
                calls = [asyncio.ensure_future(client.start_task()) for _ in range(6)]
                await slow.until_entered()
                slow.release.set()

                results = await asyncio.gather(*calls, return_exceptions=True)

                made = [r for r in results if not isinstance(r, BaseException)]
                refused = [r for r in results if isinstance(r, BrokerRefused)]
                assert len(made) == 2
                assert {r.reason for r in refused} == {"busy"}
                assert slow.calls == 2  # the others were refused before a directory was made
                assert len(list(rig.workspaces.iterdir())) == 2  # the others made no directory

        run(scenario)

    def test_one_run_id_is_used_by_one_run_however_many_ask_at_once(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await with_task(rig, client)
                same = RunId.new()

                results = await asyncio.gather(
                    *(client.run_in_sandbox(task, same, COMMAND) for _ in range(5)),
                    return_exceptions=True,
                )

                assert len([r for r in results if not isinstance(r, BaseException)]) == 1
                refused = [r for r in results if isinstance(r, BrokerRefused)]
                assert len(refused) == 4
                assert {r.reason for r in refused} == {"invalid_request"}
                assert len(rig.sandbox.policies) == 1
                authorised = [e for e in rig.audit_of(SandboxRun) if e.outcome == "authorised"]
                assert len(authorised) == 1
                reasons = [e.reason for e in rig.audit_of(SandboxRun) if e.outcome == "refused"]
                assert reasons == ["run_id_reused"] * 4

        run(scenario)

    def test_the_runs_of_a_task_cannot_exceed_its_cap_when_asked_together(self) -> None:
        async def scenario() -> None:
            async with running(
                config=lambda c: replace_config(c, max_runs_per_task=2, max_concurrent_runs=16)
            ) as rig:
                client = await rig.client()
                task = await with_task(rig, client)

                results = await asyncio.gather(
                    *(client.run_in_sandbox(task, RunId.new(), COMMAND) for _ in range(6)),
                    return_exceptions=True,
                )

                assert len([r for r in results if not isinstance(r, BaseException)]) == 2
                assert len(rig.sandbox.policies) == 2
                refused = [e for e in rig.audit_of(SandboxRun) if e.outcome == "refused"]
                assert [e.reason for e in refused] == ["run_cap"] * 4

        run(scenario)

    def test_a_refused_run_gives_its_id_back(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            async with running(sandbox=sandbox) as rig:
                client = await rig.client()
                task = await with_task(rig, client)
                bound = RunId.new()
                (rig.home / "Photos").rename(rig.home / "Photos.old")
                (rig.home / "Photos").symlink_to(rig.home / ".ssh")
                rig.prompts.allow("task")
                # The grant is made while the directory is as it was shown ...
                (rig.home / "Photos").unlink()
                (rig.home / "Photos.old").rename(rig.home / "Photos")
                await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")
                (rig.home / "Photos").rename(rig.home / "Photos.old")
                (rig.home / "Photos").symlink_to(rig.home / ".ssh")
                with pytest.raises(BrokerRefused):
                    await client.run_in_sandbox(task, bound, COMMAND)
                # ... and when it is put right the same ID runs.
                (rig.home / "Photos").unlink()
                (rig.home / "Photos.old").rename(rig.home / "Photos")

                await client.run_in_sandbox(task, bound, COMMAND)

                assert len(sandbox.policies) == 1

        run(scenario)


class TestATaskThatIsGoneIsGone:
    """`start_task(previous)` and a connection ending end a task; nothing may be done with it
    afterwards, not even with what it was granted (B5)."""

    async def granted_task(self, rig: Rig, client: BrokerClient) -> TaskId:
        task = await with_task(rig, client)
        rig.prompts.allow("task")
        assert (await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")).granted
        return task

    async def every_use_is_refused(self, rig: Rig, client: BrokerClient, task: TaskId) -> None:
        run_id = RunId.new()
        for attempt in (
            client.read_file(task, run_id, str(rig.home / "Photos/a.jpg")),
            client.list_dir(task, run_id, str(rig.home / "Photos")),
            client.run_in_sandbox(task, run_id, COMMAND),
            client.request_read(task, run_id, str(rig.home / "Photos"), "x"),
            client.end_task(task),
        ):
            with pytest.raises(BrokerRefused) as caught:
                await attempt
            assert caught.value.reason == "unknown_task"

    def test_a_task_replaced_by_start_task_is_unusable(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                old = await self.granted_task(rig, client)

                await client.start_task(previous=old)

                await self.every_use_is_refused(rig, client, old)
                assert rig.sandbox.policies == []
                assert rig.rows("SELECT count(*) FROM grants WHERE revoked_at IS NULL") == [(0,)]

        run(scenario)

    def test_a_task_of_a_connection_that_ended_is_unusable(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                mine, other = await rig.client(), await rig.client()
                old = await self.granted_task(rig, mine)

                await mine.close()
                await until(lambda: len(rig.audit_of(TaskEnded)) == 1)

                await self.every_use_is_refused(rig, other, old)
                assert rig.rows("SELECT count(*) FROM grants WHERE revoked_at IS NULL") == [(0,)]

        run(scenario)

    def test_a_run_in_progress_of_a_replaced_task_is_stopped(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.hang = True
            async with running(sandbox=sandbox) as rig:
                client = await rig.client()
                old = await with_task(rig, client)
                call = asyncio.ensure_future(client.run_in_sandbox(old, RunId.new(), COMMAND))
                await sandbox.started.wait()

                await client.start_task(previous=old)

                with pytest.raises(BrokerRefused) as caught:
                    await call
                assert caught.value.reason == "unknown_task"
                assert sandbox.cancelled

        run(scenario)


class TestACancelledStart:
    def test_a_start_that_is_cancelled_while_the_workspace_is_made_leaves_none_behind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                slow = SlowCreate(monkeypatch)
                starting = asyncio.ensure_future(client.start_task())
                await slow.until_entered()

                await client.close()  # the connection ends: the handler is cancelled
                slow.release.set()  # the directory is made after nobody is waiting for it

                await until(lambda: list(rig.workspaces.iterdir()) == [])
                assert rig.rows("SELECT count(*) FROM tasks") == [(0,)]
                with pytest.raises(BaseException):  # noqa: B017, PT011  (cancelled with its connection)
                    await starting

        run(scenario)

    def test_a_workspace_being_made_is_not_an_orphan_to_the_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                slow = SlowCreate(monkeypatch)
                starting = asyncio.ensure_future(client.start_task())
                await slow.until_entered()
                await rig.daemon.service._remove_orphans()  # noqa: SLF001  (the sweep, run now)
                slow.release.set()

                made = await starting

                assert (rig.workspaces / str(made.task)).is_dir()

        run(scenario)

    def test_starting_the_broker_does_not_wait_for_the_first_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orphan = TaskId.new()
        release = threading.Event()
        real = Workspaces.delete

        def plant(config: BrokerConfig) -> None:
            workspaces = Workspaces.open(config.workspaces_directory)
            workspaces.create(orphan)
            workspaces.close()

        def blocked(workspaces: Workspaces, task: TaskId) -> str:
            release.wait(30)
            return real(workspaces, task)

        async def scenario() -> None:
            monkeypatch.setattr(Workspaces, "delete", blocked)
            async with asyncio.timeout(10):
                async with running(before_start=plant) as rig:
                    client = await rig.client()  # served while the orphan is still being removed
                    await client.start_task()
                    release.set()
                    await until(lambda: not (rig.workspaces / str(orphan)).exists())

        try:
            run(scenario)
        finally:
            release.set()
