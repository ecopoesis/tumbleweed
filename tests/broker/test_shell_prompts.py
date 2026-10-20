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
from tumbleweed.broker.audit import GrantDecision, TaskEnded
from tumbleweed.broker.client import BrokerClient
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
