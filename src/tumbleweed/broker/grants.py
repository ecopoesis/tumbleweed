"""Grants, as data: what was granted, to whom, for how long, and whether it is spent.

A grant is a capability (`capabilities`) with a **scope** (ARCHITECTURE section 7):

- `once` is bound to the one run, a handler call or `run_code`, whose request caused the
  prompt, and is consumed by it (`OnceGrant.consume`). No other run can use it, and no run can
  use it twice.
- `task` lasts until the task ends.
- `tool` is persisted, bound to a `ToolId` **and** the exact version's `ContentHash` (a new
  version must be granted again), and applies only to sandbox handler runs triggered by a direct
  user input event on an instance of that tool: runs triggered by the model, by `patch_state`
  or by a connection get task grants only (I8). It is data here and applied by
  `policy.select_grants`; the shell wiring that creates tool grants and the runs they apply to
  is S24.

Every grant can also expire (`expires_at`; a grant is expired from that instant on). Whether a
grant is in force is decided by `policy`, against a `now` it is given, never by reading a clock
here. Expiry therefore means the caller's clock: one that is turned back brings an expired grant
back to life, so the daemon must use a clock it trusts.

These are boundary models (they are stored and sent over IPC): strict, frozen, closed.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self, final

from pydantic import AwareDatetime, Field, model_validator

from tumbleweed.broker.capabilities import Capability
from tumbleweed.broker.errors import GrantError
from tumbleweed.broker.model import PolicyModel
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import GrantId, RunId, TaskId, ToolId, ToolInstanceId
from tumbleweed.core.types import ContentHash


class GrantScope(StrEnum):
    """How long a grant lasts and who can use it."""

    ONCE = "once"
    TASK = "task"
    TOOL = "tool"


class GrantOrigin(StrEnum):
    """Which trusted prompt the user decided on. Both are the user's decision (I2)."""

    PROMPT = "prompt"  # allow once, for this task, or for this tool
    PICKER = "picker"  # the user chose the file or folder in the trusted picker


class RunTrigger(StrEnum):
    """What started a sandbox run."""

    USER_EVENT = "user_event"  # a direct user input event on a widget
    MODEL = "model"  # the agent: `run_code`, or a broker call it made
    STATE_PATCH = "state_patch"  # `patch_state`
    CONNECTION = "connection"  # a value arriving over a `connect_tools` wire


@final
class ToolBinding(PolicyModel):
    """The saved tool a run executes: the instance on the canvas, the tool, and the content
    hash of the version the instance runs."""

    instance: ToolInstanceId
    tool: ToolId
    content_hash: ContentHash


@final
class RunContext(PolicyModel):
    """The run a request comes from: its ID, what triggered it, and the saved tool it runs.

    `tool` is None for `run_code` and for a tool that has not been saved: no tool grant applies
    to such a run.
    """

    run: RunId
    trigger: RunTrigger
    tool: ToolBinding | None = None


class _GrantBase(PolicyModel):
    id: GrantId
    capability: Capability
    origin: GrantOrigin
    issued_at: AwareDatetime
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _expires_after_issue(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.issued_at:
            raise InvalidValueError("a grant must expire after it was issued")
        return self

    def is_expired(self, now: datetime) -> bool:
        """Whether the grant has expired at `now` (an aware time). From `expires_at` on."""
        return self.expires_at is not None and now >= self.expires_at


@final
class OnceGrant(_GrantBase):
    """Bound to one run of one task, and used up by it."""

    kind: Literal[GrantScope.ONCE] = GrantScope.ONCE
    task: TaskId
    run: RunId
    consumed_by: RunId | None = None

    @model_validator(mode="after")
    def _consumed_only_by_its_run(self) -> Self:
        if self.consumed_by is not None and self.consumed_by != self.run:
            raise InvalidValueError("a once grant is consumed only by the run it is bound to")
        return self

    def consume(self, by: RunId) -> Self:
        """The grant after `by` used it. `GrantError` if `by` is not its run or it is spent."""
        if by != self.run:
            raise GrantError("a once grant can be consumed only by the run it is bound to")
        if self.consumed_by is not None:
            raise GrantError("the once grant was already consumed")
        return type(self)(
            id=self.id,
            capability=self.capability,
            origin=self.origin,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            task=self.task,
            run=self.run,
            consumed_by=by,
        )


@final
class TaskGrant(_GrantBase):
    """Lasts until the task ends."""

    kind: Literal[GrantScope.TASK] = GrantScope.TASK
    task: TaskId


@final
class ToolGrant(_GrantBase):
    """Bound to one version of one saved tool; applies only to runs a user event triggered.

    `trigger` is that restriction, as data: the one kind of run the grant applies to. It has
    one possible value, and `policy.select_grants` compares it with the run's trigger.
    """

    kind: Literal[GrantScope.TOOL] = GrantScope.TOOL
    tool: ToolId
    content_hash: ContentHash
    trigger: Literal[RunTrigger.USER_EVENT] = RunTrigger.USER_EVENT


type Grant = Annotated[OnceGrant | TaskGrant | ToolGrant, Field(discriminator="kind")]
