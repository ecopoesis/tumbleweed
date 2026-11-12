"""A task rides on every message and turn (D26, #80): from the reply that ended, through the
transcript and the chat history, to the turns the model is sent."""

from pathlib import Path

from tests.shell.fakes import ManualResponder
from tests.shell.test_session import Rig as SessionRig
from tumbleweed.agent.responder import ReplyDelta, ReplyFailed, ReplyFinished
from tumbleweed.core.ids import MessageId, TaskId
from tumbleweed.shell.viewmodel.chat import ChatController, context_turns
from tumbleweed.shell.viewmodel.transcript import MessageState, Role, Transcript, TranscriptMessage


def test_a_finished_reply_is_tagged_with_the_task_the_responder_names() -> None:
    task = TaskId.new()
    transcript, responder = Transcript(), ManualResponder()
    controller = ChatController(transcript, responder)

    controller.send("hello")
    responder.sink.emit(ReplyDelta(text="hi"))
    responder.sink.emit(ReplyFinished(task=task))

    assert [m.task for m in transcript.messages] == [None, task]
    assert context_turns(transcript.messages)[0].task == task


def test_a_user_message_is_tagged_with_the_current_task() -> None:
    task = TaskId.new()
    transcript, responder = Transcript(), ManualResponder()
    controller = ChatController(transcript, responder, lambda: task)

    controller.send("hello")

    assert transcript.messages[0].task == task


def test_a_failed_reply_keeps_the_task_it_names_and_a_cancelled_one_the_current_task() -> None:
    named, current = TaskId.new(), TaskId.new()
    transcript, responder = Transcript(), ManualResponder()
    controller = ChatController(transcript, responder, lambda: current)

    controller.send("one")
    responder.sink.emit(ReplyFailed(reason="no", task=named))
    controller.send("two")
    controller.cancel()

    assert [m.task for m in transcript.messages if m.role is Role.ASSISTANT] == [named, current]


def test_the_task_is_stored_and_restored_with_the_message(tmp_path: Path) -> None:
    task = TaskId.new()
    first = SessionRig(tmp_path)
    first.controller.send("question")
    first.responder.sink.emit(ReplyDelta(text="answer"))
    first.responder.sink.emit(ReplyFinished(task=task))
    first.session.close()

    second = SessionRig(tmp_path)

    assert [m.task for m in second.transcript.messages] == [None, task]


def test_a_turn_without_a_task_has_none() -> None:
    message = TranscriptMessage(
        id=MessageId.new(), role=Role.USER, text="q", state=MessageState.COMPLETE
    )
    assert message.task is None
