"""The prompt protocol between the broker and the shell (S18, I5, I10), over a real daemon.

The shell is a `FakeShell`: it records what it is shown and answers when the test says so, on the
connection the test picks. What is proved here is the broker's half of I5: a `PromptId` is
unguessable and single-use, only the connection a prompt was sent to can answer it, a timeout, a
vanished shell or a shell that cannot show it is a denial, a task cannot ask more than five times in
ten minutes, and the picker's choice goes through the same policy as any other path.
"""

import asyncio
import json
from datetime import timedelta

import pytest

from tests.broker.fake_shell import FakeShell, allow, answer, deny
from tests.broker.rig import Rig, replace_config, running, until
from tests.ipc.bounded import run
from tumbleweed.broker import service as service_module
from tumbleweed.broker.audit import GrantDecision, TaskEnded
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.errors import FsTimeoutError
from tumbleweed.broker.service import BrokerService
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import PromptId, RunId, TaskId
from tumbleweed.ipc.broker_messages import (
    GrantOutcome,
    NetworkAccess,
    PickAccess,
    ReadAccess,
    Refused,
)
from tumbleweed.ipc.errors import ConnectionClosedError, ErrorCode, RemoteError
from tumbleweed.ipc.prompt_messages import AllowDecision, PromptAnswered, ShowPrompt


async def task_of(client: BrokerClient) -> TaskId:
    return (await client.start_task()).task


def photos(rig: Rig) -> str:
    return str(rig.home / "Photos")


async def asked(
    client: BrokerClient, task: TaskId, rig: Rig, reason: str = "to look"
) -> "asyncio.Task[GrantOutcome]":
    return asyncio.create_task(client.request_read(task, RunId.new(), photos(rig), reason))


