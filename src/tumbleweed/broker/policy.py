"""The decision function: (task state, grants, request) -> allow, deny or needs-prompt.

Pure and deterministic: nothing is read from the file system, the clock or the environment. The
file system is looked at before (`paths.ResolvedPath.resolve`, which the request carries, and
`deny.DenyRules.build`) and after (`StoredPath.open`); the time is an argument; a new grant's ID
is an argument. What `decide` returns the caller must act on and record: it does not change
anything itself (see `Allow`).

`decide` checks, in this order, and the first that fails refuses with a typed reason
(`Refusal`):

1. the request is for this task (`TaskMismatch`), and the task has not ended (`TaskEnded`);
2. the static deny rules (`StaticDeny`): the path equals, is inside or is an ancestor of a
   protected location. Nothing beats this: it comes before every grant, and a stored grant
   that now overlaps the list is not used either (`SkipReason.STATIC_DENY`);
3. the write gate (`WriteGateClosed`): every `fs.write` request is refused until S21 ships
   snapshots and replaces `_write_gate`. A stored `fs.write` grant is not used for anything,
   not even for reading;
4. the grants in force (`select_grants`): one that covers the request is an `Allow`. A grant
   covers a path request by identity (`ResolvedPath.below`), not by text;
5. otherwise a prompt could still end in a grant only if the task's taint allows it
   (`TaintConflict`, I12) and the target can be recorded (`Ungrantable`); if so, `NeedsPrompt`.

`issue_grant` is what happens after the user answers a prompt or picks a file: it makes the same
checks again for the capability the user actually chose and, if they pass, returns the grant and
the task with its taint raised. Whatever calls it must be a trusted decision by the user (I2).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self, assert_never, final

from pydantic import Field, model_validator

from tumbleweed.broker.capabilities import (
    Capability,
    CapabilityKind,
    FsRead,
    FsWrite,
    NetFull,
)
from tumbleweed.broker.deny import DenyRelation, DenyRules, Label
from tumbleweed.broker.errors import PathError, PathFault
from tumbleweed.broker.fdio import OpenedPath, OpenStrategy
from tumbleweed.broker.grants import (
    Grant,
    GrantOrigin,
    GrantScope,
    OnceGrant,
    RunContext,
    RunTrigger,
    TaskGrant,
    ToolGrant,
)
from tumbleweed.broker.model import PolicyModel
from tumbleweed.broker.paths import ResolvedPath, StoredPath
from tumbleweed.broker.task import (
    TaintKind,
    TaskState,
    merged_taint,
    other_taint,
    taint_of,
)
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import GrantId, TaskId
from tumbleweed.core.types import WorkspaceRelPath

# --- requests --------------------------------------------------------------------------------


@final
class AccessRequest(PolicyModel):
    """What a run, or the agent through the broker, asks for.

    `path` is what `ResolvedPath.resolve` found out about the path, for `fs.*` requests, and is
    absent for `net.full`. `run` says which run is asking and what started it: a once grant is
    for that run alone, and a tool grant needs it to be a user-triggered run of that tool.
    """

    task: TaskId
    run: RunContext
    capability: CapabilityKind
    path: ResolvedPath | None = None

    @model_validator(mode="after")
    def _a_path_for_files_only(self) -> Self:
        match self.capability:
            case CapabilityKind.FS_READ | CapabilityKind.FS_WRITE:
                if self.path is None:
                    raise InvalidValueError(f"a {self.capability.value} request needs a path")
            case CapabilityKind.NET_FULL:
                if self.path is not None:
                    raise InvalidValueError("a net.full request has no path")
            case _ as unreachable:
                assert_never(unreachable)
        return self


# --- reasons ---------------------------------------------------------------------------------


@final
class StaticDeny(PolicyModel):
    """The path equals, is inside, or is an ancestor of a protected location."""

    kind: Literal["static_deny"] = "static_deny"
    entry: Label
    relation: DenyRelation


@final
class WriteGateClosed(PolicyModel):
    """Writes are not available until snapshots exist (S21)."""

    kind: Literal["write_gate"] = "write_gate"


@final
class TaintConflict(PolicyModel):
    """The task holds one kind of taint, and the grant would add the one that cannot be held
    with it (I12). The model is told to start a task with `fresh_context=true`."""

    kind: Literal["taint_conflict"] = "taint_conflict"
    held: TaintKind
    wanted: TaintKind


@final
class TaskEnded(PolicyModel):
    kind: Literal["task_ended"] = "task_ended"


@final
class TaskMismatch(PolicyModel):
    """The request is for a different task than the one whose state was given."""

    kind: Literal["task_mismatch"] = "task_mismatch"


@final
class Ungrantable(PolicyModel):
    """The target does not exist, or is not a regular file or directory, or cannot be opened by
    this user: there is nothing a grant could be bound to."""

    kind: Literal["ungrantable"] = "ungrantable"


@final
class ScopeUnavailable(PolicyModel):
    """The scope cannot be had for this request (a tool grant needs a user-triggered run of a
    saved tool)."""

    kind: Literal["scope_unavailable"] = "scope_unavailable"
    scope: GrantScope


@final
class BadExpiry(PolicyModel):
    """The expiry given is not in the future."""

    kind: Literal["bad_expiry"] = "bad_expiry"


type Refusal = Annotated[
    StaticDeny
    | WriteGateClosed
    | TaintConflict
    | TaskEnded
    | TaskMismatch
    | Ungrantable
    | ScopeUnavailable
    | BadExpiry,
    Field(discriminator="kind"),
]


def describe(reason: Refusal) -> str:
    """Fixed text for a refusal, safe to log and to tell the model: it names the rule, never the
    path (the only variable parts are a rule list label and enum values)."""
    match reason:
        case StaticDeny(entry=entry, relation=relation):
            return (
                f"refused: {_relation_phrase(relation)} ({entry}), and those can never be granted"
            )
        case WriteGateClosed():
            return (
                "refused: writing to files is not available yet (it needs undo first); "
                "a sandbox can write only to its own workspace"
            )
        case TaintConflict(held=held, wanted=wanted):
            return (
                f"refused: this task has been given {held.value} access and may not also be "
                f"given {wanted.value} access; start a new task with fresh_context=true"
            )
        case TaskEnded():
            return "refused: the task has ended"
        case TaskMismatch():
            return "refused: the request is for another task"
        case Ungrantable():
            return "refused: only an existing regular file or directory can be granted"
        case ScopeUnavailable(scope=scope):
            return f"refused: the {scope.value} scope is not available for this request"
        case BadExpiry():
            return "refused: a grant must expire in the future"
        case _ as unreachable:
            assert_never(unreachable)


def _relation_phrase(relation: DenyRelation) -> str:
    match relation:
        case DenyRelation.EQUAL:
            return "the path is a protected location"
        case DenyRelation.INSIDE:
            return "the path is inside a protected location"
        case DenyRelation.ANCESTOR:
            return "the path contains a protected location"
        case _ as unreachable:
            assert_never(unreachable)


# --- decisions -------------------------------------------------------------------------------


@final
class Allow(PolicyModel):
    """The request is covered by `grant`. The caller must then:

    - if `consumes`, record `OnceGrant.consume(run)` before acting on the answer, and treat the
      request as refused if it cannot (two runs racing for one grant must not both succeed);
    - store `task_taint` as the task's taint: it is the task's taint plus that of the tool grants
      applied to this run (applying one counts as issuing it), and never smaller.

    `within_grant`, for a file request, is the path from the object the grant was made for down
    to the target (None if the target is that object): open the grant's `StoredPath`, then open
    this below it with `workspace.open_beneath`.
    """

    kind: Literal["allow"] = "allow"
    grant: GrantId
    consumes: bool
    within_grant: WorkspaceRelPath | None = None
    task_taint: frozenset[TaintKind]


@final
class NeedsPrompt(PolicyModel):
    """No grant covers the request, and the user could be asked. `capability` is exactly what
    was requested, in structured form for the prompt to render (I5); `scopes` are the scopes the
    prompt may offer."""

    kind: Literal["needs_prompt"] = "needs_prompt"
    capability: Capability
    scopes: Annotated[frozenset[GrantScope], Field(min_length=1)]


@final
class Deny(PolicyModel):
    """Refused, and no prompt could change that."""

    kind: Literal["deny"] = "deny"
    reason: Refusal


@final
class Issued(PolicyModel):
    """A grant was made: store `grant` and replace the task's state with `task`."""

    kind: Literal["issued"] = "issued"
    grant: Grant
    task: TaskState


