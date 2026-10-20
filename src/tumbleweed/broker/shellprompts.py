"""The prompt interface that asks the shell, over the connection of the task's owner (S18, I5).

`ShellPrompts.ask` sends a `ShowPrompt` with a fresh `PromptId` to the connection that made the
task, and waits for the `AnswerPrompt` the shell sends back. What makes the answer count:

- **Unguessable and single-use.** The `PromptId` is random (`tumbleweed.core.ids`) and is taken out
  of the waiting set by the first answer that counts, so a replay finds nothing.
- **Only from the connection the prompt was sent on.** A prompt waits under the `Owner` it was sent
  to; an answer from another owner, even with the right `PromptId`, is refused and the prompt keeps
  waiting. (`Owner` is one IPC connection: the daemon makes one per connection.)
- **A timeout, a closed connection or any failure is a denial** and never a grant (I10). When the
  broker gives up (its timeout cancels `ask`) it tells the shell to take the prompt down.

The shell does the rest of I5: how it is shown, the rate of answers and who can answer. Nothing
here knows about GTK or voice.
"""

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Final, final

from pydantic import ValidationError

from tumbleweed.broker.capabilities import FsRead, FsWrite, NetFull
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.owner import Owner
from tumbleweed.broker.prompts import Allowed, Denied, GrantPrompt, PromptAnswer
from tumbleweed.core.ids import PromptId
from tumbleweed.ipc.broker_messages import (
    NetworkAccess,
    PickAccess,
    ReadAccess,
    RequestedAccess,
    Scope,
    WriteAccess,
)
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import ErrorCode, IpcError, RemoteError, UnexpectedResponseError
from tumbleweed.ipc.prompt_messages import ClosePrompt, PromptClosed, PromptShown, ShowPrompt

_log = logging.getLogger(__name__)

_SHOW_TIMEOUT: Final = timedelta(seconds=10)
"""How long the shell has to acknowledge a prompt. It does so at once (the user is not waited for
inside this call), so this is only the bound on a shell that does not answer."""
_CLOSE_TIMEOUT: Final = timedelta(seconds=5)
_MAX_WAITING: Final = 64

_SCOPES: Final[dict[GrantScope, Scope]] = {GrantScope.ONCE: "once", GrantScope.TASK: "task"}


@final
class _Waiting:
    def __init__(self, owner: Owner, scopes: frozenset[GrantScope]) -> None:
        self.owner = owner
        self.scopes = scopes
        self.answer: asyncio.Future[PromptAnswer] = asyncio.get_running_loop().create_future()


def _access_of(prompt: GrantPrompt) -> RequestedAccess:
    """The wire form of what the prompt asks, from its structure (the shell renders from this)."""
    capability = prompt.capability
    if capability is None:
        if prompt.pick is None:  # unreachable: `GrantPrompt` holds a capability or a pick
            raise ValueError("a prompt with nothing to ask")
        return PickAccess(kind="fs.pick", choose=prompt.pick)
    match capability:
        case FsRead(path=stored):
            return ReadAccess(kind="fs.read", path=stored.path)
        case FsWrite(path=stored):
            return WriteAccess(kind="fs.write", path=stored.path)
        case NetFull():
            return NetworkAccess(kind="net.full")


def _scopes_of(prompt: GrantPrompt) -> tuple[Scope, ...]:
    """The scopes the shell can offer, in a fixed order. The wire has no `tool` scope (S24)."""
    return tuple(text for scope, text in _SCOPES.items() if scope in prompt.scopes)


