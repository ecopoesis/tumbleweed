"""The undo panel in a real window (S21): rows are plain labels, the click is the user's."""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from tests.shell.gtk_support import descendants, labels_in
from tumbleweed.core.ids import SnapshotId, TaskId
from tumbleweed.ipc.broker_messages import SnapshotInfo
from tumbleweed.shell._gi import Gtk
from tumbleweed.shell.undo_chrome import UndoPanel
from tumbleweed.shell.viewmodel.undo import UndoOutcome

pytestmark = pytest.mark.gtk


class Port:
    def __init__(self, found: tuple[SnapshotInfo, ...]) -> None:
        self.found = found
        self.undone: list[SnapshotId] = []

    def list(self, done: Callable[[tuple[SnapshotInfo, ...] | None], None]) -> None:
        done(self.found)

    def undo(self, snapshot: SnapshotId, done: Callable[[UndoOutcome], None]) -> None:
        self.undone.append(snapshot)
        done(UndoOutcome("undone", "Undone."))


def snapshot(path: str, *, undoable: bool = True) -> SnapshotInfo:
    return SnapshotInfo(
        snapshot=SnapshotId.new(),
        kind="run",
        task=TaskId.new(),
        taken_at=datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        paths=(path,),
        undoable=undoable,
        undone=False,
    )


def test_the_panel_is_hidden_until_opened_and_shows_paths_as_plain_text() -> None:
    markup = "/home/u/<b>bold</b>&amp;"
    panel = UndoPanel(Port((snapshot(markup),)))
    assert not panel.get_visible()

    panel.open()

    assert panel.get_visible()
    texts = [label.get_text() for label in labels_in(panel)]
    assert any("<b>bold</b>" in text for text in texts)  # literal, never markup
    assert all(not label.get_use_markup() for label in labels_in(panel))


def test_the_undo_button_asks_the_broker_for_that_row_only() -> None:
    first, second = snapshot("/home/u/a"), snapshot("/home/u/b", undoable=False)
    port = Port((first, second))
    panel = UndoPanel(port)
    panel.open()

    buttons = [
        w for w in descendants(panel) if isinstance(w, Gtk.Button) and w.get_label() == "Undo"
    ]
    assert [b.get_sensitive() for b in buttons] == [True, False]
    buttons[0].emit("clicked")

    assert port.undone == [first.snapshot]