class TestWhatTheShellIsShown:
    def test_the_prompt_is_the_structure_the_broker_resolved_not_the_models_text(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)
                link = rig.home / "link"
                link.symlink_to(rig.home / "Photos")

                pending = asyncio.create_task(
                    client.request_read(task, RunId.new(), str(link), "I want to look")
                )
                shown = await shell.next_prompt()

                assert shown.task == task
                assert shown.tool is None
                assert shown.access == ReadAccess(kind="fs.read", path=photos(rig))  # not `link`
                assert shown.scopes == ("once", "task")
                assert shown.reason == "I want to look"
                assert 0 < shown.expires_in_ms <= 120_000
                await deny(client, shown)
                assert not (await pending).granted

        run(scenario)

    def test_a_network_request_and_a_picker_are_shown_as_what_they_are(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                first = asyncio.create_task(client.request_network(task, RunId.new(), "net"))
                net = await shell.next_prompt()
                await deny(client, net)
                second = asyncio.create_task(client.request_pick(task, RunId.new(), "folder", "p"))
                pick = await shell.next_prompt()
                await deny(client, pick)
                await first
                await second

                assert net.access == NetworkAccess(kind="net.full")
                assert pick.access == PickAccess(kind="fs.pick", choose="folder")

        run(scenario)

    def test_a_write_request_never_reaches_the_shell(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                outcome = await client.request_write(task, RunId.new(), photos(rig), "write")

                assert not outcome.granted
                assert shell.shown == []  # the write gate (S16) refused it first

        run(scenario)


class TestWhoCanAnswer:
    def test_an_allow_from_the_connection_the_prompt_went_to_is_a_grant_and_is_audited(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = await asked(client, task, rig)
                shown = await shell.next_prompt()
                reply = await allow(client, shown, scope="once")
                outcome = await pending

                assert isinstance(reply, PromptAnswered)
                assert outcome.granted
                assert outcome.scope == "once"
                (entry,) = rig.audit_of(GrantDecision)
                assert (entry.outcome, entry.scope, entry.grant) == (
                    "granted",
                    "once",
                    outcome.grant,
                )

        run(scenario)

    def test_an_answer_from_another_connection_is_refused_and_the_prompt_stays_open(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                intruder = await rig.client(handler=FakeShell())
                task = await task_of(client)

                pending = await asked(client, task, rig)
                shown = await shell.next_prompt()
                # The intruder somehow has the PromptId: it is still not its prompt.
                refused = await allow(intruder, shown)
                await asyncio.sleep(0.05)

                assert isinstance(refused, Refused)
                assert refused.reason == "unknown_prompt"
                assert not pending.done()
                assert rig.audit_of(GrantDecision) == []
                # The right connection can still answer it.
                await deny(client, shown)
                assert not (await pending).granted

        run(scenario)

    def test_a_prompt_id_cannot_be_used_twice(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = await asked(client, task, rig)
                shown = await shell.next_prompt()
                first = await allow(client, shown, scope="once")
                outcome = await pending
                replay = await allow(client, shown, scope="task")

                assert isinstance(first, PromptAnswered)
                assert outcome.scope == "once"
                assert isinstance(replay, Refused)
                assert replay.reason == "unknown_prompt"
                assert len(rig.audit_of(GrantDecision)) == 1  # the replay made no second decision

        run(scenario)

    def test_two_answers_sent_together_make_one_decision(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = await asked(client, task, rig)
                shown = await shell.next_prompt()
                replies = await asyncio.gather(
                    allow(client, shown, scope="once"), allow(client, shown, scope="task")
                )
                outcome = await pending

                assert [type(reply) for reply in replies].count(PromptAnswered) == 1
                assert [type(reply) for reply in replies].count(Refused) == 1
                assert outcome.granted
                assert len(rig.audit_of(GrantDecision)) == 1
                assert rig.rows("select count(*) from grants") == [(1,)]

        run(scenario)

    def test_a_replay_cannot_answer_the_next_prompt_for_the_same_request(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)
                first = await asked(client, task, rig)
                old = await shell.next_prompt()
                await deny(client, old)
                await first

                second = await asked(client, task, rig)
                new = await shell.next_prompt()
                replay = await allow(client, old)  # the old id, while a new prompt is waiting

                assert new.prompt != old.prompt
                assert isinstance(replay, Refused)
                assert not second.done()
                await deny(client, new)
                await second

        run(scenario)

    def test_an_id_the_broker_never_sent_is_refused(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                client = await rig.client(handler=FakeShell())

                refused = await answer(
                    client, PromptId.new(), AllowDecision(kind="allow", scope="task", picked=None)
                )

                assert isinstance(refused, Refused)
                assert refused.reason == "unknown_prompt"

        run(scenario)


class TestWhichEndServesWhat:
    def test_the_broker_does_not_serve_the_requests_of_the_shell_end(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                from tumbleweed.ipc.client import connect

                raw = await connect(rig.socket, FakeShell())  # no role: any request may be sent
                try:
                    show = ShowPrompt(
                        kind="show_prompt",
                        prompt=PromptId.new(),
                        task=TaskId.new(),
                        tool=None,
                        access=NetworkAccess(kind="net.full"),
                        scopes=("task",),
                        reason="x",
                        expires_in_ms=1000,
                    )
                    with pytest.raises(RemoteError) as caught:
                        await raw.call(show, PromptAnswered)
                    assert caught.value.code is ErrorCode.UNSUPPORTED_REQUEST
                finally:
                    await raw.close()

        run(scenario)

    def test_a_shell_end_does_not_send_what_only_the_shell_serves(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                client = await rig.client(handler=FakeShell())
                show = ShowPrompt(
                    kind="show_prompt",
                    prompt=PromptId.new(),
                    task=TaskId.new(),
                    tool=None,
                    access=NetworkAccess(kind="net.full"),
                    scopes=("task",),
                    reason="x",
                    expires_in_ms=1000,
                )
                with pytest.raises(TypeError):
                    await client.connection.call(show, PromptAnswered)

        run(scenario)


class TestNoAnswerIsADenial:
    def test_the_brokers_timeout_denies_and_takes_the_prompt_down_in_the_shell(self) -> None:
        async def scenario() -> None:
            async with running(
                ask_shell=True,
                config=lambda c: replace_config(c, prompt_timeout=timedelta(milliseconds=200)),
            ) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = await asked(client, task, rig)
                shown = await shell.next_prompt()
                outcome = await pending
                await shell.wait_closed()
                late = await allow(client, shown)  # a user who answered after all

                assert not outcome.granted
                assert outcome.message == "the user did not answer in time"
                assert shell.closed == [shown.prompt]
                assert isinstance(late, Refused)  # the prompt is gone: the answer is no grant
                assert rig.audit_of(GrantDecision)[0].reason == "prompt_timeout"

        run(scenario)

    def test_a_users_deny_is_the_only_denial_that_says_the_user_did_not_allow_it(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = await asked(client, task, rig)
                await deny(client, await shell.next_prompt())

                assert (await pending).message == "the user did not allow it"

        run(scenario)

    def test_the_shells_own_timeout_denial_is_a_denial_with_its_reason(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = await asked(client, task, rig)
                await deny(client, await shell.next_prompt(), reason="prompt_timeout")
                outcome = await pending

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[0].reason == "prompt_timeout"

        run(scenario)

    def test_a_shell_that_goes_away_leaves_no_prompt_and_no_grant(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = await asked(client, task, rig)
                shown = await shell.next_prompt()
                await client.close()
                with pytest.raises(ConnectionClosedError):
                    await pending

                # Its tasks end with it, and the prompt cannot be answered by anyone any more.
                await until(lambda: bool(rig.audit_of(TaskEnded)))
                survivor = await rig.client(handler=FakeShell())
                refused = await allow(survivor, shown)
                assert isinstance(refused, Refused)
                assert rig.rows("select count(*) from grants") == [(0,)]

        run(scenario)

    def test_a_client_that_serves_no_prompts_is_denied_without_waiting(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                client = await rig.client()  # `tumbleweed-tools` is one: no handler
                task = await task_of(client)

                outcome = await client.request_read(task, RunId.new(), photos(rig), "x")

                assert not outcome.granted
                assert outcome.message == "there is nobody to ask here"
                assert rig.audit_of(GrantDecision)[0].reason == "no_prompt_ui"

        run(scenario)

    def test_a_shell_that_cannot_show_another_prompt_denies_it(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                shell.busy = True
                client = await rig.client(handler=shell)
                task = await task_of(client)

                outcome = await client.request_read(task, RunId.new(), photos(rig), "x")

                assert not outcome.granted
                assert "could not be asked" in outcome.message  # not "the user did not allow it"
                assert rig.audit_of(GrantDecision)[0].reason == "prompt_busy"

        run(scenario)

    def test_a_task_of_one_connection_is_never_asked_on_another(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                owner_shell, other_shell = FakeShell(), FakeShell()
                owner = await rig.client(handler=owner_shell)
                await rig.client(handler=other_shell)
                task = await task_of(owner)

                pending = await asked(owner, task, rig)
                shown = await owner_shell.next_prompt()
                await deny(owner, shown)
                await pending

                assert other_shell.shown == []

        run(scenario)


class TestTheRateLimit:
    def test_the_sixth_prompt_in_ten_minutes_is_denied_without_asking_and_says_why(self) -> None:
        async def scenario() -> None:
            async with running(
                ask_shell=True, config=lambda c: replace_config(c, prompts_per_window=5)
            ) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)
                for _ in range(5):
                    pending = await asked(client, task, rig)
                    await deny(client, await shell.next_prompt())
                    await pending

                outcome = await client.request_read(task, RunId.new(), photos(rig), "again")

                assert not outcome.granted
                assert "too many permission requests" in outcome.message
                assert len(shell.shown) == 5  # the sixth never reached the user
                assert rig.audit_of(GrantDecision)[-1].reason == "rate_limited"

        run(scenario)

    def test_the_limit_is_per_task_and_the_window_slides(self) -> None:
        async def scenario() -> None:
            async with running(
                ask_shell=True, config=lambda c: replace_config(c, prompts_per_window=2)
            ) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)
                for _ in range(2):
                    pending = await asked(client, task, rig)
                    await deny(client, await shell.next_prompt())
                    await pending
                limited = await client.request_read(task, RunId.new(), photos(rig), "x")
                other = await task_of(client)  # another task has its own allowance
                pending = await asked(client, other, rig)
                await deny(client, await shell.next_prompt())
                await pending

                rig.clock.advance(timedelta(minutes=10, seconds=1))
                later = await asked(client, task, rig)
                await deny(client, await shell.next_prompt())
                await later

                assert not limited.granted
                assert "too many" in limited.message
                assert len(shell.shown) == 4

        run(scenario)

    def test_pickers_count_against_the_same_limit(self) -> None:
        async def scenario() -> None:
            async with running(
                ask_shell=True, config=lambda c: replace_config(c, prompts_per_window=1)
            ) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)
                pending = asyncio.create_task(
                    client.request_pick(task, RunId.new(), "file", "first")
                )
                await deny(client, await shell.next_prompt())
                await pending

                outcome = await client.request_pick(task, RunId.new(), "file", "second")

                assert "too many" in outcome.message
                assert len(shell.shown) == 1

        run(scenario)


class TestThePicker:
    def test_the_users_choice_becomes_the_grant_at_the_chosen_scope(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)
                chosen = rig.home / "Documents"

                pending = asyncio.create_task(
                    client.request_pick(task, RunId.new(), "folder", "my documents")
                )
                shown = await shell.next_prompt()
                await allow(client, shown, scope="task", picked=str(chosen))
                outcome = await pending

                assert outcome.granted
                assert outcome.scope == "task"
                (entry,) = rig.audit_of(GrantDecision)
                assert entry.outcome == "granted"
                assert entry.path == str(chosen)
                assert entry.capability == "fs.read"
                ((record, scope),) = rig.rows("select record, scope from grants")
                assert isinstance(record, str)
                assert json.loads(record)["origin"] == "picker"
                assert scope == "task"
                # And the grant works.
                listing = await client.list_dir(task, RunId.new(), str(chosen))
                assert [e.name for e in listing.entries] == ["notes.txt"]

        run(scenario)

    def test_a_protected_place_the_user_picks_is_still_refused(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = asyncio.create_task(client.request_pick(task, RunId.new(), "folder", "x"))
                shown = await shell.next_prompt()
                await allow(client, shown, scope="task", picked=str(rig.home / ".ssh"))
                outcome = await pending

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[0].reason == "static_deny"
                assert rig.rows("select count(*) from grants") == [(0,)]

        run(scenario)

    def test_a_protected_place_that_is_picked_is_in_the_audit_with_its_path(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = asyncio.create_task(client.request_pick(task, RunId.new(), "folder", "x"))
                await allow(client, await shell.next_prompt(), picked=str(rig.home / ".ssh"))
                await pending

                (entry,) = rig.audit_of(GrantDecision)
                assert entry.path == str(rig.home / ".ssh")  # what the user picked, resolved

        run(scenario)

    def test_an_error_after_the_choice_is_resolved_is_audited_with_its_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async def broken(*_args: object) -> object:
                raise InvalidValueError("something about the grant")

            monkeypatch.setattr(BrokerService, "_issue_after_prompt", broken)
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)
                chosen = rig.home / "Documents"

                pending = asyncio.create_task(client.request_pick(task, RunId.new(), "folder", "x"))
                await allow(client, await shell.next_prompt(), picked=str(chosen))
                with pytest.raises(BrokerRefused):
                    await pending

                (entry,) = rig.audit_of(GrantDecision)
                assert entry.path == str(chosen)  # what the user picked, resolved
                assert entry.outcome == "denied"

        run(scenario)

    def test_a_choice_that_cannot_be_resolved_at_all_is_audited_without_a_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stated in D25: nothing was resolved, so nothing canonical can be recorded, and the raw
        text of a choice is not copied into the log."""

        async def scenario() -> None:
            async def hung(*_args: object) -> object:
                raise FsTimeoutError("the file system did not answer")

            monkeypatch.setattr(BrokerService, "_resolve", hung)
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = asyncio.create_task(client.request_pick(task, RunId.new(), "file", "x"))
                await allow(client, await shell.next_prompt(), picked=str(rig.home / "Documents"))
                with pytest.raises(BrokerRefused):
                    await pending

                (entry,) = rig.audit_of(GrantDecision)
                assert entry.path is None
                assert entry.outcome == "denied"

        run(scenario)

    def test_a_choice_that_is_not_there_is_audited_with_the_path_it_resolved_to(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = asyncio.create_task(client.request_pick(task, RunId.new(), "file", "x"))
                await allow(client, await shell.next_prompt(), picked=str(rig.home / "nowhere"))
                await pending

                (entry,) = rig.audit_of(GrantDecision)
                assert entry.path == str(rig.home / "nowhere")
                assert (entry.outcome, entry.reason) == ("denied", "ungrantable")

        run(scenario)

    def test_a_task_that_has_the_network_is_not_asked_to_pick_a_file(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)
                pending = asyncio.create_task(client.request_network(task, RunId.new(), "net"))
                await allow(client, await shell.next_prompt(), scope="task")
                assert (await pending).granted

                outcome = await client.request_pick(task, RunId.new(), "folder", "files too")

                assert not outcome.granted
                assert len(shell.shown) == 1  # nobody was asked to choose what would be refused
                assert rig.audit_of(GrantDecision)[-1].reason == "taint_conflict"

        run(scenario)

    def test_a_pick_prompt_answered_without_a_choice_is_a_denial(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = asyncio.create_task(client.request_pick(task, RunId.new(), "file", "x"))
                await allow(client, await shell.next_prompt(), scope="task", picked=None)
                outcome = await pending

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[0].reason == "prompt_failed"

        run(scenario)

    def test_a_capability_prompt_cannot_be_answered_with_a_path_of_the_shells_choosing(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = await asked(client, task, rig)
                await allow(
                    client,
                    await shell.next_prompt(),
                    scope="task",
                    picked=str(rig.home / "Documents"),
                )
                outcome = await pending

                assert not outcome.granted  # the user was asked about Photos, not Documents
                assert rig.rows("select count(*) from grants") == [(0,)]

        run(scenario)

    def test_a_path_that_is_not_there_is_refused_not_granted(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)

                pending = asyncio.create_task(client.request_pick(task, RunId.new(), "file", "x"))
                await allow(
                    client,
                    await shell.next_prompt(),
                    scope="once",
                    picked=str(rig.home / "no-such-file"),
                )
                outcome = await pending
                assert not outcome.granted
                assert "only an existing" in outcome.message
                assert rig.rows("select count(*) from grants") == [(0,)]

        run(scenario)


class TestTheAllowanceFollowsTheLineage:
    def test_a_task_that_replaces_another_has_what_is_left_of_its_allowance(self) -> None:
        async def scenario() -> None:
            async with running(
                ask_shell=True, config=lambda c: replace_config(c, prompts_per_window=2)
            ) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                first = await task_of(client)
                for _ in range(2):
                    pending = await asked(client, first, rig)
                    await deny(client, await shell.next_prompt())
                    await pending

                second = (await client.start_task(previous=first)).task
                fresh = (await client.start_task(previous=second, fresh_context=True)).task
                outcome = await client.request_read(fresh, RunId.new(), photos(rig), "x")

                assert "too many permission requests" in outcome.message
                assert len(shell.shown) == 2  # the model got no new allowance by starting tasks

        run(scenario)

    def test_tasks_started_from_nothing_share_the_ceiling_of_their_connection(self) -> None:
        async def scenario() -> None:
            async with running(
                ask_shell=True,
                config=lambda c: replace_config(
                    c, prompts_per_window=2, owner_prompts_per_window=3
                ),
            ) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                one, two = await task_of(client), await task_of(client)
                for task in (one, one, two):
                    pending = await asked(client, task, rig)
                    await deny(client, await shell.next_prompt())
                    await pending

                outcome = await client.request_read(two, RunId.new(), photos(rig), "x")

                assert (
                    "too many permission requests in all" in outcome.message
                )  # not "for this task"
                assert "this task" not in outcome.message
                assert rig.audit_of(GrantDecision)[-1].reason == "rate_limited_connection"
                assert len(shell.shown) == 3

        run(scenario)

    def test_ending_the_predecessor_first_does_not_start_the_allowance_over(self) -> None:
        async def scenario() -> None:
            async with running(
                ask_shell=True, config=lambda c: replace_config(c, prompts_per_window=2)
            ) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                first = await task_of(client)
                for _ in range(2):
                    pending = await asked(client, first, rig)
                    await deny(client, await shell.next_prompt())
                    await pending
                await client.end_task(first)  # ended first, then the successor names it

                second = (await client.start_task(previous=first)).task
                outcome = await client.request_read(second, RunId.new(), photos(rig), "x")
                rig.clock.advance(timedelta(minutes=10))  # and the memory of it is bounded
                third = (await client.start_task(previous=first)).task
                later = await asked(client, third, rig)
                await deny(client, await shell.next_prompt())
                await later

                assert "too many permission requests for this task" in outcome.message
                assert len(shell.shown) == 3

        run(scenario)

    def test_nobody_inherits_the_allowance_of_another_connections_task(self) -> None:
        async def scenario() -> None:
            async with running(
                ask_shell=True, config=lambda c: replace_config(c, prompts_per_window=1)
            ) as rig:
                shell, other_shell = FakeShell(), FakeShell()
                client = await rig.client(handler=shell)
                other = await rig.client(handler=other_shell)
                first = await task_of(client)
                pending = await asked(client, first, rig)
                await deny(client, await shell.next_prompt())
                await pending
                await client.end_task(first)

                mine = (await other.start_task(previous=first)).task  # names a stranger's task
                pending = await asked(other, mine, rig)
                await deny(other, await other_shell.next_prompt())
                await pending

                assert len(other_shell.shown) == 1

        run(scenario)

    def test_one_connections_churn_evicts_nothing_of_anothers_and_eviction_is_oldest_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            monkeypatch.setattr(service_module, "_MAX_ENDED_PER_OWNER", 2)
            async with running(
                ask_shell=True, config=lambda c: replace_config(c, prompts_per_window=1)
            ) as rig:
                shell, other_shell = FakeShell(), FakeShell()
                client = await rig.client(handler=shell)
                other = await rig.client(handler=other_shell)

                async def used_and_ended(who: BrokerClient, shown: FakeShell) -> TaskId:
                    task = await task_of(who)
                    pending = await asked(who, task, rig)
                    await deny(who, await shown.next_prompt())
                    await pending
                    await who.end_task(task)
                    return task

                theirs = await used_and_ended(other, other_shell)
                first, second, third = [await used_and_ended(client, shell) for _ in range(3)]
                shown_before = len(shell.shown)

                # `first` was evicted (the oldest of two kept): a successor starts a fresh one ...
                fresh = (await client.start_task(previous=first)).task
                pending = await asked(client, fresh, rig)
                await deny(client, await shell.next_prompt())
                await pending
                # ... `third` is kept, and so is the other connection's, whatever `client` churned.
                kept = (await client.start_task(previous=third)).task
                held = await client.request_read(kept, RunId.new(), photos(rig), "x")
                mine = (await other.start_task(previous=theirs)).task
                other_held = await other.request_read(mine, RunId.new(), photos(rig), "x")

                assert second != first
                assert len(shell.shown) == shown_before + 1  # only the evicted lineage asked
                assert "too many permission requests for this task" in held.message
                assert "too many permission requests for this task" in other_held.message

        run(scenario)

    def test_kept_allowances_older_than_the_window_are_dropped_when_the_next_task_ends(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                client = await rig.client(handler=FakeShell())
                old = await task_of(client)
                await client.end_task(old)
                service = rig.daemon.service
                assert list(service._ended_prompts[next(iter(service._ended_prompts))]) == [old]  # noqa: SLF001

                rig.clock.advance(timedelta(minutes=10, seconds=1))
                newer = await task_of(client)
                await client.end_task(newer)

                (kept,) = service._ended_prompts.values()  # noqa: SLF001
                assert list(kept) == [newer]  # the old one aged out: memory is bounded by time too

        run(scenario)

    def test_a_connections_kept_allowances_go_when_it_closes(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                client = await rig.client(handler=FakeShell())
                task = await task_of(client)
                await client.end_task(task)
                service = rig.daemon.service
                assert service._ended_prompts  # noqa: SLF001  (the point of the test)

                await client.close()
                await until(lambda: not service._ended_prompts)  # noqa: SLF001

        run(scenario)

    def test_another_connection_has_a_ceiling_of_its_own(self) -> None:
        async def scenario() -> None:
            async with running(
                ask_shell=True,
                config=lambda c: replace_config(
                    c, prompts_per_window=2, owner_prompts_per_window=2
                ),
            ) as rig:
                shell, other_shell = FakeShell(), FakeShell()
                client = await rig.client(handler=shell)
                other = await rig.client(handler=other_shell)
                task, other_task = await task_of(client), await task_of(other)
                for _ in range(2):
                    pending = await asked(client, task, rig)
                    await deny(client, await shell.next_prompt())
                    await pending

                pending = await asked(other, other_task, rig)
                await deny(other, await other_shell.next_prompt())
                await pending

                assert len(other_shell.shown) == 1

        run(scenario)

    def test_the_window_is_over_exactly_when_it_has_passed_and_a_wall_clock_jump_is_no_time(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running(
                ask_shell=True, config=lambda c: replace_config(c, prompts_per_window=1)
            ) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)
                pending = await asked(client, task, rig)
                await deny(client, await shell.next_prompt())
                await pending

                rig.clock.jump_wall_clock(timedelta(hours=1))  # not ten minutes of the broker's
                jumped = await client.request_read(task, RunId.new(), photos(rig), "x")
                rig.clock.advance(timedelta(minutes=10) - timedelta(seconds=1))
                early = await client.request_read(task, RunId.new(), photos(rig), "x")
                rig.clock.advance(timedelta(seconds=1))
                again = await asked(client, task, rig)
                await deny(client, await shell.next_prompt())
                await again

                assert "too many" in jumped.message
                assert "too many" in early.message
                assert len(shell.shown) == 2

        run(scenario)


class TestTheShellsRoom:
    def test_one_connection_cannot_fill_the_shells_queue(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task = await task_of(client)
                waiting = [await asked(client, task, rig) for _ in range(8)]
                for _ in range(8):
                    await shell.next_prompt()

                ninth = await client.request_read(task, RunId.new(), photos(rig), "x")

                assert "too many questions" in ninth.message
                assert len(shell.shown) == 8
                for pending in waiting:
                    pending.cancel()

        run(scenario)
