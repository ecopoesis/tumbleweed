"""A task rides on every message and turn (D26, #80): from the reply that ended, through the
transcript and the chat history, to the turns the model is sent."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.history.support import closing_every_store
from tests.shell.fakes import ManualResponder
from tests.shell.test_session import Rig as SessionRig
from tumbleweed.agent.responder import ReplyDelta, ReplyFailed, ReplyFinished
from tumbleweed.core.ids import MessageId, TaskId
from tumbleweed.shell.viewmodel.chat import ChatController, context_turns
from tumbleweed.shell.viewmodel.transcript import MessageState, Role, Transcript, TranscriptMessage


@pytest.fixture(autouse=True)
def _close_every_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # A store a test leaves open is finalised later, and that is an error on Python 3.13.
    with closing_every_store(monkeypatch):
        yield


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


class _Lineage:
    def __init__(self) -> None:
        self.restored: list[TaskId | None] = []
        self.ended = 0

    def restore(self, head: TaskId | None) -> None:
        self.restored.append(head)

    def end_session(self) -> None:
        self.ended += 1


def test_a_restored_chat_tells_the_tasks_the_newest_tagged_task_and_a_new_session_ends_it(
    tmp_path: Path,
) -> None:
    from tests.history.support import history_directory
    from tumbleweed.history.store import HistoryStore
    from tumbleweed.shell.viewmodel.session import ChatSession

    older, newest = TaskId.new(), TaskId.new()
    first = SessionRig(tmp_path)
    for task in (older, newest):
        first.controller.send("q")
        first.responder.sink.emit(ReplyDelta(text="a"))
        first.responder.sink.emit(ReplyFinished(task=task))
    first.controller.send("untagged tail")
    first.responder.sink.emit(ReplyDelta(text="b"))
    first.responder.sink.emit(ReplyFinished())
    first.session.close()

    transcript, responder = Transcript(), ManualResponder()
    controller = ChatController(transcript, responder)
    lineage = _Lineage()
    directory = history_directory(tmp_path)
    session = ChatSession(transcript, controller, lambda: HistoryStore.open(directory), lineage)
    session.start()

    assert lineage.restored == [newest]
    session.new_session()
    assert lineage.ended == 1
    session.close()


def test_a_hidden_reply_is_never_the_task_a_restart_inherits_from(tmp_path: Path) -> None:
    from tests.history.support import history_directory
    from tumbleweed.agent.responder import HIDDEN_TASK
    from tumbleweed.history.store import HistoryStore
    from tumbleweed.shell.viewmodel.session import ChatSession

    older = TaskId.new()
    first = SessionRig(tmp_path)
    for task in (older, HIDDEN_TASK):
        first.controller.send("q")
        first.responder.sink.emit(ReplyDelta(text="a"))
        first.responder.sink.emit(ReplyFinished(task=task))
    first.session.close()

    transcript, responder = Transcript(), ManualResponder()
    lineage = _Lineage()
    directory = history_directory(tmp_path)
    session = ChatSession(
        transcript,
        ChatController(transcript, responder),
        lambda: HistoryStore.open(directory),
        lineage,
    )
    session.start()

    assert lineage.restored == [older]
    assert [m.task for m in transcript.messages if m.role is Role.ASSISTANT] == [older, HIDDEN_TASK]
    session.close()
