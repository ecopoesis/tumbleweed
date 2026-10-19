"""`Worker`: one daemon thread that runs jobs in order, shared by the voice side's workers
(`VoiceInput`'s audio and speech threads, `SpeechOutput`'s synthesis and playback threads)."""

import logging
import queue
import threading
from collections.abc import Callable
from typing import final

logger = logging.getLogger(__name__)


@final
class Worker:
    """One daemon thread that runs jobs in order. A job that raises is logged, not fatal."""

    def __init__(self, name: str) -> None:
        self._jobs: queue.SimpleQueue[Callable[[], None] | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, job: Callable[[], None]) -> None:
        self._jobs.put(job)

    def close(self) -> None:
        """Stop after the jobs already queued; does not wait for them."""
        self._jobs.put(None)

    def _run(self) -> None:
        while (job := self._jobs.get()) is not None:
            try:
                job()
            except Exception:
                logger.exception("a voice job failed")
