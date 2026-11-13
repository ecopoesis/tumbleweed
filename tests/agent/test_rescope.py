"""What the model is sent after a task starts in the middle of a reply (D26 (4), round-1 M1, M3).

A real broker, the real loop and tools, the fake provider. The text of anything read or written in
the earlier, possibly tainted, context must not reach the model again after a fresh start, and
nothing written in that context may run in the new, clean task.
"""
# ruff: noqa: PT018  (compound assertions read better here)

from itertools import pairwise

import pytest

from tests.agent.support import make_rig
from tests.agent.test_tasks import PHOTO, all_text, call, results, say, world
from tumbleweed.agent.canvas_events import CanvasEvent, CanvasTurn, StateSnapshot
from tumbleweed.agent.fake import FakeTurn
from tumbleweed.agent.llm import LlmMessage, TextBlock, ToolResultBlock, ToolUseBlock
from tumbleweed.agent.loop import EVENT_NOTE, NOT_RUN
from tumbleweed.core.ids import MessageId, ToolInstanceId
from tumbleweed.shell.viewmodel.transcript import MessageState, Role, TranscriptMessage
from tumbleweed.ui.spec.names import HandlerName


def well_formed(messages: tuple[LlmMessage, ...]) -> None:
    """Every tool result answers a tool use of the message before it, and nothing is orphaned."""
    assert messages[0].role == "user"
    for before, message in pairwise(messages):
        ids = {block.tool_use_id for block in message.content if isinstance(block, ToolResultBlock)}
        if ids:
            uses = {block.id for block in before.content if isinstance(block, ToolUseBlock)}
            assert ids == uses, "a tool result without its tool use"
        if message.role == before.role:
            raise AssertionError("two messages of one role in a row")


def history_of(turns: int) -> tuple[TranscriptMessage, ...]:
    messages: list[TranscriptMessage] = []
    for number in range(turns):
        for role, text in ((Role.USER, f"old question {number}"), (Role.ASSISTANT, "old answer")):
            messages.append(
                TranscriptMessage(
                    id=MessageId.new(), role=role, text=text, state=MessageState.COMPLETE
                )
            )
    return tuple(messages)


@pytest.mark.parametrize("turns", [0, 1, 2, 3])
def test_two_fresh_starts_in_one_reply_leave_nothing_of_the_tasks_between(turns: int) -> None:
    with world() as w:
        photos = str(w.home / "Photos")
        w.allow("task")
        w.allow("task")
        script = [
            FakeTurn(tool_calls=(call("request_access", kind="read", path=photos, reason="r"),)),
            FakeTurn(tool_calls=(call("start_task", title="A", fresh_context=True),)),
            FakeTurn(tool_calls=(call("request_access", kind="read", path=photos, reason="r"),)),
            FakeTurn(tool_calls=(call("read_file", path=str(w.home / PHOTO)),)),
            FakeTurn(tool_calls=(call("start_task", title="B", fresh_context=True),)),
            say("done"),
        ]
        with make_rig(script, registry=w.registry, dispatcher=w.dispatcher, tasks=w.tasks) as rig:
            rig.transcript.replace(history_of(turns))

            rig.send_and_wait("go")

            assert "jpeg" in all_text(rig.provider.requests[4].messages)  # task A did read it
            last = rig.provider.requests[5].messages
            assert "jpeg" not in all_text(last)
            assert "old question" not in all_text(last)
            assert last[0].content == (TextBlock(text="go"),)  # the user's own message stays
            well_formed(last)


def test_what_is_written_before_a_fresh_start_does_not_run_after_it_or_come_back() -> None:
    with world() as w:
        w.allow("task")
        script = [
            FakeTurn(
                tool_calls=(
                    call("request_access", kind="read", path=str(w.home / "Photos"), reason="r"),
                )
            ),
            FakeTurn(tool_calls=(call("read_file", path=str(w.home / PHOTO)),)),
            FakeTurn(
                tool_calls=(
                    call("start_task", title="LEAKTITLE", fresh_context=True),
                    call("request_access", kind="network", reason="LEAKREASON"),
                    call("list_dir", path="/LEAKPATH"),
                )
            ),
            say("done"),
        ]
        with make_rig(script, registry=w.registry, dispatcher=w.dispatcher, tasks=w.tasks) as rig:
            rig.send_and_wait("go")

            started, network, listing = results(rig, 3)
            assert "Started a new task" in started.content
            assert network.content == NOT_RUN and network.is_error
            assert listing.content == NOT_RUN
            assert len(w.broker.rig.prompts.asked) == 1  # the network was never asked for
            text = all_text(rig.provider.requests[3].messages)
            for leaked in ("LEAKTITLE", "LEAKREASON", "LEAKPATH", "jpeg"):
                assert leaked not in text
            well_formed(rig.provider.requests[3].messages)


def test_a_canvas_event_turn_does_not_survive_a_fresh_start() -> None:
    with world() as w:
        script = [FakeTurn(tool_calls=(call("start_task", title="x", fresh_context=True),)), say()]
        with make_rig(script, registry=w.registry, dispatcher=w.dispatcher, tasks=w.tasks) as rig:
            turn = CanvasTurn(
                event=CanvasEvent(
                    instance=ToolInstanceId.new(),
                    handler=HandlerName("ask"),
                    component="button",
                    event="click",
                    bind=None,
                    note=None,
                    state=StateSnapshot(values={"q": "SECRETSTATE"}),
                )
            )
            rig.controller.start_canvas_turn(turn)
            rig.dispatcher.run_until(lambda: not rig.controller.busy)

            assert "SECRETSTATE" in all_text(rig.provider.requests[0].messages)
            after = rig.provider.requests[1].messages
            assert "SECRETSTATE" not in all_text(after)
            assert after[0].content == (TextBlock(text=EVENT_NOTE),)
            well_formed(after)
