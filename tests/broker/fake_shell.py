"""The shell's side of the prompt protocol, by hand, for the broker's tests (S18).

`FakeShell` is the request handler of a `BrokerClient`: it records what the broker asks it to show
and take down, and answers `AnswerPrompt` only when the test says so, on whichever connection the
test chooses (to prove that the wrong one is refused).
"""

import asyncio
from typing import final

from tumbleweed.broker.client import BrokerClient
from tumbleweed.core.ids import PromptId
from tumbleweed.ipc.broker_messages import Refused, Scope
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import ErrorCode, RequestRefused
from tumbleweed.ipc.messages import RequestBody, Response, ResponseBody
from tumbleweed.ipc.prompt_messages import (
    AllowDecision,
    AnswerPrompt,
    ClosePrompt,
    DenyDecision,
    DenyReason,
    PromptAnswered,
    PromptClosed,
    PromptShown,
    ShowPrompt,
)

WAIT = 10.0


@final
class FakeShell:
    """Records the prompts it is shown. With `busy` it refuses them, like a full queue."""

    def __init__(self) -> None:
        self.shown: list[ShowPrompt] = []
        self.closed: list[PromptId] = []
        self.busy = False
        self.delay = 0.0  # how long it takes to acknowledge a prompt it has taken
        self._arrived: asyncio.Queue[ShowPrompt] = asyncio.Queue()
        self._closed_event = asyncio.Event()

    async def __call__(self, request: RequestBody, connection: Connection, /) -> ResponseBody:
        match request:
            case ShowPrompt():
                if self.busy:
                    return Refused(kind="refused", reason="busy", message="the queue is full")
                self.shown.append(request)
                self._arrived.put_nowait(request)
                if self.delay:
                    await asyncio.sleep(self.delay)
                return PromptShown(kind="prompt_shown")
            case ClosePrompt():
                self.closed.append(request.prompt)
                self._closed_event.set()
                return PromptClosed(kind="prompt_closed")
            case _:
                raise RequestRefused(ErrorCode.UNSUPPORTED_REQUEST, "not a shell request")

    async def next_prompt(self) -> ShowPrompt:
        async with asyncio.timeout(WAIT):
            return await self._arrived.get()

    async def wait_closed(self) -> None:
        async with asyncio.timeout(WAIT):
            await self._closed_event.wait()


async def allow(
    client: BrokerClient, shown: ShowPrompt, *, scope: Scope = "task", picked: str | None = None
) -> PromptAnswered | Refused:
    """Answer `shown` with Allow over `client`'s connection: the broker's answer or its refusal."""
    decision = AllowDecision(kind="allow", scope=scope, picked=picked)
    return await answer(client, shown.prompt, decision)


async def deny(
    client: BrokerClient, shown: ShowPrompt, *, reason: DenyReason = "user_denied"
) -> PromptAnswered | Refused:
    return await answer(client, shown.prompt, DenyDecision(kind="deny", reason=reason))


async def answer(
    client: BrokerClient, prompt: PromptId, decision: AllowDecision | DenyDecision
) -> PromptAnswered | Refused:
    response = await client.connection.call(
        AnswerPrompt(kind="answer_prompt", prompt=prompt, decision=decision), Response
    )
    assert isinstance(response, PromptAnswered | Refused)
    return response
