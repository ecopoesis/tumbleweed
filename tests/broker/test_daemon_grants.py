"""Grants over IPC: requested, decided by the user, covered, refused, revoked (I2, I12)."""

import dataclasses
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.rig import Rig, running
from tests.ipc.bounded import run
from tumbleweed.broker.audit import AuditLog, GrantDecision, GrantRevoked
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.errors import AuditError
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.prompts import Allowed, Denied
from tumbleweed.core.ids import GrantId, RunId, TaskId


async def task_of(client: BrokerClient) -> TaskId:
    return (await client.start_task()).task


def read_path(rig: Rig, relative: str = "Photos") -> str:
    return str(rig.home / relative)


@contextmanager
def directory_under_tmp() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="twgr-", dir="/tmp"))
    try:
        yield directory
    finally:
        directory.rmdir()


class TestAskingTheUser:
    def test_with_nobody_to_ask_nothing_is_granted(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)

                outcome = await client.request_read(task, RunId.new(), read_path(rig), "to look")

                assert not outcome.granted
                assert outcome.grant is None
                (entry,) = rig.audit_of(GrantDecision)
                assert entry.outcome == "denied"
                assert entry.reason == "user_denied"
                assert entry.capability == "fs.read"
                assert entry.path == read_path(rig)
                assert entry.model_reason == "to look"
                (prompt,) = rig.prompts.asked
                assert prompt.reason == "to look"
                assert prompt.task == task

        run(scenario)

    def test_the_prompt_is_given_the_capability_as_a_structure_and_the_scopes_it_may_offer(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)

                await client.request_read(task, RunId.new(), read_path(rig), "x")

                (prompt,) = rig.prompts.asked
                assert prompt.capability.kind.value == "fs.read"
                assert prompt.capability.path.path == read_path(rig)  # type: ignore[union-attr]
                assert prompt.scopes == {GrantScope.ONCE, GrantScope.TASK}  # no tool in S17

        run(scenario)

    def test_the_user_allowing_it_makes_a_grant_and_audits_it(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")

                outcome = await client.request_read(task, RunId.new(), read_path(rig), "x")

                assert outcome.granted
                assert outcome.scope == "task"
                assert outcome.grant is not None
                (entry,) = rig.audit_of(GrantDecision)
                assert (entry.outcome, entry.scope, entry.grant) == (
                    "granted",
                    "task",
                    outcome.grant,
                )

        run(scenario)

    def test_a_request_the_grants_already_cover_is_not_asked_again(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")
                first = await client.request_read(task, RunId.new(), read_path(rig), "x")

                again = await client.request_read(
                    task, RunId.new(), read_path(rig, "Photos/2026"), "x"
                )

                assert again.granted
                assert again.grant == first.grant
                assert again.message == "already covered by a grant"
                assert len(rig.prompts.asked) == 1
                assert [e.outcome for e in rig.audit_of(GrantDecision)] == ["granted", "covered"]

        run(scenario)

    def test_the_prompt_is_asked_after_the_path_is_resolved_not_with_the_text_it_was_given(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                link = rig.home / "link-to-photos"
                link.symlink_to(rig.home / "Photos")

                await client.request_read(task, RunId.new(), str(link), "x")

                (prompt,) = rig.prompts.asked
                assert prompt.capability.path.path == read_path(rig)  # type: ignore[union-attr]

        run(scenario)


class TestWhatCannotBeGranted:
    @pytest.mark.parametrize("relative", [".ssh", ".ssh/id_rsa", ".gnupg", ".aws", ""])
    def test_a_protected_place_is_refused_without_asking_anyone(self, relative: str) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")  # would be accepted, if anyone asked

                path = str(rig.home / relative) if relative else str(rig.home)
                outcome = await client.request_read(task, RunId.new(), path, "x")

                assert not outcome.granted
                assert "protected location" in outcome.message or "contains a protected" in (
                    outcome.message
                )
                assert rig.prompts.asked == []
                assert rig.audit_of(GrantDecision)[0].reason == "static_deny"
                assert str(rig.home) not in outcome.message  # the rule is named, not the path

        run(scenario)

    def test_the_brokers_own_directories_are_refused_wherever_the_data_home_is(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")  # would be accepted, if anyone asked
                own = [
                    rig.workspaces / str(task),  # another task's, or this one's: never granted
                    rig.workspaces,
                    rig.config.broker_directory,  # the database and the audit log
                    rig.config.socket_directory,
                ]

                for path in own:
                    outcome = await client.request_read(task, RunId.new(), str(path), "x")
                    assert not outcome.granted, path
                    assert str(path) not in outcome.message

                assert rig.prompts.asked == []

        run(scenario)

    def test_a_path_inside_tmp_is_refused_because_the_sandbox_would_refuse_it(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")

                with directory_under_tmp() as directory:
                    outcome = await client.request_read(task, RunId.new(), str(directory), "x")

                assert not outcome.granted
                assert rig.prompts.asked == []
                assert rig.audit_of(GrantDecision)[0].reason == "static_deny"

        run(scenario)

    def test_writes_are_refused_by_the_gate_without_asking_anyone(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")

                outcome = await client.request_write(task, RunId.new(), read_path(rig), "x")

                assert not outcome.granted
                assert "not available yet" in outcome.message
                assert rig.prompts.asked == []
                assert rig.audit_of(GrantDecision)[0].reason == "write_gate"

        run(scenario)

    def test_a_path_that_does_not_exist_cannot_be_granted(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")

                outcome = await client.request_read(
                    task, RunId.new(), read_path(rig, "Nowhere"), "x"
                )

                assert not outcome.granted
                assert rig.prompts.asked == []
                assert rig.audit_of(GrantDecision)[0].reason == "ungrantable"

        run(scenario)

    @pytest.mark.parametrize("path", ["relative/path", "/a/\nb", "/a/\u202eb", "/a/b\x00c"])
    def test_a_path_that_is_not_acceptable_is_refused_not_resolved(self, path: str) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.request_read(task, RunId.new(), path, "x")

                assert caught.value.reason == "invalid_request"
                assert rig.prompts.asked == []

        run(scenario)

    def test_the_refusal_never_repeats_what_it_was_given(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.request_read(task, RunId.new(), "/SECRET/\nPATH", "x")

                assert "SECRET" not in str(caught.value)

        run(scenario)

    def test_dots_are_resolved_before_anything_is_decided(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")

                outcome = await client.request_read(
                    task, RunId.new(), str(rig.home / "Photos" / ".." / ".ssh"), "x"
                )

                assert not outcome.granted  # it is ~/.ssh, whatever it was called
                assert rig.audit_of(GrantDecision)[0].reason == "static_deny"
                assert rig.prompts.asked == []

        run(scenario)


class TestTheWorldMovesWhileTheUserDecides:
    def test_a_directory_that_became_a_protected_one_during_the_prompt_is_not_granted(
        self,
    ) -> None:
        """The user was shown `Photos`. By the time they click, `Photos` has been renamed to
        `.ssh` (and the real `.ssh` moved away): the grant would be for the key directory. The
        deny rules are rebuilt when the grant is issued, so the identity is recognised."""

        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")

                def swap() -> None:
                    (rig.home / ".ssh").rename(rig.home / ".ssh.moved")
                    (rig.home / "Photos").rename(rig.home / ".ssh")

                rig.prompts.while_asking = swap

                outcome = await client.request_read(task, RunId.new(), read_path(rig), "x")

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[0].reason == "static_deny"
                with pytest.raises(BrokerRefused):
                    await client.list_dir(task, RunId.new(), str(rig.home / ".ssh"))

        run(scenario)

    def test_a_directory_swapped_for_another_during_the_prompt_is_granted_as_it_was_shown(
        self,
    ) -> None:
        """What is recorded is what the user was shown (its identity): after a swap the grant
        covers the old directory, wherever it is now, and not what stands at the name."""

        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")

                def swap() -> None:
                    (rig.home / "Photos").rename(rig.home / "Photos.old")
                    (rig.home / "Photos").mkdir()
                    (rig.home / "Photos" / "a.jpg").write_text("a stranger's file")

                rig.prompts.while_asking = swap

                outcome = await client.request_read(task, RunId.new(), read_path(rig), "x")

                assert outcome.granted
                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), read_path(rig, "Photos/a.jpg"))
                assert caught.value.reason == "not_granted"  # the new directory is not covered

        run(scenario)


class TestTheAnswerIsChecked:
    def test_a_scope_that_was_not_offered_is_a_denial(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.answers.append(Allowed(scope=GrantScope.TOOL))

                outcome = await client.request_read(task, RunId.new(), read_path(rig), "x")

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[0].reason == "scope_not_offered"

        run(scenario)

    def test_a_prompt_that_raises_is_a_denial(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.explode = True

                outcome = await client.request_read(task, RunId.new(), read_path(rig), "x")

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[0].reason == "prompt_failed"

        run(scenario)

    def test_a_prompt_that_never_answers_is_a_denial_when_the_time_is_up(self) -> None:
        async def scenario() -> None:
            async with running(
                config=lambda c: _replace(c, prompt_timeout=timedelta(milliseconds=200))
            ) as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.hang = True

                outcome = await client.request_read(task, RunId.new(), read_path(rig), "x")

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[0].reason == "prompt_timeout"

        run(scenario)

    def test_an_expiry_in_the_past_is_refused_and_one_in_the_future_ends_the_grant(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.answers.append(
                    Allowed(scope=GrantScope.TASK, expires_at=rig.clock.now - timedelta(seconds=1))
                )
                refused = await client.request_read(task, RunId.new(), read_path(rig), "x")
                rig.prompts.answers.append(
                    Allowed(scope=GrantScope.TASK, expires_at=rig.clock.now + timedelta(minutes=5))
                )
                granted = await client.request_read(task, RunId.new(), read_path(rig), "x")
                run_id = RunId.new()
                readable = await client.read_file(task, run_id, read_path(rig, "Photos/a.jpg"))
                rig.clock.advance(timedelta(minutes=5))

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, run_id, read_path(rig, "Photos/a.jpg"))

                assert not refused.granted
                assert granted.granted
                assert readable.size == 4
                assert caught.value.reason == "not_granted"

        run(scenario)

    def test_a_prompt_that_answers_with_something_else_is_a_denial(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.answers.append("yes please")  # type: ignore[arg-type]

                outcome = await client.request_read(task, RunId.new(), read_path(rig), "x")

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[0].reason == "prompt_failed"

        run(scenario)

    def test_the_denied_answer_keeps_its_own_reason_in_the_audit_log(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.answers.append(Denied(reason="rate_limited"))

                await client.request_read(task, RunId.new(), read_path(rig), "x")

                assert rig.audit_of(GrantDecision)[0].reason == "rate_limited"

        run(scenario)


class TestTaint:
    def test_a_task_given_files_cannot_be_given_the_network(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")
                await client.request_read(task, RunId.new(), read_path(rig), "x")
                rig.prompts.allow("task")  # would be accepted, if anyone asked

                outcome = await client.request_network(task, RunId.new(), "x")

                assert not outcome.granted
                assert "fresh_context=true" in outcome.message
                assert len(rig.prompts.asked) == 1
                assert rig.audit_of(GrantDecision)[-1].reason == "taint_conflict"

        run(scenario)

    def test_a_task_given_the_network_cannot_be_given_files(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("once")
                await client.request_network(task, RunId.new(), "x")

                outcome = await client.request_read(task, RunId.new(), read_path(rig), "x")

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[-1].reason == "taint_conflict"

        run(scenario)

    def test_a_refused_grant_does_not_taint(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                await client.request_read(task, RunId.new(), read_path(rig), "x")  # denied

                rig.prompts.allow("task")
                outcome = await client.request_network(task, RunId.new(), "x")

                assert outcome.granted

        run(scenario)

    def test_the_taint_is_the_brokers_not_the_callers(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("once")
                await client.request_read(task, RunId.new(), read_path(rig), "x")  # used or not

                started = await client.start_task(previous=task, fresh_context=False)

                assert started.taint == ("fs",)

        run(scenario)


class TestRevoking:
    def test_a_revoked_grant_is_gone_and_audited(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")
                granted = await client.request_read(task, RunId.new(), read_path(rig), "x")
                assert granted.grant is not None

                await client.revoke_grant(task, granted.grant)

                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, RunId.new(), read_path(rig, "Photos/a.jpg"))
                assert caught.value.reason == "not_granted"
                (entry,) = rig.audit_of(GrantRevoked)
                assert (entry.grant, entry.task, entry.reason) == (
                    granted.grant,
                    task,
                    "requested",
                )

        run(scenario)

    def test_taint_stays_after_a_revocation(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")
                granted = await client.request_read(task, RunId.new(), read_path(rig), "x")
                assert granted.grant is not None
                await client.revoke_grant(task, granted.grant)
                rig.prompts.allow("task")

                outcome = await client.request_network(task, RunId.new(), "x")

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[-1].reason == "taint_conflict"

        run(scenario)

    def test_a_grant_that_is_not_the_tasks_is_not_found(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                one, two = await task_of(client), await task_of(client)
                rig.prompts.allow("task")
                granted = await client.request_read(one, RunId.new(), read_path(rig), "x")
                assert granted.grant is not None

                with pytest.raises(BrokerRefused) as unknown:
                    await client.revoke_grant(two, granted.grant)
                with pytest.raises(BrokerRefused) as never_issued:
                    await client.revoke_grant(one, GrantId.new())

                assert unknown.value.reason == never_issued.value.reason == "not_found"
                assert rig.audit_of(GrantRevoked) == []
                # Still held:
                await client.read_file(one, RunId.new(), read_path(rig, "Photos/a.jpg"))

        run(scenario)


class TestTheAuditLogIsFailClosed:
    def test_a_grant_whose_entry_cannot_be_written_is_not_made(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)
                rig.prompts.allow("task")
                real = AuditLog.append

                def refuse(self: AuditLog, entry: object) -> None:
                    if isinstance(entry, GrantDecision):
                        raise AuditError("the audit log cannot be written")
                    real(self, entry)  # type: ignore[arg-type]

                monkeypatch.setattr(AuditLog, "append", refuse)

                with pytest.raises(BrokerRefused) as caught:
                    await client.request_read(task, RunId.new(), read_path(rig), "x")
                monkeypatch.undo()

                assert caught.value.reason == "unavailable"
                with pytest.raises(BrokerRefused) as unread:
                    await client.read_file(task, RunId.new(), read_path(rig, "Photos/a.jpg"))
                assert unread.value.reason == "not_granted"  # nothing was granted
                # Nor was the task tainted by a grant that was never made:
                rig.prompts.allow("task")
                assert (await client.request_network(task, RunId.new(), "x")).granted

        run(scenario)

    def test_a_task_whose_start_cannot_be_recorded_is_not_made_and_leaves_no_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                def refuse(self: AuditLog, entry: object) -> None:
                    raise AuditError("the audit log cannot be written")

                monkeypatch.setattr(AuditLog, "append", refuse)
                with pytest.raises(BrokerRefused) as caught:
                    await client.start_task()
                monkeypatch.undo()

                assert caught.value.reason == "unavailable"
                assert list(rig.workspaces.iterdir()) == []

        run(scenario)

    def test_a_task_whose_end_cannot_be_recorded_is_not_ended_on_record_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await task_of(client)

                def refuse(self: AuditLog, entry: object) -> None:
                    raise AuditError("the audit log cannot be written")

                monkeypatch.setattr(AuditLog, "append", refuse)
                with pytest.raises(BrokerRefused) as caught:
                    await client.end_task(task)
                monkeypatch.undo()

                assert caught.value.reason == "unavailable"
                await client.end_task(task)  # the task was still live: this time it works

        run(scenario)


def _replace(config: BrokerConfig, **changes: object) -> BrokerConfig:
    return dataclasses.replace(config, **changes)  # type: ignore[arg-type]
