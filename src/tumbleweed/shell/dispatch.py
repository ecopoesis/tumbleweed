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
