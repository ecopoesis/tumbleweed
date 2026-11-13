"""A reply whose context was cut after the model began to write belongs to no task (D26 (3)).

Its text derives from the old context and from the new one, so neither may see it. It is tagged
`HIDDEN_TASK`: in no context set, never a restore head, still shown in the chat.
"""
# ruff: noqa: PT018  (compound assertions read better here)

import asyncio
import time

import pytest

from tests.agent.support import make_rig
from tests.agent.test_tasks import PHOTO, all_text, call, current, say, world
from tumbleweed.agent.fake import FakeTurn
from tumbleweed.agent.responder import HIDDEN_TASK
from tumbleweed.agent.tasks import TaskError, TaskSession
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.core.ids import MessageId, TaskId
from tumbleweed.shell.viewmodel.transcript import MessageState, Role, TranscriptMessage


def restored(ghost: TaskId) -> tuple[TranscriptMessage, ...]:
    return tuple(
        TranscriptMessage(
            id=MessageId.new(), role=role, text=text, state=MessageState.COMPLETE, task=ghost
        )
        for role, text in ((Role.USER, "old q"), (Role.ASSISTANT, "old a OLDSECRET"))
    )


def test_a_reply_that_started_writing_before_the_broker_did_not_know_the_task_is_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with world() as w:
        ghost = TaskId.new()
        w.tasks.restore(ghost)
        real = TaskSession.ensure
        calls = {"n": 0}

        async def unreachable_first(self: TaskSession) -> TaskId:
            calls["n"] += 1
            if calls["n"] == 1:  # the check at the start of the reply: the broker is away
                raise TaskError("The broker is not reachable right now.")
            return await real(self)

        monkeypatch.setattr(TaskSession, "ensure", unreachable_first)
        script = [
            FakeTurn(chunks=("you said OLDSECRET. ",), tool_calls=(call("list_workspace"),)),
            say("done"),
            say("next"),
            FakeTurn(tool_calls=(call("start_task", title="clean", fresh_context=True),)),
            say("in the clean task"),
            say("and again"),
        ]
        with make_rig(script, registry=w.registry, dispatcher=w.dispatcher, tasks=w.tasks) as rig:
            rig.transcript.replace(restored(ghost))

            rig.send_and_wait("new")  # the model writes OLDSECRET, then the broker says unknown

            reply = rig.transcript.messages[-1]
            assert reply.task == HIDDEN_TASK
            assert reply.text.startswith("you said OLDSECRET")  # still shown in the chat
            rig.send_and_wait("next")
            rig.send_and_wait("start fresh")
            rig.send_and_wait("what do you know?")
            assert "OLDSECRET" not in all_text(rig.provider.requests[-1].messages)


def test_a_reply_cut_before_the_model_wrote_anything_belongs_to_the_task_it_ended_in() -> None:
    # The check of a restored chat that finds the broker does not know the task happens before the
    # first model call; the reply is a reply of the clean task, not of nowhere.
    with world() as w:
        ghost = TaskId.new()
        w.tasks.restore(ghost)
        with make_rig(
            [say("a"), say("b")], registry=w.registry, dispatcher=w.dispatcher, tasks=w.tasks
        ) as rig:
            rig.transcript.replace(restored(ghost))

            rig.send_and_wait("new")

            reply = rig.transcript.messages[-1]
            assert reply.task == current(w) and reply.task != HIDDEN_TASK
            assert "OLDSECRET" not in all_text(rig.provider.requests[0].messages)
            rig.send_and_wait("again")
            assert "a" in all_text(rig.provider.requests[1].messages)  # the clean task sees it


def test_a_lost_connection_then_an_unknown_task_in_one_reply_hides_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with world() as w:
        w.tasks.begin_reply()
        w.tasks.anchor_reply()
        first = asyncio.run(w.tasks.ensure())

        async def hang_up(client: BrokerClient) -> None:
            await client.connection.close()

        w.link.call_blocking(hang_up, deadline=5.0)
        for _ in range(100):
            if current(w) is None:
                break
            time.sleep(0.05)
        real = w.tasks._start_on_broker  # noqa: SLF001

        async def forgets(previous: TaskId | None, fresh: bool) -> TaskId:
            if previous is not None:
                raise BrokerRefused("unknown_task", "no such task")
            return await real(previous, fresh)

        monkeypatch.setattr(w.tasks, "_start_on_broker", forgets)
        clean = asyncio.run(w.tasks.ensure())  # the next tool call of the same reply

        tag = w.tasks.reply_tag()
        assert tag == HIDDEN_TASK
        assert not w.tasks.visible(tag) and not w.tasks.visible(first)
        assert w.tasks.visible(clean)


def test_a_fresh_start_in_the_middle_of_a_reply_hides_it_from_every_task_and_from_a_restore() -> (
    None
):
    with world() as w:
        w.allow("task")
        w.allow("task")
        photos = str(w.home / "Photos")
        script = [
            FakeTurn(
                chunks=("Starting. ",),
                tool_calls=(call("start_task", title="B", fresh_context=True),),
            ),
            FakeTurn(tool_calls=(call("request_access", kind="read", path=photos, reason="r"),)),
            FakeTurn(tool_calls=(call("read_file", path=str(w.home / PHOTO)),)),
            say("The file says jpeg."),
        ]
        with make_rig(script, registry=w.registry, dispatcher=w.dispatcher, tasks=w.tasks) as rig:
            rig.send_and_wait("go")

            reply = rig.transcript.messages[-1]
            assert reply.task == HIDDEN_TASK
            assert "jpeg" in reply.text
            task_b = current(w)
            assert task_b is not None
            assert not w.tasks.visible(reply.task)
            w.tasks.restore(TaskId.new())  # a restart: the head is whatever the chat tagged last
            assert not w.tasks.visible(HIDDEN_TASK)
