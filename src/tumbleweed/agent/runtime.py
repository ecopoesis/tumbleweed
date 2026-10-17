"""The agent's `Responder`: runs the loop on a worker thread and reports to the shell's main thread.

Threading (DECISIONS D2, D9, #47). The agent runs its own asyncio event loop on a daemon worker
thread, and hands every event back to the shell through a `MainThreadDispatcher` that the shell
injects (built on `GLib.idle_add`; this package never imports `gi`). The alternative D2 names,
`gi.events` (asyncio on the GLib main loop), was not chosen: it is PyGObject's asyncio policy, which
Python 3.14 deprecates (#47, and our tests turn warnings into errors), it ties the agent to GTK,
and a slow handler or a stuck DNS lookup would run on the thread that draws the window. With the
worker thread, the main loop does nothing but append text, and the agent is testable with a plain
queue as the dispatcher.

Thread affinity: `start`, `cancel` (the handle's) and `close` belong to the main thread, which is
the one that built the responder; they refuse any other. All state the main thread owns (the active
reply, the conversation) is touched only there: the worker never reads or writes it, it posts
callbacks that do. Events reach the sink only through `MainThreadDispatcher.post`, so the sink sees
them on the thread that sent the message, as the `Responder` protocol requires (the sink raises
`WrongThreadError` otherwise).

Cancellation: the handle's `cancel` ends the reply at once on the main thread (nothing posted
later is delivered) and cancels the loop's task on the worker, which closes the provider's stream
and records the invocation as cancelled. A reply ends exactly once: finished, failed, or cancelled.
Only a finished reply is added to the conversation.
"""

import asyncio
import concurrent.futures
import functools
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Final, Protocol, final

from tumbleweed.agent.conversation import Conversation
from tumbleweed.agent.errors import AgentBusyError, AgentError, LimitError, describe_unexpected
from tumbleweed.agent.limits import MAX_USER_TEXT_BYTES, AgentLimits
from tumbleweed.agent.llm import LlmMessage, LlmProvider
from tumbleweed.agent.loop import AgentLoop
from tumbleweed.agent.recorder import InvocationRecorder
from tumbleweed.agent.responder import (
    ReplyDelta,
    ReplyEvent,
    ReplyFailed,
    ReplyFinished,
    ReplyHandle,
    ReplyRequest,
    ReplySink,
)
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.core.ids import SessionId
from tumbleweed.core.types import ModelName

logger = logging.getLogger(__name__)

_START_TIMEOUT: Final = 5.0
_STOP_TIMEOUT: Final = 3.0
_INTERNAL_FAILURE = "An internal error stopped the reply."


class MainThreadDispatcher(Protocol):
    """Runs callbacks on the shell's main thread (the shell's implementation: `GLib.idle_add`)."""

    def post(self, callback: Callable[[], None]) -> None:
        """Run `callback` on the main thread soon. Safe to call from any thread, never blocks."""
        ...


