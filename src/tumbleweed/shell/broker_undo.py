"""The undo list's connection to the broker (S21): the shell's own link, the user's own calls.

`BrokerUndoPort` asks `list_snapshots` and `undo_snapshot` on the shell's broker link and reports on
the main thread. The broker's refusals are fixed text of the broker's; they are shown as the note of
the list. Nothing the model wrote reaches here.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, final

from tumbleweed.agent.broker_link import BrokerLink, BrokerUnavailableError
from tumbleweed.agent.runtime import MainThreadDispatcher
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.config import UNDO_WAIT
from tumbleweed.core.ids import SnapshotId
from tumbleweed.ipc.broker_messages import SnapshotInfo
from tumbleweed.shell.viewmodel.prompts import plain_reason
from tumbleweed.shell.viewmodel.undo import UndoOutcome

if TYPE_CHECKING:
    import concurrent.futures

_log = logging.getLogger(__name__)

_LIST_SECONDS: Final = 20.0
_UNDO_SECONDS: Final = UNDO_WAIT.total_seconds()


@final
class BrokerUndoPort:
    def __init__(self, link: BrokerLink, dispatcher: MainThreadDispatcher) -> None:
        self._link = link
        self._dispatcher = dispatcher

    def list(self, done: Callable[[tuple[SnapshotInfo, ...] | None], None]) -> None:
        async def work(client: BrokerClient) -> tuple[SnapshotInfo, ...]:
            return (await client.list_snapshots()).snapshots

        def finished(future: "concurrent.futures.Future[tuple[SnapshotInfo, ...]]") -> None:
            found: tuple[SnapshotInfo, ...] | None
            try:
                found = future.result()
            except Exception:  # noqa: BLE001  (any failure is "the broker is not answering")
                _log.warning("the undo list could not be read")
                found = None
            self._dispatcher.post(lambda: done(found))

        try:
            self._link.submit(work, deadline=_LIST_SECONDS).add_done_callback(finished)
        except BrokerUnavailableError:
            self._dispatcher.post(lambda: done(None))

    def undo(self, snapshot: SnapshotId, done: Callable[[UndoOutcome], None]) -> None:
        async def work(client: BrokerClient) -> UndoOutcome:
            try:
                result = await client.undo_snapshot(snapshot)
            except BrokerRefused as refusal:
                text, _ = plain_reason(f"Not undone: {refusal.message}.", 300)
                return UndoOutcome("refused", text)
            note = (
                " You can undo this too."
                if result.safety is not None
                else " What was there before could not be saved, so this cannot be undone."
            )
            return UndoOutcome("undone", f"Undone: {result.paths} place(s) put back.{note}")

        def finished(future: "concurrent.futures.Future[UndoOutcome]") -> None:
            try:
                outcome = future.result()
            except Exception:  # noqa: BLE001
                _log.warning("an undo could not be asked of the broker")
                outcome = UndoOutcome("failed", "Not undone: the broker is not answering.")
            self._dispatcher.post(lambda: done(outcome))

        try:
            self._link.submit(work, deadline=_UNDO_SECONDS).add_done_callback(finished)
        except BrokerUnavailableError:
            self._dispatcher.post(
                lambda: done(UndoOutcome("failed", "Not undone: the broker is not available."))
            )
