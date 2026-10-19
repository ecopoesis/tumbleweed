"""The main-thread dispatcher the shell gives the agent (DECISIONS D9): `GLib.idle_add`.

The agent runs on a worker thread and may not import `gi`; it reports through this object, whose
`post` is safe to call from any thread. The callback runs on the thread of the default main
context, which in the shell is the GTK main thread. A callback that raises is logged and does not
stop the next one.
"""

import logging
from collections.abc import Callable
from typing import final

from tumbleweed.shell._gi import GLib

logger = logging.getLogger(__name__)


@final
class GlibDispatcher:
    """A `MainThreadDispatcher` over the default GLib main context."""

    def post(self, callback: Callable[[], None]) -> None:
        GLib.idle_add(self._run, callback, priority=GLib.PRIORITY_DEFAULT)

    @staticmethod
    def _run(callback: Callable[[], None]) -> bool:
        try:
            callback()
        except Exception:
            logger.exception("a callback posted to the main thread failed")
        return GLib.SOURCE_REMOVE


@final
class _Timer:
    """One `GLib.timeout_add` that can be cancelled whether or not it already ran."""

    def __init__(self, seconds: float, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._source = GLib.timeout_add(max(1, round(seconds * 1000)), self._fire)

    def _fire(self) -> bool:
        self._source = 0  # the source is gone once this returns SOURCE_REMOVE
        try:
            self._callback()
        except Exception:
            logger.exception("a timer callback failed")
        return GLib.SOURCE_REMOVE

    def cancel(self) -> None:
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0


@final
class GlibScheduler:
    """A `Scheduler` over the default GLib main context (the event gate's timer, S12)."""

    def call_later(self, seconds: float, callback: Callable[[], None]) -> Callable[[], None]:
        return _Timer(seconds, callback).cancel