type Decision = Annotated[Allow | NeedsPrompt | Deny, Field(discriminator="kind")]
type Issuance = Annotated[Issued | Deny, Field(discriminator="kind")]


# --- the grants in force ---------------------------------------------------------------------


class SkipReason(StrEnum):
    """Why a grant does not apply to a run."""

    EXPIRED = "expired"
    CONSUMED = "consumed"
    OTHER_TASK = "other_task"
    OTHER_RUN = "other_run"
    OTHER_TOOL = "other_tool"  # a different tool, a different version, or no saved tool at all
    NOT_USER_TRIGGERED = "not_user_triggered"  # a tool grant, for a run a user event did not start
    WRITE_GATE = "write_gate"
    STATIC_DENY = "static_deny"
    TAINT_CONFLICT = "taint_conflict"  # I12: withheld from the run


@final
class SkippedGrant(PolicyModel):
    grant: GrantId
    reason: SkipReason


@final
class GrantSelection(PolicyModel):
    """Which grants apply to a run. `task_taint` is the task's taint with that of the applied
    tool grants added."""

    applicable: tuple[Grant, ...]
    skipped: tuple[SkippedGrant, ...]
    task_taint: frozenset[TaintKind]


def _require_aware(moment: datetime, what: str) -> None:
    raw: object = moment  # the annotation is for the checker; callers can pass anything
    if not isinstance(raw, datetime) or raw.utcoffset() is None:
        raise InvalidValueError(f"{what} must be a timezone-aware datetime")


