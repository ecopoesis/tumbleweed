"""A task that has used many grants can still be ended, replaced and cleaned up (I9, I7).

Audit entries hold at most so many grant IDs, so nothing that ends a task may depend on how many
it held: used-up once grants are dropped when they are used up, and whatever a task holds is
bounded, and an entry that lists IDs lists the first of them and says how many there were.
"""

import asyncio
import sqlite3
from datetime import timedelta

import pytest

from tests.broker.rig import START, FakeSandbox, Rig, replace_config, running, until
from tests.broker.test_audit_consistency import commit_that_fails
from tests.ipc.bounded import run
from tumbleweed.broker.audit import (
    MAX_AUDITED_IDS,
    MAX_ROLLED_BACK_KINDS,
    GrantRevoked,
    RolledBack,
    TaskEnded,
    TaskEndReason,
    TaskStarted,
    read_entries,
)
from tumbleweed.broker.capabilities import NetFull
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.errors import StoreError, WorkspaceError
from tumbleweed.broker.grants import GrantOrigin, OnceGrant
from tumbleweed.broker.service import BrokerService, _retry_after
from tumbleweed.broker.store import BrokerStore, TaskRecord
from tumbleweed.broker.workspaces import Workspaces
from tumbleweed.core.ids import GrantId, RunId, TaskId
from tumbleweed.core.types import WorkspaceRelPath

MANY = MAX_AUDITED_IDS + 6
COMMAND = ("/usr/bin/python3", "-c", "print(1)")
DAY = timedelta(days=1)


async def use_up_once_grants(rig: Rig, client: BrokerClient, task: TaskId, count: int) -> None:
    for number in range(count):
        directory = rig.home / f"once{number}"
        directory.mkdir(exist_ok=True)
        run_id = RunId.new()
        rig.prompts.allow("once")
        assert (await client.request_read(task, run_id, str(directory), "x")).granted
        await client.list_dir(task, run_id, str(directory))  # uses it up


def valid_log(rig: Rig) -> list[object]:
    return list(read_entries(rig.audit_path))  # every line parses as an entry


