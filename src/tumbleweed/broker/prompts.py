"""The prompt interface the broker asks a user through, and the stand-in that denies (S17, S18).

`request_grant` asks the user when no grant covers a request and policy allows one to be made
(`policy.NeedsPrompt`), or when the request is for the trusted picker. Who asks is a
`PromptInterface`: `shellprompts.ShellPrompts` sends the prompt to the shell's chrome (I5) over the
connection of the task's owner; `AutoDeny` refuses every time, for a broker with nobody to ask, so
that nothing can be granted without a user.

A prompt carries the structured capability (what the shell renders), the scopes it may offer, the
tool that asked, if any, and the model's reason as text to be quoted, never believed. A prompt for
the picker has no capability yet: the user's choice is the path. Its answer is a decision by the
user (I2): `Allowed` with one of the offered scopes (and, for the picker, the path the user chose),
or `Denied`. The broker checks the answer against the scopes it offered, re-checks policy when it
issues the grant (`issue_grant`), bounds the wait (`BrokerConfig.prompt_timeout`, a timeout is a
denial) and treats a prompt that raises as a denial.

The answers are a closed union so that adding a kind is a visible edit. The interface is also what
later prompts (an imported tool's taint, #98) extend: with a new subject next to
`capability` and `pick`.
"""

from typing import Annotated, Final, Literal, Protocol, Self, final

from pydantic import AwareDatetime, Field, model_validator

from tumbleweed.broker.capabilities import Capability
from tumbleweed.broker.errors import SnapshotFault
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.model import PolicyModel
from tumbleweed.broker.owner import Owner
from tumbleweed.broker.paths import StoredPath
from tumbleweed.core.ids import PromptId, RunId, TaskId, ToolId

MAX_REASON_CHARS: Final = 500
MAX_PICKED_CHARS: Final = 4096


@final
class NoUndoSubject(PolicyModel):
    """The "no undo" question (S21, I13): `paths` are what the run may change and could not be
    saved, for `cause`. The answer `Allowed` (scope `once`) lets this one run go ahead."""

    paths: Annotated[tuple[StoredPath, ...], Field(min_length=1, max_length=16)]
    cause: SnapshotFault
    purpose: Literal["run", "undo"] = "run"


@final
class GrantPrompt(PolicyModel):
    """What the user is asked, for `run` of `task` (and `tool`, if a tool's handler asked), with
    the model's `reason`: to allow `capability`, to choose a file or folder (`pick`), or to run
    with no undo (`no_undo`, whose `reason` is empty: there is no model text in it). Exactly one
    of the three is set."""

    task: TaskId
    run: RunId
    tool: ToolId | None = None
    tool_name: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    """`tool`'s name from the index: the tool's own text (I5), shown plain, capped, the tool's."""
    capability: Capability | None = None
    pick: Literal["file", "folder"] | None = None
    no_undo: NoUndoSubject | None = None
    scopes: Annotated[frozenset[GrantScope], Field(min_length=1)]
    reason: Annotated[str, Field(max_length=MAX_REASON_CHARS)]

    @model_validator(mode="after")
    def _one_subject(self) -> Self:
        subjects = [self.capability, self.pick, self.no_undo]
        if sum(subject is not None for subject in subjects) != 1:
            raise ValueError("a prompt asks for one thing: a capability, a pick or no undo")
        return self


@final
class Allowed(PolicyModel):
    """The user allowed it, at `scope`, until `expires_at` (never, if null). `picked` is the text of
    the path the user chose, for a picker prompt (and only for one): received text, resolved and
    checked by the broker like any other path, never taken as a decision already made."""

    kind: Literal["allowed"] = "allowed"
    scope: GrantScope
    expires_at: AwareDatetime | None = None
    picked: Annotated[str, Field(min_length=1, max_length=MAX_PICKED_CHARS)] | None = None


@final
class Denied(PolicyModel):
    """The user refused, or nobody could be asked. `reason` is a short fixed code for the log."""

    kind: Literal["denied"] = "denied"
    reason: Annotated[str, Field(max_length=64)] = "user_denied"


type PromptAnswer = Annotated[Allowed | Denied, Field(discriminator="kind")]


class PromptInterface(Protocol):
    """Asks the user. Implementations must not return `Allowed` unless a person at the keyboard
    or pointer decided so (I10: not by voice, not by a timeout)."""

    async def ask(self, prompt: GrantPrompt, owner: Owner, /) -> PromptAnswer:
        """Ask on behalf of `owner`'s connection: the one that made the task, and the only one
        whose answer counts."""
        ...

    def answer(self, owner: Owner, prompt: PromptId, answer: PromptAnswer, /) -> bool:
        """Take the answer `owner` sent to `prompt`. True if that prompt was asked of `owner` and
        was still waiting; False for any other (another connection's, answered, closed, unknown)."""
        ...


@final
class AutoDeny:
    """The prompt interface with nobody to ask: everything is denied, so no grant is ever made."""

    async def ask(self, prompt: GrantPrompt, owner: Owner, /) -> PromptAnswer:
        return Denied(reason="no_prompt_ui")

    def answer(self, owner: Owner, prompt: PromptId, answer: PromptAnswer, /) -> bool:
        return False
