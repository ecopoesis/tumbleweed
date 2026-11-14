"""The undo list as a state machine, with no GTK (S21, D28).

The chrome's "Undo" action lists the recent write-enabled runs of every task and puts one back. It
is the user's own action: it is reached only by a click on a button of the trusted chrome, and no
model tool calls `UndoSnapshot`. What is shown is built from the broker's structure: the paths are
file-system names (data, never believed), so each is spelled out (`visible`) and capped; the time
is formatted by us. Nothing here touches a file: the broker restores, after snapshotting what is
there so that the undo can be undone.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol, final

from tumbleweed.core.ids import SnapshotId
from tumbleweed.ipc.broker_messages import SnapshotInfo
from tumbleweed.shell.viewmodel.prompts import visible

_log = logging.getLogger(__name__)

MAX_ROWS: Final = 30
MAX_PATHS_SHOWN: Final = 3
MAX_PATH_SHOWN: Final = 200
_ELLIPSIS: Final = "…"


@final
@dataclass(frozen=True, slots=True)
class UndoRow:
    """One line of the list. `paths` are plain and capped; `more` counts those not shown."""

    snapshot: SnapshotId
    when: str
    what: str
    paths: tuple[str, ...]
    more: int
    undoable: bool
    status: str | None


type UndoResultKind = Literal["undone", "refused", "failed"]


@final
@dataclass(frozen=True, slots=True)
class UndoOutcome:
    """How an undo ended: `refused` carries the broker's fixed text, `failed` that nothing could
    be asked (the broker is away)."""

    kind: UndoResultKind
    text: str


class UndoPort(Protocol):
    """Asks the broker. Both calls return at once and report on the main thread, once."""

    def list(self, done: Callable[[tuple[SnapshotInfo, ...] | None], None]) -> None: ...

    def undo(self, snapshot: SnapshotId, done: Callable[[UndoOutcome], None]) -> None: ...


class UndoView(Protocol):
    def present(self, rows: tuple[UndoRow, ...], note: str | None) -> None:
        """Draw the list (and a note of ours: what happened, or why there is nothing)."""
        ...

    def busy(self, busy: bool) -> None: ...


def _cap(text: str) -> str:
    shown = visible(text)
    return shown if len(shown) <= MAX_PATH_SHOWN else shown[: MAX_PATH_SHOWN - 1] + _ELLIPSIS


def row_of(info: SnapshotInfo) -> UndoRow:
    """The row for a snapshot of the broker's list."""
    when = info.taken_at.astimezone().strftime("%Y-%m-%d %H:%M")
    status: str | None = None
    if info.undone:
        status = "undone"
    elif info.superseded:
        status = "a later undo covers it"
    elif info.blocked:
        status = "undo the later changes to that folder first"
    elif not info.undoable:
        status = "ran with no undo"
    return UndoRow(
        snapshot=info.snapshot,
        when=when,
        what="Undo of an undo" if info.kind == "undo" else "Change made by code",
        paths=tuple(_cap(path) for path in info.paths[:MAX_PATHS_SHOWN]),
        more=max(0, len(info.paths) - MAX_PATHS_SHOWN),
        undoable=info.undoable,
        status=status,
    )


@final
class UndoModel:
    """Fetches the list, and undoes one entry when the user clicks it. Main thread only."""

    def __init__(self, port: UndoPort, view: UndoView) -> None:
        self._port = port
        self._view = view
        self._working = False
        self.rows: tuple[UndoRow, ...] = ()

    def refresh(self, note: str | None = None) -> None:
        if self._working:
            return
        self._working = True
        self._view.busy(True)

        def done(found: tuple[SnapshotInfo, ...] | None) -> None:
            self._working = False
            self._view.busy(False)
            if found is None:
                self.rows = ()
                self._view.present((), "The list could not be read: the broker is not answering.")
                return
            self.rows = tuple(row_of(info) for info in found[:MAX_ROWS])
            self._view.present(self.rows, note or (None if self.rows else "Nothing to undo yet."))

        self._port.list(done)

    def undo(self, snapshot: SnapshotId) -> bool:
        """The user chose to undo `snapshot`. False if it is not a row that can be, or one undo is
        already under way."""
        row = next((each for each in self.rows if each.snapshot == snapshot), None)
        if row is None or not row.undoable or self._working:
            return False
        self._working = True
        self._view.busy(True)

        def done(outcome: UndoOutcome) -> None:
            self._working = False
            self._view.busy(False)
            self.refresh(outcome.text)

        self._port.undo(snapshot, done)
        return True

    @staticmethod
    def now_text(moment: datetime) -> str:
        return moment.astimezone().strftime("%Y-%m-%d %H:%M")
