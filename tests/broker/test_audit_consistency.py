"""What the audit log and the database say after a request that is refused or fails (I7)."""

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import pytest

from tests.broker.rig import FakeSandbox, Rig, replace_config, running
from tests.ipc.bounded import run
from tumbleweed.broker.audit import (
    AuditLog,
    FileAccess,
    GrantDecision,
    GrantRevoked,
    RolledBack,
    SandboxRun,
    TaskStarted,
)
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.deny import DenyRules
from tumbleweed.broker.errors import AuditError, StoreError
from tumbleweed.broker.policy import open_granted
from tumbleweed.broker.service import MAX_GRANTS_PER_TASK
from tumbleweed.broker.store import BrokerStore
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.ipc.broker_messages import EnvPair
from tumbleweed.ipc.errors import RequestTimeoutError

COMMAND = ("/usr/bin/python3", "-c", "print(1)")
PHOTO = "Photos/a.jpg"


async def granted(
    rig: Rig, client: BrokerClient, relative: str = "Photos", scope: str = "task"
) -> TaskId:
    task = (await client.start_task()).task
    rig.prompts.allow(scope)
    assert (await client.request_read(task, RunId.new(), str(rig.home / relative), "x")).granted
    return task


def refuse_audit_of(kind: type, monkeypatch: pytest.MonkeyPatch) -> None:
    real = AuditLog.append

    def refuse(self: AuditLog, entry: object) -> None:
        if isinstance(entry, kind):
            raise AuditError("the audit log cannot be written")
        real(self, entry)  # type: ignore[arg-type]  # the wrapper takes object, as any stand-in

    monkeypatch.setattr(AuditLog, "append", refuse)