class TestAfterManyUsedUpGrants:
    def test_the_task_can_be_ended(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                await use_up_once_grants(rig, client, task, MANY)

                ended = await client.end_task(task)

                assert ended.revoked_grants == 0  # nothing was held: all were used up
                assert valid_log(rig)
                assert rig.rows("SELECT count(*) FROM grants WHERE revoked_at IS NULL") == [(0,)]

        run(scenario)

    def test_the_same_when_the_grants_were_used_up_by_runs(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                for number in range(MANY):
                    directory = rig.home / f"once{number}"
                    directory.mkdir(exist_ok=True)
                    run_id = RunId.new()
                    rig.prompts.allow("once")
                    assert (await client.request_read(task, run_id, str(directory), "x")).granted
                    await client.run_in_sandbox(task, run_id, COMMAND)  # uses it up

                await client.end_task(task)

                assert valid_log(rig)
                assert rig.rows("SELECT count(*) FROM grants WHERE revoked_at IS NULL") == [(0,)]

        run(scenario)

    def test_the_task_can_be_replaced(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                await use_up_once_grants(rig, client, task, MANY)

                second = await client.start_task(previous=task)

                assert second.taint == ("fs",)
                assert valid_log(rig)

        run(scenario)

    def test_a_connection_that_ends_ends_the_task_and_stops_its_run(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            async with running(sandbox=sandbox) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                await use_up_once_grants(rig, client, task, MANY)
                sandbox.hang = True
                call = asyncio.ensure_future(client.run_in_sandbox(task, RunId.new(), COMMAND))
                await sandbox.started.wait()

                await client.close()
                await until(lambda: sandbox.cancelled)
                await until(lambda: any(isinstance(e, TaskEnded) for e in rig.audit()))

                assert valid_log(rig)
                (ended,) = rig.audit_of(TaskEnded)
                assert ended.reason is TaskEndReason.CONNECTION_CLOSED
                assert rig.rows("SELECT state FROM tasks") == [("ended",)]
                await asyncio.gather(call, return_exceptions=True)

        run(scenario)

    def test_the_grants_a_task_holds_are_bounded_and_the_audit_says_how_many_were_revoked(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                for number in range(MAX_AUDITED_IDS):
                    (rig.home / f"d{number}").mkdir()
                    rig.prompts.allow("task")
                    assert (
                        await client.request_read(
                            task, RunId.new(), str(rig.home / f"d{number}"), "x"
                        )
                    ).granted

                ended = await client.end_task(task)

                assert ended.revoked_grants == MAX_AUDITED_IDS
                (entry,) = rig.audit_of(TaskEnded)
                assert len(entry.revoked) == MAX_AUDITED_IDS
                assert entry.revoked_count == MAX_AUDITED_IDS
                assert len(rig.audit_of(GrantRevoked)) == MAX_AUDITED_IDS

        run(scenario)


class TestAConnectionThatEndsEndsItsTasksWhateverGoesWrong:
    def test_a_task_that_cannot_be_ended_on_record_is_ended_in_memory_and_its_run_stopped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.hang = True
            async with running(sandbox=sandbox) as rig:
                client, other = await rig.client(), await rig.client()
                task = (await client.start_task()).task
                call = asyncio.ensure_future(client.run_in_sandbox(task, RunId.new(), COMMAND))
                await sandbox.started.wait()

                def broken(self: BrokerStore, task: TaskId, at: object) -> None:
                    raise RuntimeError("the database is damaged")

                monkeypatch.setattr(BrokerStore, "end_task", broken)
                await client.close()
                await until(lambda: sandbox.cancelled)  # it ended in memory all the same
                monkeypatch.undo()

                # Another connection's refusal would say the same, so look at the task itself:
                assert task not in rig.daemon.service._live  # noqa: SLF001  (is it still live?)
                with pytest.raises(BrokerRefused):
                    await other.read_workspace_file(task, WorkspaceRelPath("x"))
                assert rig.rows("SELECT state FROM tasks") == [("live",)]  # until the next start
                await asyncio.gather(call, return_exceptions=True)

        run(scenario)


class TestWhatAStoppedBrokerLeft:
    def test_a_task_left_live_with_more_grants_than_an_entry_holds_is_ended_at_start(self) -> None:
        task = TaskId.new()
        ids = [GrantId.new() for _ in range(MANY)]

        def leave_live(config: BrokerConfig) -> None:
            store = BrokerStore.open(config.broker_directory)
            workspaces = Workspaces.open(config.workspaces_directory)
            identity = workspaces.create(task)
            with store.transaction():
                store.insert_task(
                    TaskRecord(
                        task=task,
                        taint=frozenset(),
                        created_at=START,
                        ended_at=None,
                        workspace_present=True,
                        workspace=identity,
                    )
                )
                for grant in ids:  # unrevoked, as an older or a damaged file may hold them
                    store.insert_grant(
                        OnceGrant(
                            id=grant,
                            capability=NetFull(),
                            origin=GrantOrigin.PROMPT,
                            issued_at=START,
                            task=task,
                            run=RunId.new(),
                        ),
                        task,
                    )
            workspaces.close()
            store.close()

        async def scenario() -> None:
            async with running(before_start=leave_live) as rig:  # it starts
                (ended,) = rig.audit_of(TaskEnded)
                assert ended.reason is TaskEndReason.STARTUP
                assert ended.revoked_count == MANY
                assert len(ended.revoked) == MAX_AUDITED_IDS
                assert len(rig.audit_of(GrantRevoked)) == MANY
                assert rig.rows("SELECT count(*) FROM grants WHERE revoked_at IS NULL") == [(0,)]
                assert rig.rows("SELECT state FROM tasks") == [("ended",)]
                assert valid_log(rig)
                client = await rig.client()
                await client.start_task()  # and serves

        run(scenario)

    def test_a_task_that_cannot_be_ended_on_record_does_not_stop_the_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = TaskId.new()

        def leave_live(config: BrokerConfig) -> None:
            store = BrokerStore.open(config.broker_directory)
            workspaces = Workspaces.open(config.workspaces_directory)
            identity = workspaces.create(task)
            with store.transaction():
                store.insert_task(
                    TaskRecord(
                        task=task,
                        taint=frozenset(),
                        created_at=START,
                        ended_at=None,
                        workspace_present=True,
                        workspace=identity,
                    )
                )
            workspaces.close()
            store.close()

        async def scenario() -> None:
            def broken(self: BrokerStore, task: TaskId, at: object) -> None:
                raise RuntimeError("the database is damaged")

            monkeypatch.setattr(BrokerStore, "end_task", broken)
            async with running(before_start=leave_live) as rig:
                client = await rig.client()
                monkeypatch.undo()
                await client.start_task()  # it serves, and the stale task is not live in memory
                with pytest.raises(BrokerRefused):
                    await client.end_task(task)

        run(scenario)


class TestTheLimitOfARolledBackEntry:
    @pytest.mark.parametrize(
        "count",
        [1, MAX_ROLLED_BACK_KINDS - 1, MAX_ROLLED_BACK_KINDS, MAX_ROLLED_BACK_KINDS + 1, 300],
    )
    def test_however_many_entries_were_written_the_note_can_be_made(
        self, monkeypatch: pytest.MonkeyPatch, count: int
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                service = rig.daemon.service

                def act() -> None:
                    with service._atomic() as entries:  # noqa: SLF001  (the transaction is the subject)
                        for _ in range(count):
                            entries.append(
                                TaskStarted(
                                    at=rig.clock.now,
                                    task=TaskId.new(),
                                    previous=None,
                                    fresh_context=True,
                                    taint=(),
                                )
                            )

                with commit_that_fails(monkeypatch), pytest.raises(StoreError, match="commit"):
                    act()
                monkeypatch.undo()

                note = rig.audit()[-1]
                assert isinstance(note, RolledBack)
                assert note.count == count
                assert len(note.entries) == min(count, MAX_ROLLED_BACK_KINDS)
                assert set(note.entries) == {"task_started"}

        run(scenario)


class TestRetentionBacksOff:
    def test_the_pause_doubles_from_an_hour_up_to_a_day(self) -> None:
        hours = [_retry_after(failures) / timedelta(hours=1) for failures in range(8)]

        assert hours == [1, 2, 4, 8, 16, 24, 24, 24]
        assert _retry_after(-5) == timedelta(hours=1)  # a damaged count does not crash it
        assert _retry_after(10**9) == DAY

    def test_the_second_retry_waits_two_hours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def stuck(self: Workspaces, task: TaskId) -> str:
            raise WorkspaceError("cannot empty a workspace")

        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                await client.end_task(task)
                monkeypatch.setattr(Workspaces, "delete", stuck)
                rig.clock.advance(8 * DAY)

                def failures() -> int:
                    return len([e for e in rig.audit() if getattr(e, "outcome", "") == "failed"])

                await rig.daemon.service.sweep()
                assert failures() == 1  # tried; the next try is in an hour
                rig.clock.advance(timedelta(minutes=59))
                await rig.daemon.service.sweep()
                assert failures() == 1
                rig.clock.advance(timedelta(minutes=2))
                await rig.daemon.service.sweep()
                assert failures() == 2  # the second failure: the next try is in two hours
                rig.clock.advance(timedelta(hours=1, minutes=59))
                await rig.daemon.service.sweep()
                assert failures() == 2
                rig.clock.advance(timedelta(minutes=2))
                await rig.daemon.service.sweep()
                assert failures() == 3

        run(scenario)


class TestADamagedRowDoesNotStopRetention:
    def test_a_count_that_cannot_be_real_skips_the_row_and_the_sweep_goes_on(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                damaged, fine = (await client.start_task()).task, (await client.start_task()).task
                await client.end_task(damaged)
                await client.end_task(fine)
                database = sqlite3.connect(rig.config.broker_directory / "broker.sqlite3")
                database.execute("PRAGMA ignore_check_constraints = ON")
                database.execute(
                    "UPDATE tasks SET delete_failures = -1 WHERE id = ?", (str(damaged),)
                )
                database.commit()
                database.close()
                rig.clock.advance(8 * DAY)

                swept = await rig.daemon.service.sweep()  # does not raise

                assert swept == 1
                assert (rig.workspaces / str(damaged)).exists()  # the damaged row is left alone
                assert not (rig.workspaces / str(fine)).exists()

        run(scenario)

    def test_a_sweep_that_raises_anything_is_logged_and_the_next_one_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = [0]
        real = Workspaces.names

        def failing(self: Workspaces) -> list[str]:
            calls[0] += 1
            if calls[0] == 1:
                raise WorkspaceError("cannot list the workspaces directory")
            return real(self)

        async def scenario() -> None:
            monkeypatch.setattr(Workspaces, "names", failing)
            async with running(
                config=lambda c: replace_config(c, sweep_interval=timedelta(milliseconds=50))
            ) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                await client.end_task(task)
                rig.clock.advance(8 * DAY)

                # The first pass failed (listing the directory); retention still happens.
                await until(lambda: not (rig.workspaces / str(task)).exists())

        run(scenario)

    def test_an_error_nobody_expected_does_not_end_the_sweep_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = BrokerService.sweep
        calls = [0]

        async def boom(self: BrokerService) -> int:
            calls[0] += 1
            if calls[0] == 1:
                raise ValueError("a bug")
            return await real(self)

        async def scenario() -> None:
            monkeypatch.setattr(BrokerService, "sweep", boom)
            async with running(
                config=lambda c: replace_config(c, sweep_interval=timedelta(milliseconds=50))
            ):
                await until(lambda: calls[0] >= 3)

        run(scenario)
