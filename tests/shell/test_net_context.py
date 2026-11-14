"""The model context of a net-tainted task holds no turn of an fs-tainted one, also after a
restart (S22, I12, #157): real stored history, restored into a real `TaskSession`."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.agent.test_tasks import world
from tests.history.support import closing_every_store, history_directory
from tests.shell.fakes import ManualResponder
from tumbleweed.agent.responder import HIDDEN_TASK, ReplyDelta, ReplyFinished
from tumbleweed.core.ids import TaskId
from tumbleweed.history.store import HistoryStore
from tumbleweed.shell.viewmodel.chat import ChatController, context_turns
from tumbleweed.shell.viewmodel.session import ChatSession
from tumbleweed.shell.viewmodel.transcript import Transcript


@pytest.fixture(autouse=True)
def _close_every_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with closing_every_store(monkeypatch):
        yield


def store_turns(tmp_path: Path, turns: list[tuple[str, TaskId]], current: list[TaskId]) -> None:
    """Write a chat to the real history: each turn is a question sent while `current[0]` was
    the task and a reply tagged with the task it names."""
    transcript, responder = Transcript(), ManualResponder()
    controller = ChatController(transcript, responder, lambda: current[0])
    directory = history_directory(tmp_path)
    session = ChatSession(transcript, controller, lambda: HistoryStore.open(directory))
    session.start()
    for text, task in turns:
        controller.send(f"question about {text}")
        responder.sink.emit(ReplyDelta(text=f"answer about {text}"))
        responder.sink.emit(ReplyFinished(task=task))
    session.close()


def model_sees(tmp_path: Path) -> list[str]:
    """What a restart would send the model: the stored turns that survive the task filter."""
    with world() as w:
        transcript = Transcript()
        controller = ChatController(transcript, ManualResponder())
        directory = history_directory(tmp_path)
        session = ChatSession(
            transcript, controller, lambda: HistoryStore.open(directory), lineage=w.tasks
        )
        session.start()
        try:
            return [
                turn.user
                for turn in context_turns(transcript.messages)
                if w.tasks.visible(turn.task)
            ]
        finally:
            session.close()


def test_a_net_tainted_task_after_a_restart_sees_nothing_of_the_fs_tainted_one(
    tmp_path: Path,
) -> None:
    files, network = TaskId.new(), TaskId.new()
    store_turns(
        tmp_path,
        [("the secret photos", files), ("the weather", network)],
        [files],
    )

    seen = model_sees(tmp_path)

    assert seen == ["question about the weather"]


def test_a_fresh_start_in_the_last_reply_survives_a_restart_and_shows_the_model_no_old_turn(
    tmp_path: Path,
) -> None:
    # #157: the reply in which the user's fresh start happened is hidden, and the user message
    # before it names the old task. The old restore revived that task; it must start from nothing.
    files = TaskId.new()
    store_turns(
        tmp_path,
        [("the secret photos", files), ("starting over", HIDDEN_TASK)],
        [files, files],
    )

    with world() as w:
        transcript = Transcript()
        directory = history_directory(tmp_path)
        session = ChatSession(
            transcript,
            ChatController(transcript, ManualResponder()),
            lambda: HistoryStore.open(directory),
            lineage=w.tasks,
        )
        session.start()
        try:
            assert not w.tasks.unverified  # nothing to check with the broker: no head
            seen = [t.user for t in context_turns(transcript.messages) if w.tasks.visible(t.task)]
        finally:
            session.close()

    assert seen == []


def test_a_hidden_reply_that_is_not_the_newest_does_not_cut_the_lineage(tmp_path: Path) -> None:
    first, second = TaskId.new(), TaskId.new()
    store_turns(
        tmp_path,
        [("one", first), ("hidden", HIDDEN_TASK), ("two", second)],
        [first, first, second],
    )

    assert model_sees(tmp_path) == ["question about two"]