@final
class _Worker:
    """An asyncio event loop on a daemon thread, started on first use."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()

    def ensure_started(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(target=self._main, name="tumbleweed-agent", daemon=True)
        try:
            thread.start()
        except RuntimeError:  # the process cannot make another thread
            raise AgentBusyError("The agent could not start its worker thread.") from None
        self._thread = thread
        if not self._ready.wait(_START_TIMEOUT):
            raise AgentBusyError("The agent could not start its worker thread.")

    def _main(self) -> None:
        async def serve() -> None:
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            self._ready.set()
            await self._stop.wait()

        # asyncio.run cancels whatever is still running when `serve` returns, and awaits it.
        asyncio.run(serve())

    def submit(self, coroutine: Coroutine[None, None, None]) -> concurrent.futures.Future[None]:
        if self._loop is None:
            coroutine.close()
            raise AgentBusyError("The agent's worker thread is not running.")
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except RuntimeError:  # the loop has been closed: the worker is gone
            coroutine.close()
            raise AgentBusyError("The agent's worker thread is not running.") from None

    def close(self, cleanup: Callable[[], Awaitable[None]] | None) -> None:
        """Run `cleanup` on the loop, then stop it and wait (bounded) for the thread."""
        loop, stop, thread = self._loop, self._stop, self._thread
        if loop is None or stop is None or thread is None:
            return

        async def run_cleanup() -> None:
            if cleanup is not None:
                await cleanup()

        try:
            asyncio.run_coroutine_threadsafe(run_cleanup(), loop).result(_STOP_TIMEOUT)
        except Exception:  # noqa: BLE001  (cleanup is best effort: the thread is stopped regardless)
            logger.warning("the agent's cleanup did not finish")
        loop.call_soon_threadsafe(stop.set)
        thread.join(_STOP_TIMEOUT)
        if thread.is_alive():
            logger.warning("the agent's worker thread did not stop in time")


@final
class _Run:
    """One reply in flight, owned by the main thread."""

    def __init__(self, sink: ReplySink) -> None:
        self.sink = sink
        self.ended = False
        self.future: concurrent.futures.Future[None] | None = None


@final
class _Handle:
    def __init__(self, responder: "AgentResponder", run: _Run) -> None:
        self._responder = responder
        self._run = run

    def cancel(self) -> None:
        self._responder.cancel_run(self._run)


@final
class AgentResponder:
    """Answers chat messages with the agent loop; see the module docstring."""

    def __init__(
        self,
        *,
        provider: LlmProvider,
        recorder: InvocationRecorder,
        registry: ToolRegistry,
        model: ModelName,
        system_prompt: str,
        limits: AgentLimits,
        dispatcher: MainThreadDispatcher,
        cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._limits = limits
        self._session_id = SessionId.new()
        self._loop = AgentLoop(
            provider=recorder.record(provider, session_id=self._session_id),
            registry=registry,
            model=model,
            system_prompt=system_prompt,
            limits=limits,
        )
        self._cleanup = cleanup
        self._conversation = Conversation()
        self._worker = _Worker()
        self._active: _Run | None = None
        self._closed = False
        self._thread = threading.get_ident()

    def _check_thread(self) -> None:
        if threading.get_ident() != self._thread:
            raise AgentBusyError("The agent must be used from the thread that created it.")

    def start(self, request: ReplyRequest, sink: ReplySink) -> ReplyHandle:
        self._check_thread()
        if self._closed:
            raise AgentBusyError("The agent has shut down.")
        if self._active is not None:
            raise AgentBusyError("A reply is already in progress.")
        if len(request.text.encode("utf-8", errors="replace")) > MAX_USER_TEXT_BYTES:
            raise LimitError(f"The message is larger than {MAX_USER_TEXT_BYTES // 1024} KiB.")
        self._worker.ensure_started()
        run = _Run(sink)
        self._active = run
        try:
            run.future = self._worker.submit(
                self._drive(run, self._conversation.messages, request.text)
            )
        except BaseException:
            self._active = None
            raise
        return _Handle(self, run)

    def cancel_run(self, run: _Run) -> None:
        self._check_thread()
        if run.ended:
            return
        run.ended = True
        if self._active is run:
            self._active = None
        if run.future is not None:
            run.future.cancel()  # cancels the loop's task on the worker thread

    def close(self) -> None:
        """Cancel the reply in flight and stop the worker thread. Idempotent."""
        self._check_thread()
        if self._closed:
            return
        self._closed = True
        if self._active is not None:
            self.cancel_run(self._active)
        self._worker.close(self._cleanup)

    # On the worker thread.

    async def _drive(self, run: _Run, history: tuple[LlmMessage, ...], text: str) -> None:
        try:
            reply = await self._loop.run(
                history, text, lambda piece: self._post(run, ReplyDelta(text=piece))
            )
        except AgentError as exc:
            self._post(run, ReplyFailed(reason=str(exc)))
        except Exception as exc:  # noqa: BLE001  (the worker is a boundary)
            # A bug (everything a provider can do wrong is a ProviderError by now): by type and
            # place only, because its message could hold what a server said.
            logger.error("the reply failed unexpectedly: %s", describe_unexpected(exc))
            self._post(run, ReplyFailed(reason=_INTERNAL_FAILURE))
        else:
            self._post(run, ReplyFinished(), commit=(text, reply))

    def _post(self, run: _Run, event: ReplyEvent, *, commit: tuple[str, str] | None = None) -> None:
        try:
            self._dispatcher.post(functools.partial(self._deliver, run, event, commit))
        except Exception:
            logger.exception("could not hand an event to the main thread")

    # On the main thread, through the dispatcher.

    def _deliver(self, run: _Run, event: ReplyEvent, commit: tuple[str, str] | None) -> None:
        if run.ended:
            return
        try:
            if not isinstance(event, ReplyDelta):
                run.ended = True
                if self._active is run:
                    self._active = None
                if commit is not None:
                    self._conversation.commit(*commit)
        finally:
            # The reply must end whatever happens to its record: a terminal event that is not
            # emitted leaves the chat busy for ever.
            run.sink.emit(event)


@final
class UnavailableResponder:
    """Fails every message with `reason`, which the chat shows (no key, a bad config file)."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def start(self, request: ReplyRequest, sink: ReplySink) -> ReplyHandle:
        raise AgentError(self._reason)
