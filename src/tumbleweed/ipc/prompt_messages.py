"""The messages of the trusted prompt (S18): the broker asks the shell, the shell answers.

Registered in `tumbleweed.ipc.messages`. A prompt is two requests, one in each direction, so that
nothing waits on the user inside a handler (the user may take minutes; a handler has seconds, #60):

- `ShowPrompt` (broker to shell): what to ask, as a structure. The shell acknowledges at once
  (`PromptShown`) or refuses (`Refused`: `busy`, its queue is full). The access is the broker's
  *resolved* one: a path is canonical text, the one a grant would record, not the model's spelling.
  `reason` is model text, bounded: the shell shows it as quoted and labelled, never as a fact.
- `AnswerPrompt` (shell to broker): the user's decision, under the `PromptId` of the prompt. The
  broker takes it only from the connection it sent that prompt to, once (I5); anything else is
  answered `Refused(unknown_prompt)`.
- `ClosePrompt` (broker to shell): the prompt is over (the broker's timeout, or the request that
  asked it is gone); the shell takes it down without answering.

A `PromptId` is unguessable (random, `tumbleweed.core.ids`) and single-use. The answer holds no
path the model chose: for a picker (`PickAccess`) it holds the path the *user* picked, as text,
which the broker resolves and checks against policy like any other path.
"""

from typing import Annotated, Final, Literal, final

from pydantic import Field

from tumbleweed.core.ids import PromptId, TaskId, ToolId
from tumbleweed.ipc.base import IpcMessage, Request, Response
from tumbleweed.ipc.broker_messages import (
    MAX_PATH_CHARS,
    MAX_REASON_CHARS,
    RequestedAccess,
    Scope,
)

MAX_PROMPT_SECONDS: Final = 3600
"""The longest a prompt may be asked to wait (the broker's own ceiling for `prompt_timeout`)."""

DenyReason = Literal[
    "user_denied",  # the user pressed Deny
    "prompt_timeout",  # the shell's own deadline passed with no answer: a timeout is a denial
]


@final
class ShowPrompt(Request):
    """Ask the user. `scopes` are the grant scopes the user may choose from, `expires_in_ms` how
    long the broker will wait, from now: the shell denies when it has passed."""

    kind: Literal["show_prompt"]
    prompt: PromptId
    task: TaskId
    tool: ToolId | None
    access: RequestedAccess
    scopes: Annotated[tuple[Scope, ...], Field(min_length=1, max_length=2)]
    reason: Annotated[str, Field(max_length=MAX_REASON_CHARS)]
    expires_in_ms: Annotated[int, Field(ge=1, le=MAX_PROMPT_SECONDS * 1000)]


@final
class PromptShown(Response):
    """The shell has the prompt (shown, or queued behind the one it shows)."""

    kind: Literal["prompt_shown"]


@final
class ClosePrompt(Request):
    """The prompt is over; take it down. Answered `PromptClosed` even if it is already gone."""

    kind: Literal["close_prompt"]
    prompt: PromptId


@final
class PromptClosed(Response):
    kind: Literal["prompt_closed"]


@final
class AllowDecision(IpcMessage):
    """The user allowed it, for `scope`. `picked` is what they chose in the picker (a picker prompt
    only: the broker refuses an answer to any other prompt that holds one)."""

    kind: Literal["allow"]
    scope: Scope
    picked: Annotated[str, Field(min_length=1, max_length=MAX_PATH_CHARS)] | None


@final
class DenyDecision(IpcMessage):
    """The user did not allow it, or nobody could be asked."""

    kind: Literal["deny"]
    reason: DenyReason


PromptDecision = Annotated[AllowDecision | DenyDecision, Field(discriminator="kind")]


@final
class AnswerPrompt(Request):
    """The user's decision on `prompt`."""

    kind: Literal["answer_prompt"]
    prompt: PromptId
    decision: PromptDecision


@final
class PromptAnswered(Response):
    """The broker took the answer."""

    kind: Literal["prompt_answered"]