def _binding_skip(grant: Grant, task: TaskState, run: RunContext) -> SkipReason | None:
    match grant:
        case OnceGrant():
            if grant.task != task.task:
                return SkipReason.OTHER_TASK
            if grant.consumed_by is not None:
                return SkipReason.CONSUMED
            if grant.run != run.run:
                return SkipReason.OTHER_RUN
        case TaskGrant():
            if grant.task != task.task:
                return SkipReason.OTHER_TASK
        case ToolGrant():
            if run.trigger is not grant.trigger:
                return SkipReason.NOT_USER_TRIGGERED
            binding = run.tool
            if (
                binding is None
                or binding.tool != grant.tool
                or binding.content_hash != grant.content_hash
            ):
                return SkipReason.OTHER_TOOL
        case _ as unreachable:
            assert_never(unreachable)
    return None


def _skip_reason(
    grant: Grant, task: TaskState, run: RunContext, rules: DenyRules, now: datetime
) -> SkipReason | None:
    if grant.is_expired(now):
        return SkipReason.EXPIRED
    binding = _binding_skip(grant, task, run)
    if binding is not None:
        return binding
    match grant.capability:
        case FsWrite():
            return SkipReason.WRITE_GATE
        case FsRead(path=stored):
            # Deny beats grant even for a grant made before the list grew or before it was
            # forged into a store; by text only, as a stored path has no identities to compare.
            if rules.check_text(stored) is not None:
                return SkipReason.STATIC_DENY
        case NetFull():
            pass
        case _ as unreachable:
            assert_never(unreachable)
    return None


def _taint_skip(taint: frozenset[TaintKind], grant: Grant) -> SkipReason | None:
    wanted = frozenset({taint_of(grant.capability.kind)})
    return SkipReason.TAINT_CONFLICT if merged_taint(taint, wanted) is None else None


def select_grants(
    task: TaskState,
    run: RunContext,
    grants: Sequence[Grant],
    *,
    rules: DenyRules,
    now: datetime,
) -> GrantSelection:
    """Sort `grants` into those that apply to `run` and those that do not, and say why.

    A grant applies if it has not expired, belongs here (a once grant: to this task and this run
    and is not consumed; a task grant: to this task; a tool grant: the run is a user-triggered
    run of exactly that tool and content hash), is not an `fs.write` grant (the write gate),
    does not overlap the deny list, and does not conflict with the task's taint.

    **Applying a tool grant counts as issuing it** (I12): the tool grants that apply are taken
    in order of issue (`issued_at`, then ID) and each adds its taint to the running taint, so
    the first of two conflicting tool grants applies and the second is withheld
    (`TAINT_CONFLICT`), as is any tool grant that conflicts with what the task already holds. A
    once or task grant was issued, and its taint set then, so it only has to agree with the
    task's taint. Everything is returned in order of issue.
    """
    _require_aware(now, "now")
    ordered = sorted(grants, key=lambda grant: (grant.issued_at, grant.id))
    reasons: dict[int, SkipReason | None] = {}
    taint = task.taint
    for index, grant in enumerate(ordered):
        if not isinstance(grant, ToolGrant):
            reason = _skip_reason(grant, task, run, rules, now)
            reasons[index] = _taint_skip(task.taint, grant) if reason is None else reason
    for index, grant in enumerate(ordered):
        if isinstance(grant, ToolGrant):
            reason = _skip_reason(grant, task, run, rules, now)
            if reason is None:
                reason = _taint_skip(taint, grant)
                if reason is None:
                    taint = taint | {taint_of(grant.capability.kind)}
            reasons[index] = reason
    return GrantSelection(
        applicable=tuple(grant for index, grant in enumerate(ordered) if reasons[index] is None),
        skipped=tuple(
            SkippedGrant(grant=grant.id, reason=reason)
            for index, grant in enumerate(ordered)
            if (reason := reasons[index]) is not None
        ),
        task_taint=taint,
    )


