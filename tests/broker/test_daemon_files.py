"""Reads and listings over IPC: under a grant, and in the task's own workspace (I11)."""

import asyncio
import base64
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.broker.fsutil import make_fifo
from tests.broker.rig import Rig, running
from tests.ipc.bounded import run
from tumbleweed.broker.audit import FileAccess
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.deny import DenyRules
from tumbleweed.broker.fdio import OpenedPath
from tumbleweed.broker.paths import StoredPath
from tumbleweed.broker.policy import open_granted
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.core.types import WorkspaceRelPath

PHOTO = "Photos/a.jpg"


async def granted_task(rig: Rig, client: BrokerClient, relative: str = "Photos") -> TaskId:
    task = (await client.start_task()).task
    rig.prompts.allow("task")
    outcome = await client.request_read(task, RunId.new(), str(rig.home / relative), "x")
    assert outcome.granted
    return task


def decoded(data: str) -> bytes:
    return base64.b64decode(data, validate=True)


class TestReadingUnderAGrant:
    def test_a_file_in_a_granted_directory_is_read(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)

                content = await client.read_file(task, RunId.new(), str(rig.home / PHOTO))

                assert decoded(content.data) == b"jpeg"
                assert content.size == 4

        run(scenario)

    def test_a_file_that_is_itself_the_grant_is_read(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client, "Documents/notes.txt")

                content = await client.read_file(
                    task, RunId.new(), str(rig.home / "Documents" / "notes.txt")
                )

                assert decoded(content.data) == b"notes"

        run(scenario)

    def test_every_read_is_audited_with_the_grant_that_allowed_it(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)

                await client.read_file(task, RunId.new(), str(rig.home / PHOTO))

                (entry,) = rig.audit_of(FileAccess)
                assert entry.outcome == "allowed"
                assert entry.operation == "read_file"
                assert entry.path == str(rig.home / PHOTO)
                assert entry.grant is not None
                assert not entry.consumed

        run(scenario)

    def test_outside_every_grant_nothing_is_read_and_the_refusal_is_audited(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(
                        task, RunId.new(), str(rig.home / "Documents" / "notes.txt")
                    )

                assert caught.value.reason == "not_granted"
                (entry,) = rig.audit_of(FileAccess)
                assert (entry.outcome, entry.reason, entry.grant) == (
                    "refused",
                    "not_granted",
                    None,
                )
                assert len(rig.prompts.asked) == 1  # reading never asks: request access first

        run(scenario)

    @pytest.mark.parametrize("relative", [".ssh/id_rsa", ".netrc", ".gnupg"])
    def test_protected_files_are_refused_even_with_a_grant_over_the_home(
        self, relative: str
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                # The home is not grantable, so grant what is next to it and try to reach across.
                rig.prompts.allow("task")
                await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), str(rig.home / relative))

                assert caught.value.reason in {"denied", "not_granted"}

        run(scenario)

    def test_a_file_the_granted_directory_does_not_contain_is_not_found(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), str(rig.home / "Photos/missing"))

                assert caught.value.reason == "not_found"

        run(scenario)

    def test_a_directory_is_not_a_file(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), str(rig.home / "Photos/2026"))

                assert caught.value.reason == "invalid_request"

        run(scenario)

    def test_a_file_over_the_limit_is_refused_not_cut(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                (rig.home / "Photos" / "big.bin").write_bytes(b"x" * 1001)

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(
                        task, RunId.new(), str(rig.home / "Photos/big.bin"), max_bytes=1000
                    )
                exact = await client.read_file(
                    task, RunId.new(), str(rig.home / "Photos/big.bin"), max_bytes=1001
                )

                assert caught.value.reason == "too_large"
                assert exact.size == 1001

        run(scenario)

    def test_a_fifo_inside_a_granted_directory_is_refused_without_blocking(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                make_fifo(rig.home / "Photos" / "pipe")

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), str(rig.home / "Photos/pipe"))

                assert caught.value.reason in {"unsafe", "invalid_request"}

        run(scenario)

    def test_a_symlink_inside_a_granted_directory_is_not_followed(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                secret = rig.home / ".ssh" / "id_rsa"
                secret.write_text("PRIVATE KEY")
                (rig.home / "Photos" / "innocent.jpg").symlink_to(secret)

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), str(rig.home / "Photos/innocent.jpg"))

                # The decision resolves the path to the key (which is protected); never the content.
                assert caught.value.reason in {"denied", "not_granted", "unsafe"}
                assert "PRIVATE" not in str(caught.value)

        run(scenario)

    def test_a_grant_whose_directory_was_replaced_covers_nothing(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                (rig.home / "Photos").rename(rig.home / "Photos.old")
                (rig.home / "Photos").mkdir()
                (rig.home / "Photos" / "a.jpg").write_text("not the photo that was granted")

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), str(rig.home / PHOTO))

                assert caught.value.reason == "not_granted"

        run(scenario)

    def test_a_grant_whose_path_became_a_symlink_covers_nothing_and_leaks_nothing(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                (rig.home / "Photos").rename(rig.home / "Photos.old")
                (rig.home / "Photos").symlink_to(rig.home / "Documents")

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), str(rig.home / PHOTO))

                assert caught.value.reason in {"not_granted", "not_found", "unsafe"}

        run(scenario)

    def test_the_content_is_data_in_a_response_not_text(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                (rig.home / "Photos" / "bin").write_bytes(bytes(range(256)))

                content = await client.read_file(task, RunId.new(), str(rig.home / "Photos/bin"))

                assert decoded(content.data) == bytes(range(256))

        run(scenario)


class TestTheLargestAnswersFitAFrame:
    """Every answer is bounded so that the biggest one still fits the 1 MiB of a frame (I9)."""

    def test_a_read_of_the_most_a_request_may_ask_for(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                data = bytes(range(256)) * 2048  # 512 KiB, every byte value, in base64
                (rig.home / "Photos" / "max.bin").write_bytes(data)

                content = await client.read_file(
                    task, RunId.new(), str(rig.home / "Photos/max.bin")
                )

                assert decoded(content.data) == data

        run(scenario)

    def test_a_listing_of_the_most_entries_with_the_longest_names(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                many = rig.home / "Photos" / "many"
                many.mkdir()
                for number in range(1001):
                    (
                        many / (f"{number:04}x" + "\u00e9" * 125)
                    ).touch()  # 5 + 250 = 255 bytes, the most

                listing = await client.list_dir(task, RunId.new(), str(many))

                assert len(listing.entries) == 1000
                assert listing.truncated
                assert all(len(e.name.encode()) == 255 for e in listing.entries)

        run(scenario)


class TestOnceGrants:
    def test_a_once_grant_is_for_its_run_and_used_up_by_it(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                bound, other = RunId.new(), RunId.new()
                rig.prompts.allow("once")
                await client.request_read(task, bound, str(rig.home / "Photos"), "x")

                with pytest.raises(BrokerRefused) as other_run:
                    await client.read_file(task, other, str(rig.home / PHOTO))
                first = await client.read_file(task, bound, str(rig.home / PHOTO))
                with pytest.raises(BrokerRefused) as again:
                    await client.read_file(task, bound, str(rig.home / PHOTO))

                assert other_run.value.reason == "not_granted"
                assert first.size == 4
                assert again.value.reason == "not_granted"
                consumed = [e.consumed for e in rig.audit_of(FileAccess) if e.outcome == "allowed"]
                assert consumed == [True]

        run(scenario)

    def test_two_requests_racing_for_one_once_grant_cannot_both_win(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                bound = RunId.new()
                rig.prompts.allow("once")
                await client.request_read(task, bound, str(rig.home / "Photos"), "x")

                results = await asyncio.gather(
                    *(client.read_file(task, bound, str(rig.home / PHOTO)) for _ in range(8)),
                    return_exceptions=True,
                )

                wins = [r for r in results if not isinstance(r, BaseException)]
                refusals = [r for r in results if isinstance(r, BrokerRefused)]
                assert len(wins) == 1
                assert len(refusals) == 7
                assert {r.reason for r in refusals} == {"not_granted"}

        run(scenario)

    def test_a_task_grant_is_used_before_a_once_grant_so_the_once_grant_is_not_spent(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                bound = RunId.new()
                rig.prompts.allow("once")
                await client.request_read(task, bound, str(rig.home / "Photos"), "x")
                rig.prompts.allow("task")  # another run's request: the once grant is not for it
                await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")

                await client.read_file(task, bound, str(rig.home / PHOTO))
                await client.read_file(task, bound, str(rig.home / PHOTO))

                allowed = [e for e in rig.audit_of(FileAccess) if e.outcome == "allowed"]
                assert [e.consumed for e in allowed] == [False, False]

        run(scenario)


class TestASwapAfterTheDecision:
    """The decision saw one object; what is opened must be that object (S16: `expect=`)."""

    def swap_after_opening_the_grant(
        self, monkeypatch: pytest.MonkeyPatch, swap: Callable[[], None]
    ) -> None:
        real = open_granted

        def opened_then_swapped(stored: StoredPath, *, rules: DenyRules) -> OpenedPath:
            opened = real(stored, rules=rules)
            swap()
            return opened

        monkeypatch.setattr("tumbleweed.broker.service.open_granted", opened_then_swapped)

    def test_a_file_replaced_between_the_decision_and_the_open_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                photo = rig.home / PHOTO

                def swap() -> None:
                    photo.rename(rig.home / "Photos" / "a.jpg.old")
                    photo.write_text("FORGED")

                self.swap_after_opening_the_grant(monkeypatch, swap)

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), str(photo))

                assert caught.value.reason == "unsafe"
                assert "FORGED" not in str(caught.value)

        run(scenario)

    def test_a_directory_replaced_between_the_decision_and_the_open_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                directory = rig.home / "Photos" / "2026"

                def swap() -> None:
                    directory.rename(rig.home / "Photos" / "2026.old")
                    directory.mkdir()
                    (directory / "forged.txt").touch()

                self.swap_after_opening_the_grant(monkeypatch, swap)

                with pytest.raises(BrokerRefused) as caught:
                    await client.list_dir(task, RunId.new(), str(directory))

                assert caught.value.reason == "unsafe"

        run(scenario)


class TestWhatIsKeptOnRecord:
    def test_the_consumption_of_a_once_grant_is_in_the_database_before_the_file_is_read(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                bound = RunId.new()
                rig.prompts.allow("once")
                await client.request_read(task, bound, str(rig.home / "Photos"), "x")

                await client.read_file(task, bound, str(rig.home / PHOTO))

                database = sqlite3.connect(
                    f"file:{rig.config.broker_directory / 'broker.sqlite3'}?mode=ro", uri=True
                )
                try:
                    (record,) = database.execute("SELECT record FROM grants").fetchone()
                finally:
                    database.close()
                assert json.loads(record)["consumed_by"] == str(bound)

        run(scenario)

    def test_the_taint_a_grant_brings_is_in_the_database(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                await granted_task(rig, client)

                database = sqlite3.connect(
                    f"file:{rig.config.broker_directory / 'broker.sqlite3'}?mode=ro", uri=True
                )
                try:
                    (taint,) = database.execute("SELECT taint FROM tasks").fetchone()
                finally:
                    database.close()
                assert taint == "fs"

        run(scenario)


class TestListingUnderAGrant:
    def test_a_granted_directory_is_listed(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)

                listing = await client.list_dir(task, RunId.new(), str(rig.home / "Photos"))

                assert [(e.name, e.entry) for e in listing.entries] == [
                    ("2026", "directory"),
                    ("a.jpg", "file"),
                ]
                (entry,) = rig.audit_of(FileAccess)
                assert entry.operation == "list_dir"

        run(scenario)

    def test_a_directory_below_a_granted_one_is_listed(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                (rig.home / "Photos" / "2026" / "x.jpg").write_text("x")

                listing = await client.list_dir(task, RunId.new(), str(rig.home / "Photos/2026"))

                assert [e.name for e in listing.entries] == ["x.jpg"]

        run(scenario)

    def test_a_directory_outside_the_grants_is_refused(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.list_dir(task, RunId.new(), str(rig.home / "Documents"))

                assert caught.value.reason == "not_granted"

        run(scenario)

    def test_the_parent_of_a_granted_directory_is_not_listed(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.list_dir(task, RunId.new(), str(rig.home))

                assert caught.value.reason in {"denied", "not_granted"}

        run(scenario)

    def test_a_symlink_in_a_granted_directory_is_listed_as_one(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)
                (rig.home / "Photos" / "to-ssh").symlink_to(rig.home / ".ssh")

                listing = await client.list_dir(task, RunId.new(), str(rig.home / "Photos"))

                assert ("to-ssh", "symlink") in [(e.name, e.entry) for e in listing.entries]

        run(scenario)

    def test_a_file_is_not_a_directory(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted_task(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.list_dir(task, RunId.new(), str(rig.home / PHOTO))

                assert caught.value.reason == "invalid_request"

        run(scenario)


def write_in(rig: Rig, task: TaskId, relative: str, data: bytes = b"data") -> Path:
    path = rig.workspaces / str(task) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TestTheWorkspace:
    def test_a_file_the_sandbox_wrote_is_read(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                write_in(rig, task, "out/result.txt", b"42")

                content = await client.read_workspace_file(task, WorkspaceRelPath("out/result.txt"))

                assert decoded(content.data) == b"42"

        run(scenario)

    def test_the_workspace_is_listed(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                write_in(rig, task, "out/result.txt", b"42")
                write_in(rig, task, "a.txt")

                top = await client.list_workspace(task)
                below = await client.list_workspace(task, WorkspaceRelPath("out"))

                assert [(e.name, e.entry) for e in top.entries] == [
                    ("a.txt", "file"),
                    ("out", "directory"),
                ]
                assert [(e.name, e.size) for e in below.entries] == [("result.txt", 2)]

        run(scenario)

    def test_a_symlink_planted_in_the_workspace_is_not_followed_by_a_read(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                secret = rig.home / ".ssh" / "id_rsa"
                secret.write_text("PRIVATE KEY")
                (rig.workspaces / str(task) / "innocent.txt").symlink_to(secret)

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_workspace_file(task, WorkspaceRelPath("innocent.txt"))

                assert caught.value.reason == "unsafe"
                assert "PRIVATE" not in str(caught.value)

        run(scenario)

    def test_a_symlinked_directory_in_the_path_is_not_followed_either(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                (rig.workspaces / str(task) / "dir").symlink_to(rig.home / "Documents")

                with pytest.raises(BrokerRefused) as read:
                    await client.read_workspace_file(task, WorkspaceRelPath("dir/notes.txt"))
                with pytest.raises(BrokerRefused) as listed:
                    await client.list_workspace(task, WorkspaceRelPath("dir"))

                assert read.value.reason == listed.value.reason == "unsafe"

        run(scenario)

    def test_a_fifo_in_the_workspace_is_refused_without_blocking(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                make_fifo(rig.workspaces / str(task) / "pipe")

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_workspace_file(task, WorkspaceRelPath("pipe"))

                assert caught.value.reason == "unsafe"

        run(scenario)

    def test_a_missing_file_is_not_found_and_a_big_one_too_large(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                write_in(rig, task, "big", b"x" * 100)

                with pytest.raises(BrokerRefused) as missing:
                    await client.read_workspace_file(task, WorkspaceRelPath("nothing"))
                with pytest.raises(BrokerRefused) as big:
                    await client.read_workspace_file(task, WorkspaceRelPath("big"), max_bytes=99)

                assert missing.value.reason == "not_found"
                assert big.value.reason == "too_large"

        run(scenario)

    def test_a_task_cannot_read_the_workspace_of_another(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                one, two = (await client.start_task()).task, (await client.start_task()).task
                write_in(rig, one, "private.txt", b"mine")

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_workspace_file(two, WorkspaceRelPath("private.txt"))

                assert caught.value.reason == "not_found"

        run(scenario)

    def test_a_workspace_swapped_for_another_directory_is_refused(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                workspace = rig.workspaces / str(task)
                workspace.rename(rig.workspaces / "moved")
                workspace.mkdir()
                (workspace / "x").write_text("forged")

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_workspace_file(task, WorkspaceRelPath("x"))

                assert caught.value.reason == "unsafe"

        run(scenario)