@contextmanager
def commit_that_fails(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The transaction's commit fails, after the audit entries were written inside it."""
    real = BrokerStore.transaction

    @contextmanager
    def failing(self: BrokerStore) -> Iterator[None]:
        with real(self):
            yield
            raise StoreError("the database could not commit")

    monkeypatch.setattr(BrokerStore, "transaction", failing)
    try:
        yield
    finally:
        monkeypatch.undo()


class TestTheDatabaseMatchesTheAnswer:
    def test_a_grant_whose_entry_cannot_be_written_is_not_in_the_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.prompts.allow("task")
                refuse_audit_of(GrantDecision, monkeypatch)

                with pytest.raises(BrokerRefused):
                    await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")
                monkeypatch.undo()

                assert rig.rows("SELECT count(*) FROM grants") == [(0,)]
                assert rig.rows("SELECT state, taint FROM tasks") == [(b"live".decode(), "none")]

        run(scenario)

    def test_a_commit_that_fails_leaves_a_rolled_back_entry_after_the_ones_it_made_untrue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.prompts.allow("task")

                with commit_that_fails(monkeypatch), pytest.raises(BrokerRefused) as caught:
                    await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")

                assert caught.value.reason == "unavailable"
                assert rig.rows("SELECT count(*) FROM grants") == [(0,)]
                log = rig.audit()
                assert isinstance(log[-1], RolledBack)
                assert log[-1].entries == ("grant_decision",)
                assert isinstance(log[-2], GrantDecision)
                assert log[-2].outcome == "granted"  # what the entry before it claimed

        run(scenario)

    def test_a_task_start_that_fails_to_commit_is_rolled_back_and_leaves_no_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                with commit_that_fails(monkeypatch), pytest.raises(BrokerRefused):
                    await client.start_task()

                assert [type(e) for e in rig.audit()][-2:] == [TaskStarted, RolledBack]
                assert list(rig.workspaces.iterdir()) == []
                assert rig.rows("SELECT count(*) FROM tasks") == [(0,)]

        run(scenario)

    def test_after_a_task_ends_the_database_holds_no_grant_of_it(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted(rig, client)
                rig.prompts.allow("once")
                await client.request_read(task, RunId.new(), str(rig.home / "Documents"), "x")
                assert rig.rows("SELECT count(*) FROM grants WHERE revoked_at IS NULL") == [(2,)]

                await client.end_task(task)

                assert rig.rows("SELECT count(*) FROM grants WHERE revoked_at IS NULL") == [(0,)]
                assert rig.rows("SELECT state FROM tasks") == [("ended",)]

        run(scenario)

    def test_after_a_revocation_the_database_holds_no_such_grant(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.prompts.allow("task")
                outcome = await client.request_read(
                    task, RunId.new(), str(rig.home / "Photos"), "x"
                )
                assert outcome.grant is not None

                await client.revoke_grant(task, outcome.grant)

                assert rig.rows("SELECT count(*) FROM grants WHERE revoked_at IS NULL") == [(0,)]
                (entry,) = rig.audit_of(GrantRevoked)
                assert entry.grant == outcome.grant

        run(scenario)

    def test_a_refused_read_or_listing_is_a_refused_entry_and_changes_nothing(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted(rig, client)
                before = rig.rows("SELECT record FROM grants")

                with pytest.raises(BrokerRefused):
                    await client.read_file(task, RunId.new(), str(rig.home / "Documents/notes.txt"))
                with pytest.raises(BrokerRefused):
                    await client.list_dir(task, RunId.new(), str(rig.home / "Documents"))

                refused = [e for e in rig.audit_of(FileAccess) if e.outcome == "refused"]
                assert [(e.operation, e.reason) for e in refused] == [
                    ("read_file", "not_granted"),
                    ("list_dir", "not_granted"),
                ]
                assert rig.rows("SELECT record FROM grants") == before

        run(scenario)

    def test_the_consumption_of_a_once_grant_by_a_run_is_in_the_database(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                bound = RunId.new()
                rig.prompts.allow("once")
                await client.request_read(task, bound, str(rig.home / "Photos"), "x")

                await client.run_in_sandbox(task, bound, COMMAND)

                ((record,),) = rig.rows("SELECT record FROM grants")
                assert str(bound) in str(record)
                assert '"consumed_by":"run_' in str(record)

        run(scenario)


class TestTheCapOnGrants:
    def test_the_sixty_fifth_grant_is_refused_without_asking_anyone(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                for number in range(MAX_GRANTS_PER_TASK):
                    (rig.home / f"d{number}").mkdir()
                    rig.prompts.allow("task")
                    assert (
                        await client.request_read(
                            task, RunId.new(), str(rig.home / f"d{number}"), "x"
                        )
                    ).granted
                asked = len(rig.prompts.asked)
                (rig.home / "one-more").mkdir()
                rig.prompts.allow("task")

                with pytest.raises(BrokerRefused) as caught:
                    await client.request_read(task, RunId.new(), str(rig.home / "one-more"), "x")

                assert caught.value.reason == "busy"
                assert len(rig.prompts.asked) == asked  # nobody was asked in vain
                last = rig.audit_of(GrantDecision)[-1]
                assert (last.outcome, last.reason) == ("denied", "grant_cap")

        run(scenario)

    def test_grants_that_are_used_up_do_not_count_against_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tumbleweed.broker.service.MAX_GRANTS_PER_TASK", 2)

        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                for number in range(5):  # five once grants, each used: never more than 2 held
                    (rig.home / f"d{number}").mkdir()
                    run_id = RunId.new()
                    rig.prompts.allow("once")
                    assert (
                        await client.request_read(task, run_id, str(rig.home / f"d{number}"), "x")
                    ).granted
                    await client.list_dir(task, run_id, str(rig.home / f"d{number}"))

        run(scenario)

    def test_a_cap_reached_meanwhile_is_a_recorded_denial_after_the_user_said_yes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tumbleweed.broker.service.MAX_GRANTS_PER_TASK", 1)

        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                (rig.home / "d1").mkdir()
                (rig.home / "d2").mkdir()

                async def thinking() -> None:
                    await asyncio.sleep(0.1)  # both are asked before either is answered

                rig.prompts.while_asking = thinking
                rig.prompts.allow("task")
                rig.prompts.allow("task")

                results = await asyncio.gather(
                    client.request_read(task, RunId.new(), str(rig.home / "d1"), "x"),
                    client.request_read(task, RunId.new(), str(rig.home / "d2"), "x"),
                    return_exceptions=True,
                )

                assert len([r for r in results if not isinstance(r, BaseException)]) == 1
                (refused,) = [r for r in results if isinstance(r, BrokerRefused)]
                assert refused.reason == "busy"
                assert sorted(e.outcome for e in rig.audit_of(GrantDecision)) == [
                    "denied",
                    "granted",
                ]
                denied = [e for e in rig.audit_of(GrantDecision) if e.outcome == "denied"]
                assert denied[0].reason == "busy"

        run(scenario)


class TestRefusalsAfterTheUserHasSaidYes:
    def test_a_task_that_ended_during_the_prompt_leaves_a_denied_entry_with_the_reason(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.prompts.allow("task")

                async def end() -> None:
                    await client.end_task(task)

                rig.prompts.while_asking = end

                with pytest.raises(BrokerRefused) as caught:
                    await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")

                assert caught.value.reason == "unknown_task"
                (entry,) = rig.audit_of(GrantDecision)
                assert (entry.outcome, entry.reason) == ("denied", "unknown_task")
                assert rig.rows("SELECT count(*) FROM grants") == [(0,)]

        run(scenario)

    def test_a_file_system_that_hangs_when_the_grant_is_issued_is_a_recorded_denial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running(
                config=lambda c: replace_config(c, fs_timeout=timedelta(milliseconds=300))
            ) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.prompts.allow("task")
                real = DenyRules.build
                calls = [0]

                def build(context: object) -> DenyRules:
                    calls[0] += 1
                    if calls[0] >= 2:  # the second build: when the grant is issued
                        time.sleep(1.5)
                    return real(context)  # type: ignore[arg-type]  # a stand-in takes object

                monkeypatch.setattr(DenyRules, "build", staticmethod(build))

                with pytest.raises(BrokerRefused) as caught:
                    await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")
                monkeypatch.undo()

                assert caught.value.reason == "timeout"
                (entry,) = rig.audit_of(GrantDecision)
                assert (entry.outcome, entry.reason) == ("denied", "timeout")
                assert rig.rows("SELECT count(*) FROM grants") == [(0,)]

        run(scenario)


class TestRefusedRunsAreRecorded:
    """Every refusal once the task is known (B3). The run's own entry says why."""

    async def reasons(self, rig: Rig) -> list[str | None]:
        return [e.reason for e in rig.audit_of(SandboxRun) if e.outcome == "refused"]

    def test_a_forbidden_variable(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                with pytest.raises(BrokerRefused):
                    await client.run_in_sandbox(
                        task,
                        RunId.new(),
                        COMMAND,
                        env=(EnvPair(name="ANTHROPIC_API_KEY", value="x"),),
                    )

                (entry,) = rig.audit_of(SandboxRun)
                assert entry.env_names == ("ANTHROPIC_API_KEY",)  # the name, never the value
                assert await self.reasons(rig) == ["invalid_request"]

        run(scenario)

    def test_busy(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.hang = True
            async with running(
                sandbox=sandbox, config=lambda c: replace_config(c, max_concurrent_runs=1)
            ) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                first = asyncio.ensure_future(client.run_in_sandbox(task, RunId.new(), COMMAND))
                await sandbox.started.wait()

                with pytest.raises(BrokerRefused):
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)

                assert "run_slots" in await self.reasons(rig)

        run(scenario)

    def test_a_run_id_used_before_and_the_cap_on_a_task(self) -> None:
        async def scenario() -> None:
            async with running(config=lambda c: replace_config(c, max_runs_per_task=1)) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                used = RunId.new()
                await client.run_in_sandbox(task, used, COMMAND)

                with pytest.raises(BrokerRefused):
                    await client.run_in_sandbox(task, used, COMMAND)
                with pytest.raises(BrokerRefused):
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert await self.reasons(rig) == ["run_id_reused", "run_cap"]

        run(scenario)

    def test_grants_that_changed_while_the_run_was_set_up_and_the_rules_used_to_open_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.prompts.allow("task")
                made = await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")
                assert made.grant is not None
                opened_with: list[object] = []
                entered, proceed = threading.Event(), threading.Event()
                real_open = open_granted

                def watching(stored: object, *, rules: object) -> object:
                    opened_with.append(rules)
                    opened = real_open(stored, rules=rules)  # type: ignore[arg-type]  # a stand-in takes object
                    entered.set()  # the grant is open; the run is not yet authorised
                    proceed.wait(10)
                    return opened

                monkeypatch.setattr("tumbleweed.broker.service.open_granted", watching)
                call = asyncio.ensure_future(client.run_in_sandbox(task, RunId.new(), COMMAND))
                assert await asyncio.to_thread(entered.wait, 10)

                await client.revoke_grant(task, made.grant)  # while the run is being set up
                proceed.set()

                with pytest.raises(BrokerRefused) as caught:
                    await call
                assert caught.value.reason == "busy"
                assert await self.reasons(rig) == ["grants_changed"]
                assert rig.sandbox.policies == []
                # Every grant of a run is opened with the deny rules, never by a bare open:
                assert opened_with
                assert all(isinstance(r, DenyRules) for r in opened_with)

        run(scenario)


class TestRefusedReadsAreRecordedAfterTheyWereAllowed:
    def test_a_file_that_was_swapped_leaves_an_allowed_entry_and_then_a_refused_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await granted(rig, client)
                photo = rig.home / PHOTO
                real = open_granted

                def swapping(stored: object, *, rules: object) -> object:
                    opened = real(stored, rules=rules)  # type: ignore[arg-type]  # a stand-in takes object
                    photo.rename(rig.home / "Photos" / "a.jpg.old")
                    photo.write_text("FORGED")
                    return opened

                monkeypatch.setattr("tumbleweed.broker.service.open_granted", swapping)

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), str(photo))

                assert caught.value.reason == "unsafe"
                outcomes = [(e.outcome, e.reason) for e in rig.audit_of(FileAccess)]
                assert outcomes == [("allowed", None), ("refused", "identity_mismatch")]

        run(scenario)


class TestTheLogIsOneLinePerEntry:
    def test_characters_a_reader_might_take_for_a_line_break_stay_in_one_line(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                reason = "a\u0085b\u2028c\u2029d\x0be\x0cf\x1cg\nh\ri \U0001f600"

                await client.request_read(task, RunId.new(), str(rig.home / "Photos"), reason)

                raw = rig.audit_path.read_text(encoding="utf-8")
                assert raw.isascii()
                assert len(raw.splitlines()) == len(rig.audit())  # one line per entry
                entry = rig.audit_of(GrantDecision)[0]
                assert entry.model_reason == reason  # and nothing was lost

        run(scenario)


class TestTheHeartbeat:
    def test_a_ping_that_fails_is_logged_and_the_next_is_tried(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await BrokerClient.connect(
                    rig.socket, heartbeat=timedelta(milliseconds=30)
                )
                rig.clients.append(client)
                real = client.connection.call
                calls = [0]

                async def flaky(*args: object, **kwargs: object) -> object:
                    calls[0] += 1
                    if calls[0] <= 2:
                        raise RequestTimeoutError("no answer")
                    return await real(*args, **kwargs)  # type: ignore[arg-type]  # a stand-in forwards its arguments

                client.connection.call = flaky  # type: ignore[method-assign,assignment]  # a seam for this test

                async with asyncio.timeout(10):
                    while calls[0] < 5:  # noqa: ASYNC110  (bounded)
                        await asyncio.sleep(0.02)

                assert not client.is_closed

        run(scenario)

    def test_the_heartbeat_stops_when_the_connection_ends(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await BrokerClient.connect(
                    rig.socket, heartbeat=timedelta(milliseconds=30)
                )
                rig.clients.append(client)
                await client.connection.close()

                async with asyncio.timeout(10):
                    while not client.heartbeat_stopped:  # noqa: ASYNC110  (bounded)
                        await asyncio.sleep(0.02)

        run(scenario)