# --- the gates -------------------------------------------------------------------------------


def _write_gate(capability: CapabilityKind) -> WriteGateClosed | None:
    """Refuse every write. S21 replaces this with the snapshot-backed gate (I13)."""
    match capability:
        case CapabilityKind.FS_WRITE:
            return WriteGateClosed()
        case CapabilityKind.FS_READ | CapabilityKind.NET_FULL:
            return None
        case _ as unreachable:
            assert_never(unreachable)


def _common_refusal(task: TaskState, request: AccessRequest, rules: DenyRules) -> Refusal | None:
    """The refusals `decide` and `issue_grant` share, in order."""
    if request.task != task.task:
        return TaskMismatch()
    if task.ended:
        return TaskEnded()
    if request.path is not None and (match := rules.check(request.path)) is not None:
        return StaticDeny(entry=match.entry, relation=match.relation)
    return _write_gate(request.capability)


def _capability_of(request: AccessRequest) -> Capability | None:
    """The structured capability a request asks for; None if there is nothing to record."""
    match request.capability:
        case CapabilityKind.FS_READ:
            if request.path is None or request.path.identity is None:
                return None
            return FsRead(path=StoredPath.record(request.path))
        case CapabilityKind.FS_WRITE:
            return None  # the write gate refuses it first; until S21 nothing records a write
        case CapabilityKind.NET_FULL:
            return NetFull()
        case _ as unreachable:
            assert_never(unreachable)


# --- decide ----------------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class _Cover:
    """A grant that covers a request, and where the target is below what it was granted for."""

    grant: Grant
    below: tuple[str, ...]


def _preference(scope: GrantScope) -> int:
    """Which covering grant to use first: one that is not spent by using it."""
    match scope:
        case GrantScope.TASK:
            return 0
        case GrantScope.TOOL:
            return 1
        case GrantScope.ONCE:
            return 2
        case _ as unreachable:
            assert_never(unreachable)


def _cover(grant: Grant, request: AccessRequest) -> _Cover | None:
    capability = grant.capability
    match request.capability:
        case CapabilityKind.FS_READ:
            if not isinstance(capability, FsRead) or request.path is None:
                return None
            below = request.path.below(capability.path)
            return None if below is None else _Cover(grant, below)
        case CapabilityKind.NET_FULL:
            return _Cover(grant, ()) if isinstance(capability, NetFull) else None
        case CapabilityKind.FS_WRITE:
            return None
        case _ as unreachable:
            assert_never(unreachable)


def _scopes_for(run: RunContext) -> frozenset[GrantScope]:
    scopes = {GrantScope.ONCE, GrantScope.TASK}
    if run.tool is not None and run.trigger is RunTrigger.USER_EVENT:
        scopes.add(GrantScope.TOOL)
    return frozenset(scopes)


def decide(
    task: TaskState,
    grants: Sequence[Grant],
    request: AccessRequest,
    *,
    rules: DenyRules,
    now: datetime,
) -> Decision:
    """Decide `request`: allow it, deny it, or say that the user must be asked. See the module
    docstring for the order of the checks.

    `grants` is every grant the caller holds that could matter (those of the task, once grants
    included, and tool grants); this function picks what applies. When several cover the
    request it uses a task grant before a tool grant before a once grant, so that a once grant
    is spent only if nothing else would do, then the earliest issued.
    """
    _require_aware(now, "now")
    refusal = _common_refusal(task, request, rules)
    if refusal is not None:
        return Deny(reason=refusal)
    selection = select_grants(task, request.run, grants, rules=rules, now=now)
    covers = [
        cover for grant in selection.applicable if (cover := _cover(grant, request)) is not None
    ]
    if covers:
        best = min(covers, key=lambda c: (_preference(c.grant.kind), c.grant.issued_at, c.grant.id))
        return Allow(
            grant=best.grant.id,
            consumes=isinstance(best.grant, OnceGrant),
            within_grant=WorkspaceRelPath("/".join(best.below)) if best.below else None,
            task_taint=selection.task_taint,
        )
    wanted = taint_of(request.capability)
    if merged_taint(selection.task_taint, frozenset({wanted})) is None:
        return Deny(reason=TaintConflict(held=other_taint(wanted), wanted=wanted))
    capability = _capability_of(request)
    if capability is None:
        return Deny(reason=Ungrantable())
    return NeedsPrompt(capability=capability, scopes=_scopes_for(request.run))


