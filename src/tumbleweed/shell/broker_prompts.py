"""What the broker asks the shell: show a prompt, take it down; and the user's answer back (S18).

`BrokerPrompts` is the `RequestHandler` of the shell's end of the broker connection. The broker's
requests arrive on the connection's event loop, which is not the GTK main thread, so each is
marshalled there (`MainThreadDispatcher`) and handled by the `PromptController`; the handler
answers the request as soon as the controller has taken the prompt, and never waits for the user
(a person may take minutes, a handler has seconds). The user's answer is a request of its own,
`AnswerPrompt`, sent back on the connection the prompt arrived on, from the main thread through that
connection's loop. An answer that cannot be sent (the connection ended, the broker refused it
because it had given up) is only logged: the broker denies what it was not answered.

The handler does not decide anything about who may answer: the broker accepts an answer only from
the connection it sent the prompt on, once (`tumbleweed.broker.shellprompts`), and the controller
accepts one only from a click on the prompt that is shown (`tumbleweed.shell.viewmodel.prompts`).
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Final, final

from tumbleweed.agent.canvas_events import Scheduler
from tumbleweed.agent.runtime import MainThreadDispatcher
from tumbleweed.core.ids import PromptId
from tumbleweed.ipc.broker_messages import Refused
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import ErrorCode, IpcError, RequestRefused
from tumbleweed.ipc.messages import RequestBody, Response, ResponseBody
from tumbleweed.ipc.prompt_messages import (
    AnswerPrompt,
    ClosePrompt,
    PromptClosed,
    PromptDecision,
    PromptShown,
    ShowPrompt,
)
from tumbleweed.shell.viewmodel.prompts import PromptController, PromptView, card_of

if TYPE_CHECKING:
    import concurrent.futures

_log = logging.getLogger(__name__)

_ANSWER_TIMEOUT: Final = timedelta(seconds=10)


async def on_main_thread[T](dispatcher: MainThreadDispatcher, work: Callable[[], T]) -> T:
    """Run `work` on the GTK main thread and wait for what it returns (or raises).

    If the caller is cancelled first the work is skipped when its turn comes, so that a request
    that was given up on does not put a prompt on the screen.
    """
    loop = asyncio.get_running_loop()
    done: asyncio.Future[T] = loop.create_future()

    def deliver(settle: Callable[[], None]) -> None:
        if not done.cancelled():
            settle()

    def run() -> None:
        if done.cancelled():
            return
        try:
            result = work()
        except BaseException as error:  # noqa: BLE001  (handed to the awaiting handler, not lost)
            loop.call_soon_threadsafe(deliver, partial(done.set_exception, error))
        else:
            loop.call_soon_threadsafe(deliver, partial(done.set_result, result))

    dispatcher.post(run)
    return await done


@final
class BrokerPrompts:
    """The request handler of the shell's end of the broker connection, with its controller."""

    def __init__(
        self,
        view: PromptView,
        dispatcher: MainThreadDispatcher,
        scheduler: Scheduler,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.controller = PromptController(view, self._answered, scheduler, clock)
        self._dispatcher = dispatcher
        # Where each prompt came from, so that its answer goes back on that connection. Main thread.
        self._routes: dict[PromptId, tuple[Connection, asyncio.AbstractEventLoop]] = {}
        self._sending: set[concurrent.futures.Future[None]] = set()

    async def __call__(self, request: RequestBody, connection: Connection, /) -> ResponseBody:
        match request:
            case ShowPrompt():
                loop = asyncio.get_running_loop()
                taken = await on_main_thread(
                    self._dispatcher, lambda: self._show(request, connection, loop)
                )
                if not taken:
                    return Refused(
                        kind="refused",
                        reason="busy",
                        message="the shell cannot show another prompt",
                    )
                return PromptShown(kind="prompt_shown")
            case ClosePrompt():
                await on_main_thread(self._dispatcher, lambda: self._withdraw(request.prompt))
                return PromptClosed(kind="prompt_closed")
            case _:
                # The connection's role has already refused what the shell never serves.
                raise RequestRefused(ErrorCode.UNSUPPORTED_REQUEST, "not a shell request")

    def close(self) -> None:
        """The window is closing: take every prompt down. Main thread."""
        self.controller.close()
        self._routes.clear()

    # --- on the main thread ----------------------------------------------------------------

    def _show(
        self, request: ShowPrompt, connection: Connection, loop: asyncio.AbstractEventLoop
    ) -> bool:
        card = card_of(request)
        if not self.controller.offer(card, request.expires_in_ms / 1000):
            return False
        self._routes[card.prompt] = (connection, loop)
        return True

    def _withdraw(self, prompt: PromptId) -> None:
        self._routes.pop(prompt, None)
        self.controller.withdraw(prompt)

    def _answered(self, prompt: PromptId, decision: PromptDecision, /) -> None:
        route = self._routes.pop(prompt, None)
        if route is None:
            return
        connection, loop = route
        request = AnswerPrompt(kind="answer_prompt", prompt=prompt, decision=decision)
        try:
            sent = asyncio.run_coroutine_threadsafe(self._send(connection, request), loop)
        except RuntimeError:
            _log.warning("an answer could not be sent: the connection's loop is closed")
            return
        self._sending.add(sent)
        sent.add_done_callback(self._sending.discard)

    @staticmethod
    async def _send(connection: Connection, request: AnswerPrompt) -> None:
        try:
            response = await connection.call(request, Response, request_timeout=_ANSWER_TIMEOUT)
        except IpcError as error:
            _log.warning("an answer to a prompt could not be sent: %s", type(error).__name__)
            return
        if isinstance(response, Refused):
            _log.info("the broker did not take an answer: %s", response.reason)
