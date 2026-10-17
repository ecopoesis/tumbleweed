"""Workspace retention: seven days after a task ends, never sooner, never while it is live (D7)."""

import dataclasses
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.fsutil import make_fifo
from tests.broker.rig import Rig, running, until
from tests.ipc.bounded import run
from tumbleweed.broker.audit import WorkspaceDeleted
from tumbleweed.broker.client import BrokerClient
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.workspaces import MAX_DEPTH, Workspaces
from tumbleweed.core.ids import TaskId

DAY = timedelta(days=1)


def replace(config: BrokerConfig, **changes: object) -> BrokerConfig:
    return dataclasses.replace(config, **changes)  # type: ignore[arg-type]


async def ended_task(rig: Rig, client: BrokerClient) -> tuple[TaskId, Path]:
    task = (await client.start_task()).task
    (rig.workspaces / str(task) / "output.txt").write_text("result")
    await client.end_task(task)
    return task, rig.workspaces / str(task)


class TestTheClock:
    def test_a_workspace_is_kept_for_seven_days_after_its_task_ended_and_then_deleted(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                _, workspace = await ended_task(rig, client)

                rig.clock.advance(7 * DAY - timedelta(seconds=1))
                early = await rig.daemon.service.sweep()
                kept = workspace.exists()
                rig.clock.advance(timedelta(seconds=2))
                late = await rig.daemon.service.sweep()

                assert (early, kept) == (0, True)
                assert late == 1
                assert not workspace.exists()
                (entry,) = rig.audit_of(WorkspaceDeleted)
                assert entry.outcome == "deleted"

        run(scenario)

    def test_it_is_counted_from_the_end_of_the_task_not_its_start(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.clock.advance(30 * DAY)  # a long task
                await client.end_task(task)

                rig.clock.advance(6 * DAY)
                assert await rig.daemon.service.sweep() == 0
                rig.clock.advance(2 * DAY)
                assert await rig.daemon.service.sweep() == 1

        run(scenario)

    def test_a_live_task_is_never_swept(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                rig.clock.advance(400 * DAY)
                swept = await rig.daemon.service.sweep()

                assert swept == 0
                assert (rig.workspaces / str(task)).is_dir()

        run(scenario)

    def test_each_workspace_is_judged_by_its_own_task(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                _, first = await ended_task(rig, client)
                rig.clock.advance(5 * DAY)
                _, second = await ended_task(rig, client)

                rig.clock.advance(3 * DAY)
                await rig.daemon.service.sweep()

                assert not first.exists()
                assert second.exists()

        run(scenario)

    def test_the_period_is_configurable(self) -> None:
        async def scenario() -> None:
            async with running(config=lambda c: replace(c, retention=timedelta(hours=1))) as rig:
                client = await rig.client()
                _, workspace = await ended_task(rig, client)

                rig.clock.advance(timedelta(hours=1, seconds=1))
                await rig.daemon.service.sweep()

                assert not workspace.exists()

        run(scenario)

    def test_a_sweep_happens_by_itself_and_at_start(self) -> None:
        async def scenario() -> None:
            async with running(
                config=lambda c: replace(c, sweep_interval=timedelta(milliseconds=50))
            ) as rig:
                client = await rig.client()
                _, workspace = await ended_task(rig, client)

                rig.clock.advance(8 * DAY)
                await until(lambda: not workspace.exists())

        run(scenario)

    def test_a_workspace_is_swept_at_the_start_of_the_next_broker(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                _, workspace = await ended_task(rig, client)
                rig.clock.advance(8 * DAY)

                await rig.restart()

                assert not workspace.exists()
                assert [e.outcome for e in rig.audit_of(WorkspaceDeleted)] == ["deleted"]

        run(scenario)


class TestWhatIsLeftInAWorkspace:
    def test_a_hostile_tree_is_deleted_without_touching_anything_it_points_at(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                outside = rig.home / "Documents"
                root = rig.workspaces / str(task)
                (root / "to-dir").symlink_to(outside)
                (root / "to-file").symlink_to(outside / "notes.txt")
                (root / "sub").mkdir()
                (root / "sub" / "to-home").symlink_to(rig.home)
                make_fifo(root / "pipe")
                locked = root / "locked"
                (locked / "inner").mkdir(parents=True)
                (locked / "inner" / "f").write_text("x")
                locked.chmod(0o000)
                await client.end_task(task)

                rig.clock.advance(8 * DAY)
                await rig.daemon.service.sweep()

                assert not root.exists()
                assert (outside / "notes.txt").read_text() == "notes"
                assert (rig.home / ".ssh").is_dir()

        run(scenario)

    def test_a_workspace_that_cannot_be_deleted_is_reported_kept_and_tried_again(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                root = rig.workspaces / str(task)
                deep = root
                directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    current = directory_fd
                    for _ in range(MAX_DEPTH + 2):
                        os.mkdir("d", dir_fd=current)
                        following = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=current)
                        if current != directory_fd:
                            os.close(current)
                        current = following
                    os.close(current)
                finally:
                    os.close(directory_fd)
                await client.end_task(task)
                rig.clock.advance(8 * DAY)

                first = await rig.daemon.service.sweep()
                second = await rig.daemon.service.sweep()

                assert (first, second) == (0, 0)
                assert deep.exists()
                failed = [e for e in rig.audit_of(WorkspaceDeleted) if e.outcome == "failed"]
                assert len(failed) == 2  # tried again, and said so again
                assert failed[0].reason == "WorkspaceError"
                assert (await rig.client()) is not None  # and the broker serves on

        run(scenario)

    def test_a_deletion_the_file_system_never_finishes_is_abandoned_not_waited_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = threading.Event()
        real = Workspaces.delete

        def hang(self: Workspaces, task: TaskId) -> str:
            release.wait(30)
            return real(self, task)

        async def scenario() -> None:
            async with running(
                config=lambda c: replace(c, delete_timeout=timedelta(milliseconds=200))
            ) as rig:
                client = await rig.client()
                _, workspace = await ended_task(rig, client)
                rig.clock.advance(8 * DAY)
                monkeypatch.setattr(Workspaces, "delete", hang)

                swept = await rig.daemon.service.sweep()
                release.set()

                assert swept == 0
                assert workspace.exists()
                (entry,) = rig.audit_of(WorkspaceDeleted)
                assert (entry.outcome, entry.reason) == ("failed", "FsTimeoutError")
                other = await rig.client()
                await other.start_task()  # the broker was never stuck

        run(scenario)

    def test_a_workspace_that_is_already_gone_is_recorded_as_missing(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, workspace = await ended_task(rig, client)
                rig_dir = workspace
                for child in rig_dir.iterdir():
                    child.unlink()
                rig_dir.rmdir()
                rig.clock.advance(8 * DAY)

                swept = await rig.daemon.service.sweep()

                assert swept == 1
                (entry,) = rig.audit_of(WorkspaceDeleted)
                assert (entry.task, entry.outcome) == (task, "missing")
                assert await rig.daemon.service.sweep() == 0

        run(scenario)