# --- issuing a grant -------------------------------------------------------------------------


def issue_grant(
    task: TaskState,
    request: AccessRequest,
    *,
    scope: GrantScope,
    origin: GrantOrigin,
    grant_id: GrantId,
    rules: DenyRules,
    now: datetime,
    expires_at: datetime | None = None,
) -> Issuance:
    """Make the grant the user decided on, or refuse.

    `request` is the capability the user chose, which may be more than the model asked for (a
    folder picked for a file asked for): its `path` is the path to grant, from
    `ResolvedPath.resolve` taken **before** the user was asked, so that what the user saw is what
    gets recorded. The refusals come in this order: the checks of `decide` (task, static deny
    with the ancestor rule, write gate), a target that cannot be recorded (it must exist),
    an `expires_at` that is not in the future, a scope that is not available (`tool` needs a
    user-triggered run of a saved tool) and, last, a taint that conflicts. On success the
    returned task has the new grant's taint, whether or not the grant is ever used (I12), and
    the grant is bound to `request.run` (once) or to its tool and content hash (tool).
    """
    _require_aware(now, "now")
    if expires_at is not None:
        _require_aware(expires_at, "expires_at")
    refusal = _common_refusal(task, request, rules)
    if refusal is not None:
        return Deny(reason=refusal)
    capability = _capability_of(request)
    if capability is None:
        return Deny(reason=Ungrantable())
    if expires_at is not None and expires_at <= now:
        return Deny(reason=BadExpiry())
    grant = _new_grant(task, request, capability, scope, origin, grant_id, now, expires_at)
    if grant is None:
        return Deny(reason=ScopeUnavailable(scope=scope))
    wanted = taint_of(request.capability)
    tainted = task.with_taint(frozenset({wanted}))
    if tainted is None:
        return Deny(reason=TaintConflict(held=other_taint(wanted), wanted=wanted))
    return Issued(grant=grant, task=tainted)


def _new_grant(
    task: TaskState,
    request: AccessRequest,
    capability: Capability,
    scope: GrantScope,
    origin: GrantOrigin,
    grant_id: GrantId,
    now: datetime,
    expires_at: datetime | None,
) -> Grant | None:
    """The grant of `scope` for `request`, or None if that scope is not available for it."""
    match scope:
        case GrantScope.ONCE:
            return OnceGrant(
                id=grant_id,
                capability=capability,
                origin=origin,
                issued_at=now,
                expires_at=expires_at,
                task=task.task,
                run=request.run.run,
            )
        case GrantScope.TASK:
            return TaskGrant(
                id=grant_id,
                capability=capability,
                origin=origin,
                issued_at=now,
                expires_at=expires_at,
                task=task.task,
            )
        case GrantScope.TOOL:
            binding = request.run.tool
            if binding is None or request.run.trigger is not RunTrigger.USER_EVENT:
                return None
            return ToolGrant(
                id=grant_id,
                capability=capability,
                origin=origin,
                issued_at=now,
                expires_at=expires_at,
                tool=binding.tool,
                content_hash=binding.content_hash,
            )
        case _ as unreachable:
            assert_never(unreachable)


# --- using a grant ---------------------------------------------------------------------------


def open_granted(
    path: StoredPath, *, rules: DenyRules, strategy: OpenStrategy = OpenStrategy.AUTO
) -> OpenedPath:
    """Open the object a grant was made for, to bind it into a run: the run-time half of a grant.

    First the deny rules once more, because deny beats grant even for a grant made before the
    list grew (`PathError(PROTECTED)`; by text, a stored path having no identities to compare);
    then `StoredPath.open`: component by component, no symlink followed, the identity verified,
    nothing resolved again. Any failure refuses the run, and the user is asked again. The caller
    closes the result.
    """
    if rules.check_text(path) is not None:
        raise PathError(PathFault.PROTECTED)
    return path.open(strategy)
