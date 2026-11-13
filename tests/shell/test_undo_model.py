"""The undo list's state machine, without a window (S21, D28)."""

from collections.abc import Callable
from datetime import UTC, datetime

from tests.shell.prompt_support import show_message
from tumbleweed.core.ids import SnapshotId, TaskId
from tumbleweed.ipc.broker_messages import NoUndoAccess, SnapshotInfo
from tumbleweed.shell.viewmodel.prompts import card_of
from tumbleweed.shell.viewmodel.undo import UndoModel, UndoOutcome, UndoRow, row_of

WHEN = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def info(**changes: object) -> SnapshotInfo:
    base: dict[str, object] = {
        "snapshot": SnapshotId.new(),
        "kind": "run",
        "task": TaskId.new(),
        "taken_at": WHEN,
        "paths": ("/home/u/Projects/app",),
        "undoable": True,
        "undone": False,
    }
    base.update(changes)
    return SnapshotInfo.model_validate(base)


class FakePort:
    def __init__(self, *found: SnapshotInfo) -> None:
        self.found: tuple[SnapshotInfo, ...] | None = found
        self.undone: list[SnapshotId] = []
        self.pending: list[Callable[[], None]] = []
        self.outcome = UndoOutcome("undone", "Undone.")

    def list(self, done: Callable[[tuple[SnapshotInfo, ...] | None], None]) -> None:
        done(self.found)

    def undo(self, snapshot: SnapshotId, done: Callable[[UndoOutcome], None]) -> None:
        self.undone.append(snapshot)
        self.pending.append(lambda: done(self.outcome))


class FakeView:
    def __init__(self) -> None:
        self.shown: list[tuple[tuple[UndoRow, ...], str | None]] = []
        self.busy_states: list[bool] = []

    def present(self, rows: tuple[UndoRow, ...], note: str | None) -> None:
        self.shown.append((rows, note))

    def busy(self, busy: bool) -> None:
        self.busy_states.append(busy)


def test_the_list_is_built_from_the_brokers_structure_and_spells_out_names() -> None:
    sneaky = "/home/u/‮evil‬\nname"
    row = row_of(info(paths=(sneaky, "/a", "/b", "/c", "/d")))

    assert "‮" not in row.paths[0]
    assert "\\u{202E}" in row.paths[0]
    assert "\n" not in row.paths[0]
    assert len(row.paths) == 3
    assert row.more == 2
    assert row.when == WHEN.astimezone().strftime("%Y-%m-%d %H:%M")


def test_a_run_with_no_undo_and_an_undone_one_cannot_be_undone() -> None:
    port = FakePort(info(undoable=False), info(undoable=False, undone=True))
    view = FakeView()
    model = UndoModel(port, view)

    model.refresh()

    rows, _ = view.shown[-1]
    assert [r.status for r in rows] == ["ran with no undo", "undone"]
    assert not any(model.undo(r.snapshot) for r in rows)
    assert port.undone == []


def test_an_undo_asks_once_then_refreshes_with_the_brokers_words() -> None:
    first = info()
    port = FakePort(first)
    view = FakeView()
    model = UndoModel(port, view)
    model.refresh()

    assert model.undo(first.snapshot)
    assert not model.undo(first.snapshot)  # one at a time
    port.outcome = UndoOutcome("refused", "Not undone: it is gone.")
    port.found = ()
    port.pending.pop()()

    assert port.undone == [first.snapshot]
    rows, note = view.shown[-1]
    assert rows == ()
    assert note == "Not undone: it is gone."
    assert view.busy_states[-1] is False


def test_a_row_that_is_not_listed_cannot_be_undone() -> None:
    model = UndoModel(FakePort(), FakeView())
    model.refresh()

    assert not model.undo(SnapshotId.new())


def test_a_broker_that_does_not_answer_says_so() -> None:
    port = FakePort()
    port.found = None
    view = FakeView()

    UndoModel(port, view).refresh()

    assert view.shown[-1][1] is not None
    assert "not answering" in str(view.shown[-1][1])


def test_the_no_undo_prompt_card_carries_the_paths_and_the_cause() -> None:
    card = card_of(
        show_message(
            access=NoUndoAccess(kind="no_undo", paths=("/home/u/a‮b",), cause="too_many_bytes"),
            reason="",
            scopes=("once",),
        )
    )

    assert card.kind == "no_undo"
    assert card.cause == "too_many_bytes"
    assert card.paths == ("/home/u/a\\u{202E}b",)
    assert card.scopes == ("once",)


def test_a_superseded_snapshot_is_shown_as_covered_by_a_later_undo() -> None:
    row = row_of(info(undoable=False, superseded=True))

    assert row.status == "a later undo covers it"
    assert not row.undoable


def test_the_undo_without_a_way_back_card_carries_its_purpose() -> None:
    card = card_of(
        show_message(
            access=NoUndoAccess(
                kind="no_undo", purpose="undo", paths=("/home/u/a",), cause="too_deep"
            ),
            reason="",
            scopes=("once",),
        )
    )

    assert (card.kind, card.purpose, card.cause) == ("no_undo", "undo", "too_deep")