@final
class ShellPrompts:
    """Asks the shell. The daemon `attach`es each connection when it first speaks and `detach`es it
    when it ends."""

    def __init__(self, *, timeout: timedelta) -> None:
        self._timeout = timeout
        self._connections: dict[Owner, Connection] = {}
        self._waiting: dict[PromptId, _Waiting] = {}
        self._closing: set[asyncio.Task[None]] = set()

    def attach(self, owner: Owner, connection: Connection) -> None:
        self._connections[owner] = connection

    def detach(self, owner: Owner) -> None:
        """The connection of `owner` is gone: whoever waits for its user is told no."""
        self._connections.pop(owner, None)
        for prompt, waiting in list(self._waiting.items()):
            if waiting.owner == owner:
                del self._waiting[prompt]
                if not waiting.answer.done():
                    waiting.answer.set_result(Denied(reason="no_prompt_ui"))

    async def ask(self, prompt: GrantPrompt, owner: Owner, /) -> PromptAnswer:
        connection = self._connections.get(owner)
        scopes = _scopes_of(prompt)
        if connection is None or connection.is_closed or not scopes:
            return Denied(reason="no_prompt_ui")
        if len(self._waiting) >= _MAX_WAITING:
            return Denied(reason="prompt_busy")
        prompt_id = PromptId.new()
        try:
            show = ShowPrompt(
                kind="show_prompt",
                prompt=prompt_id,
                task=prompt.task,
                tool=prompt.tool,
                access=_access_of(prompt),
                scopes=scopes,
                reason=prompt.reason,
                expires_in_ms=max(1, int(self._timeout.total_seconds() * 1000)),
            )
        except ValidationError:
            _log.warning("a prompt could not be put on the wire")
            return Denied(reason="prompt_failed")
        waiting = self._waiting[prompt_id] = _Waiting(owner, prompt.scopes)
        shown = False
        try:
            refused = await self._show(connection, show)
            if refused is not None:
                return refused
            shown = True
            return await waiting.answer
        finally:
            if self._waiting.pop(prompt_id, None) is not None and shown:
                # Nobody answered: the broker's timeout, or this request, ended first. The shell
                # still shows the prompt, and must not go on asking a user who is no longer asked.
                self._close_later(connection, prompt_id)

    @staticmethod
    async def _show(connection: Connection, show: ShowPrompt) -> Denied | None:
        """Send the prompt. A denial if the shell would not take it, None if it has it."""
        try:
            await connection.call(show, PromptShown, request_timeout=_SHOW_TIMEOUT)
        except UnexpectedResponseError:
            return Denied(reason="prompt_busy")  # `Refused(busy)`: its queue is full
        except RemoteError as error:
            if error.code is ErrorCode.UNSUPPORTED_REQUEST:
                return Denied(reason="no_prompt_ui")  # a client that serves no prompts
            _log.warning("the shell failed to take a prompt: %s", error.code)
            return Denied(reason="prompt_failed")
        except IpcError as error:
            _log.warning("a prompt could not be sent: %s", error)
            return Denied(reason="prompt_failed")
        return None

    def answer(self, owner: Owner, prompt: PromptId, answer: PromptAnswer, /) -> bool:
        """Take an answer: from the connection the prompt was sent to, for a prompt still waiting,
        once. Anything else is refused and leaves the prompt as it was."""
        waiting = self._waiting.get(prompt)
        if waiting is None or waiting.owner != owner or waiting.answer.done():
            return False
        if isinstance(answer, Allowed) and answer.scope not in waiting.scopes:
            answer = Denied(reason="scope_not_offered")
        del self._waiting[prompt]
        waiting.answer.set_result(answer)
        return True

    def _close_later(self, connection: Connection, prompt: PromptId) -> None:
        task = asyncio.get_running_loop().create_task(
            self._close(connection, prompt), name=f"close-prompt-{prompt}"
        )
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    @staticmethod
    async def _close(connection: Connection, prompt: PromptId) -> None:
        # The shell may be gone or slow: its own deadline takes the prompt down then.
        with contextlib.suppress(IpcError):
            await connection.call(
                ClosePrompt(kind="close_prompt", prompt=prompt),
                PromptClosed,
                request_timeout=_CLOSE_TIMEOUT,
            )

    async def close(self) -> None:
        """Wait for the shell to be told of the prompts that ended (shutdown)."""
        if self._closing:
            await asyncio.wait(set(self._closing), timeout=_CLOSE_TIMEOUT.total_seconds())
