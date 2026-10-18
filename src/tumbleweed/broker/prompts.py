"""The prompt interface the broker asks a user through, and the stand-in that denies (S17).

`request_grant` asks the user when no grant covers a request and policy allows one to be made
(`policy.NeedsPrompt`). Who asks is a `PromptInterface`: S18 implements it with the trusted
prompt and picker in the shell's chrome (I5); until then the broker uses `AutoDeny`, which
refuses every time, so that nothing can be granted without a user.

A prompt carries the structured capability (what S18 renders), the scopes it may offer, and the
model's reason as text to be quoted, never believed. Its answer is a decision by the user (I2):
`Allowed` with one of the offered scopes (and perhaps an expiry), or `Denied`. The broker checks
the answer against the scopes it offered, re-checks policy when it issues the grant (`issue_grant`),
bounds the wait (`BrokerConfig.prompt_timeout`, a timeout is a denial) and treats a prompt that
raises as a denial.

S18 adds what it needs here (a `PromptId`, the picker's choice, the requesting tool); the
answers are a closed union so that adding a kind is a visible edit.
"""

from typing import Annotated, Final, Literal, Protocol, final

from pydantic import AwareDatetime, Field

from tumbleweed.broker.capabilities import Capability
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.model import PolicyModel
from tumbleweed.core.ids import RunId, TaskId

MAX_REASON_CHARS: Final = 500


@final
class GrantPrompt(PolicyModel):
    """What the user is asked: `capability`, for `run` of `task`, with the model's `reason`."""

    task: TaskId
    run: RunId
    capability: Capability
    scopes: Annotated[frozenset[GrantScope], Field(min_length=1)]
    reason: Annotated[str, Field(max_length=MAX_REASON_CHARS)]


@final
class Allowed(PolicyModel):
    """The user allowed it, at `scope`, until `expires_at` (never, if null)."""

    kind: Literal["allowed"] = "allowed"
    scope: GrantScope
    expires_at: AwareDatetime | None = None


@final
class Denied(PolicyModel):
    """The user refused, or nobody could be asked. `reason` is a short fixed code for the log."""

    kind: Literal["denied"] = "denied"
    reason: Annotated[str, Field(max_length=64)] = "user_denied"


type PromptAnswer = Annotated[Allowed | Denied, Field(discriminator="kind")]


class PromptInterface(Protocol):
    """Asks the user. Implementations must not return `Allowed` unless a person at the keyboard
    or pointer decided so (I10: not by voice, not by a timeout)."""

    async def ask(self, prompt: GrantPrompt, /) -> PromptAnswer: ...


@final
class AutoDeny:
    """The prompt interface until S18: everything is denied, so no grant is ever made."""

    async def ask(self, prompt: GrantPrompt, /) -> PromptAnswer:
        return Denied(reason="no_prompt_ui")
