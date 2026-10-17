"""The broker's service: tasks, grants, file access and sandbox runs, behind one `handle`.

This is the traffic cop (S17). `BrokerService.handle(owner, request)` takes one validated IPC
request and returns one response (`Refused` is a response: the broker's "no"). It owns what must
be one thing: the live tasks and their grants in memory, the database and the audit log they are
written to, and the workspaces. The pure decisions are S16's (`policy.decide`, `issue_grant`,
`select_grants`); everything here is what makes them true.

**Owners.** A task belongs to the connection that made it (`Owner`, one per IPC connection, given
by the daemon). A request about a task of another connection is answered `unknown_task`. When a
connection ends, its tasks end (a shell restart ends the current task: D7), and a broker that
starts finds no live task: whatever a stopped broker left live is ended, its grants revoked.

**Atomic sections.** There is no lock because there is no `await` between a decision and the
record of what it implies: `decide` or `select_grants`, the consumption of a once grant, the new
taint and the audit entries are one synchronous block (`_atomic`), so two requests racing for a
once grant cannot both win and a decision cannot outlive the state it was made on. Anything that
waits (the file system, the prompt, a sandbox) happens before or after such a block, and the
block re-reads the task, so a task that ended in the meantime refuses.

**Audit before act (I7).** The audit entries of a decision are written inside the transaction
that records it: if one cannot be written the transaction rolls back and the request is refused
(`unavailable`), so nothing privileged happens unrecorded. A run has two entries, written before
it starts and after it ends. Reads and listings of a task's own workspace are not decisions and
are not audited.

**The file system can hang.** Decision-time and run-time opens, walks, reads and deletions go
through `FsWorker`, which abandons a call after a deadline; the request then fails with `timeout`.

**Refusals never carry input.** A `Refused` message is fixed text: paths, commands and the
model's reason are never repeated in a response, a log line or an exception message, and no
`ValidationError` text ever leaves this module.
"""

import asyncio
import base64
import contextlib
import dataclasses
import logging
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Final, Literal, NewType, Protocol, assert_never, final

from tumbleweed.broker.audit import (
    AuditEntry,
    AuditLog,
    DaemonStart,
    FileAccess,
    GrantDecision,
    GrantRevoked,
    SandboxRun,
    SandboxRunResult,
    TaskEndReason,
    WorkspaceDeleted,
    cut,
)
from tumbleweed.broker.audit import TaskEnded as TaskEndedEntry
from tumbleweed.broker.audit import TaskStarted as TaskStartedEntry
from tumbleweed.broker.capabilities import CapabilityKind, FsRead
from tumbleweed.broker.compose import InvalidRunError, Mount, disjoint_mounts, sandbox_environment
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.deny import DenyRules
from tumbleweed.broker.errors import (
    AuditError,
    FsTimeoutError,
    PathError,
    PathFault,
    StoreError,
    WorkspaceError,
    describe_fault,
)
from tumbleweed.broker.fdio import FileKind, OpenedPath, read_capped
from tumbleweed.broker.fsworker import FsWorker
from tumbleweed.broker.grants import (
    Grant,
    GrantOrigin,
    OnceGrant,
    RunContext,
    RunTrigger,
    TaskGrant,
    ToolGrant,
)
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.paths import ResolvedPath, StoredPath
from tumbleweed.broker.policy import (
    AccessRequest,
    Allow,
    BadExpiry,
    Deny,
    GrantSelection,
    Issued,
    NeedsPrompt,
    Refusal,
    ScopeUnavailable,
    StaticDeny,
    TaintConflict,
    TaskEnded,
    TaskMismatch,
    Ungrantable,
    WriteGateClosed,
    decide,
    describe,
    issue_grant,
    open_granted,
    select_grants,
)
from tumbleweed.broker.prompts import Allowed, Denied, GrantPrompt, PromptAnswer, PromptInterface
from tumbleweed.broker.store import BrokerStore, TaskRecord
from tumbleweed.broker.task import TaintKind, TaskState
from tumbleweed.broker.workspace import open_in_workspace, read_workspace_file
from tumbleweed.broker.workspaces import Listing, Workspaces, list_directory
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import GrantId, RunId, TaskId
from tumbleweed.core.types import ByteCount, WorkspaceRelPath
from tumbleweed.ipc.broker_messages import (
    DirEntry,
    DirListing,
    EndTask,
    FileContent,
    GrantOutcome,
    ListDir,
    ListWorkspace,
    NetworkAccess,
    ReadAccess,
    ReadFile,
    ReadWorkspaceFile,
    RefusalReason,
    Refused,
    RequestGrant,
    Revoked,
    RevokeGrant,
    RunInSandbox,
    RunResult,
    Scope,
    StartTask,
    Taint,
    TaskStarted,
    WriteAccess,
)
from tumbleweed.ipc.broker_messages import TaskEnded as TaskEndedReply
from tumbleweed.ipc.errors import ErrorCode, RequestRefused
from tumbleweed.ipc.messages import Echo, Ping, RequestBody, ResponseBody
from tumbleweed.sandbox.errors import (
    InvalidPolicyError,
    SandboxLaunchError,
    SandboxTeardownError,
    SandboxUnavailable,
    SandboxUnsupported,
)
from tumbleweed.sandbox.limits import LimitsPlan
from tumbleweed.sandbox.policy import (
    EnvName,
    EnvVar,
    GrantAccess,
    GrantMount,
    MountPath,
    SandboxLimits,
    SandboxPolicy,
)
from tumbleweed.sandbox.result import (
    LandlockState,
    LimitKind,
    SandboxResult,
    Termination,
)

_log = logging.getLogger(__name__)

Owner = NewType("Owner", int)
"""One IPC connection, as far as the service is concerned: the daemon makes one per connection."""

MAX_GRANTS_PER_TASK: Final = 64
_PROBE_WAIT: Final = timedelta(seconds=15)
_SWEEP_BATCH: Final = 100
_UNAVAILABLE_AUDIT: Final = "the audit log is unavailable, so nothing is done"


class Sandbox(Protocol):
    """What the service needs of a sandbox runner (`tumbleweed.sandbox.runner.SandboxRunner`)."""

    async def run(self, policy: SandboxPolicy) -> SandboxResult: ...

    async def limits_plan(self) -> LimitsPlan: ...

    async def aclose(self) -> None: ...


class _Refuse(Exception):  # noqa: N818  (it is an answer, not a failure)
    """Internal: stop the request and answer `Refused(reason, message)`. Never leaves the module."""

    def __init__(self, reason: RefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(slots=True)
class _Live:
    """A live task: its policy state, who owns it and what it holds."""

    owner: Owner
    state: TaskState
    workspace: FsIdentity
    grants: list[Grant] = field(default_factory=list)
    runs: set[RunId] = field(default_factory=set)
    running: set["asyncio.Future[ResponseBody]"] = field(default_factory=set)


@dataclass(slots=True)
class _RunPaths:
    """The descriptors of one run: its workspace and the grants it mounts. Closed after the run."""

    workspace: OpenedPath
    mounts: tuple[OpenedPath, ...]

    def close(self) -> None:
        for opened in (self.workspace, *self.mounts):
            opened.close()


def _close_paths(paths: _RunPaths) -> None:
    paths.close()


def utc_now() -> datetime:
    return datetime.now(UTC)


def _taint_names(taint: frozenset[TaintKind]) -> tuple[Taint, ...]:
    names: list[Taint] = []
    for kind in sorted(taint, key=lambda item: item.value):
        match kind:
            case TaintKind.FS:
                names.append("fs")
            case TaintKind.NET:
                names.append("net")
            case _ as unreachable:
                assert_never(unreachable)
    return tuple(names)


def _wire_scope(grant: Grant) -> Scope | None:
    match grant:
        case OnceGrant():
            return "once"
        case TaskGrant():
            return "task"
        case ToolGrant():
            return None
        case _ as unreachable:
            assert_never(unreachable)


def _reason_of_fault(fault: PathFault) -> RefusalReason:
    match fault:
        case PathFault.INVALID | PathFault.WRONG_KIND:
            return "invalid_request"
        case PathFault.SYMLINK | PathFault.SPECIAL_FILE | PathFault.IDENTITY_MISMATCH:
            return "unsafe"
        case PathFault.MISSING:
            return "not_found"
        case PathFault.DENIED | PathFault.PROTECTED:
            return "denied"
        case PathFault.TOO_LARGE:
            return "too_large"
        case PathFault.UNSUPPORTED | PathFault.IO:
            return "unavailable"
        case _ as unreachable:
            assert_never(unreachable)


def _reason_of_refusal(refusal: Refusal) -> RefusalReason:
    match refusal:
        case StaticDeny() | WriteGateClosed() | TaintConflict():
            return "denied"
        case TaskEnded() | TaskMismatch():
            return "unknown_task"
        case Ungrantable():
            return "not_found"
        case ScopeUnavailable() | BadExpiry():
            return "invalid_request"
        case _ as unreachable:
            assert_never(unreachable)


def _termination(value: Termination) -> Literal["exited", "timed_out", "output_limit"]:
    match value:
        case Termination.EXITED:
            return "exited"
        case Termination.TIMED_OUT:
            return "timed_out"
        case Termination.OUTPUT_LIMIT:
            return "output_limit"
        case _ as unreachable:
            assert_never(unreachable)


def _landlock(value: LandlockState) -> Literal["applied", "unavailable", "unknown"]:
    match value:
        case LandlockState.APPLIED:
            return "applied"
        case LandlockState.UNAVAILABLE:
            return "unavailable"
        case LandlockState.UNKNOWN:
            return "unknown"
        case _ as unreachable:
            assert_never(unreachable)


def _limit(value: LimitKind) -> Literal["memory_total", "pids", "cpu_rate"]:
    match value:
        case LimitKind.MEMORY_TOTAL:
            return "memory_total"
        case LimitKind.PIDS:
            return "pids"
        case LimitKind.CPU_RATE:
            return "cpu_rate"
        case _ as unreachable:
            assert_never(unreachable)


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@final
class BrokerService:
    """The broker's state and behaviour, without the transport. See the module docstring."""

    def __init__(
        self,
        *,
        config: BrokerConfig,
        store: BrokerStore,
        audit_log: AuditLog,
        workspaces: Workspaces,
        fs: FsWorker,
        sandbox: Sandbox,
        prompts: PromptInterface,
        environ: Mapping[str, str],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._store = store
        self._audit = audit_log
        self._workspaces = workspaces
        self._fs = fs
        self._sandbox = sandbox
        self._prompts = prompts
        self._clock = clock
        self._deny = config.deny_context(environ)
        self._live: dict[TaskId, _Live] = {}
        self._slots = asyncio.Semaphore(config.max_concurrent_runs)
        self._sweeper: asyncio.Task[None] | None = None

    # --- life cycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        """End what a stopped broker left live, remove workspaces nobody owns, sweep retention,
        and start the periodic sweep. Audits the start."""
        now = self._now()
        self._audit.append(
            DaemonStart(
                at=now,
                pid=_pid(),
                require_landlock=self._config.require_landlock,
                require_scope_limits=self._config.require_scope_limits,
            )
        )
        for record in self._store.live_tasks():
            self._end_stale(record, now)
        await self._remove_orphans()
        await self.sweep()
        self._sweeper = asyncio.get_running_loop().create_task(
            self._sweep_forever(), name="broker-retention-sweep"
        )

    async def close(self) -> None:
        """Stop the sweep and any run, and release the files. The service is not usable after."""
        sweeper, self._sweeper = self._sweeper, None
        if sweeper is not None:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
        running = [future for live in self._live.values() for future in live.running]
        for future in running:
            future.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        self._fs.close()
        self._workspaces.close()
        self._audit.close()
        self._store.close()

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.utcoffset() is None:
            raise InvalidValueError("the broker's clock must give aware times")
        return moment

    def _end_stale(self, record: TaskRecord, now: datetime) -> None:
        """End a task a previous broker left live. Its grants are revoked and the end audited."""
        live = _Live(
            owner=Owner(-1),
            state=TaskState(task=record.task, taint=record.taint),
            workspace=record.workspace,
            grants=self._store.grants_of(record.task),
        )
        with self._atomic() as entries:
            self._end_stored(live, TaskEndReason.STARTUP, now, entries)

    # --- retention -----------------------------------------------------------------------------

    async def sweep(self) -> int:
        """Delete the workspaces of tasks that ended more than the retention period ago. Returns
        how many were deleted. A workspace that cannot be deleted is audited and tried again at
        the next sweep."""
        cutoff = self._now() - self._config.retention
        deleted = 0
        for record in self._store.workspaces_to_delete(cutoff)[:_SWEEP_BATCH]:
            outcome: Literal["deleted", "missing", "failed"]
            reason: str | None = None
            try:
                outcome = await self._fs.call(
                    partial(self._workspaces.delete, record.task),
                    deadline=self._config.delete_timeout,
                )
            except (WorkspaceError, FsTimeoutError) as error:
                outcome, reason = "failed", type(error).__name__
                _log.warning("a workspace could not be deleted: %s", reason)
            if outcome != "failed":
                deleted += 1
            with self._atomic() as entries:
                if outcome != "failed":
                    self._store.mark_workspace_deleted(record.task)
                entries.append(
                    WorkspaceDeleted(
                        at=self._now(), task=record.task, outcome=outcome, reason=reason
                    )
                )
        return deleted

    async def _remove_orphans(self) -> None:
        """Delete directories named like a task that the database does not know: a crash between
        making a workspace and recording it leaves one. Other names are left alone."""
        known = self._store.known_tasks()
        for name in await self._fs.call(self._workspaces.names):
            try:
                task = TaskId(name)
            except InvalidValueError:
                continue
            if task in known:
                continue
            with contextlib.suppress(WorkspaceError, FsTimeoutError):
                await self._fs.call(
                    partial(self._workspaces.delete, task), deadline=self._config.delete_timeout
                )

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(self._config.sweep_interval.total_seconds())
            try:
                await self.sweep()
            except (AuditError, StoreError, FsTimeoutError):
                _log.exception("the retention sweep failed; it is tried again at the next one")

    # --- one request ---------------------------------------------------------------------------

    async def handle(self, owner: Owner, request: RequestBody) -> ResponseBody:
        """Answer one request of the connection `owner`. Raises only what the IPC layer should
        turn into an `internal` error (a bug) or `RequestRefused` (a request this peer never
        serves)."""
        try:
            return await self._dispatch(owner, request)
        except _Refuse as refusal:
            return Refused(kind="refused", reason=refusal.reason, message=refusal.message)
        except FsTimeoutError:
            return Refused(
                kind="refused", reason="timeout", message="the file system did not answer in time"
            )
        except PathError as error:
            return Refused(
                kind="refused",
                reason=_reason_of_fault(error.fault),
                message=describe_fault(error.fault),
            )
        except AuditError:
            return Refused(kind="refused", reason="unavailable", message=_UNAVAILABLE_AUDIT)
        except StoreError:
            return Refused(
                kind="refused", reason="unavailable", message="the broker's database is unavailable"
            )
        except WorkspaceError:
            return Refused(
                kind="refused", reason="unavailable", message="the workspace is unavailable"
            )
        except InvalidRunError as error:
            return Refused(kind="refused", reason="invalid_request", message=str(error))
        except InvalidValueError:
            # Its text can quote the input; the answer says only that it was not acceptable.
            return Refused(
                kind="refused",
                reason="invalid_request",
                message="the request holds a value that is not acceptable",
            )

    async def _dispatch(self, owner: Owner, request: RequestBody) -> ResponseBody:
        match request:
            case StartTask():
                return await self._start_task(owner, request)
            case EndTask():
                return self._end_task(owner, request)
            case RequestGrant():
                return await self._request_grant(owner, request)
            case RevokeGrant():
                return self._revoke_grant(owner, request)
            case ReadWorkspaceFile():
                return await self._read_workspace_file(owner, request)
            case ReadFile():
                return await self._read_file(owner, request)
            case ListWorkspace():
                return await self._list_workspace(owner, request)
            case ListDir():
                return await self._list_dir(owner, request)
            case RunInSandbox():
                return await self._run_in_sandbox(owner, request)
            case Ping() | Echo():
                raise RequestRefused(ErrorCode.UNSUPPORTED_REQUEST, "not a broker request")
            case _:
                assert_never(request)

    def connection_closed(self, owner: Owner) -> None:
        """End every task of a connection that has gone (revoking its grants, stopping its runs)."""
        for live in [live for live in self._live.values() if live.owner == owner]:
            try:
                with self._atomic() as entries:
                    self._end_stored(live, TaskEndReason.CONNECTION_CLOSED, self._now(), entries)
            except (AuditError, StoreError):
                # The connection is gone whatever happens: the task must not stay usable. The
                # database still says live, and the next start ends it.
                _log.exception("a task of a closed connection could not be ended on record")
            self._apply_ended(live)

    # --- atomic sections and the end of a task -------------------------------------------------

    @contextmanager
    def _atomic(self) -> Iterator[list[AuditEntry]]:
        """A database transaction whose audit entries are written just before it commits.

        Append the entries to the list the block is given. If one cannot be written, the
        transaction is rolled back and `AuditError` propagates: the act did not happen.
        """
        entries: list[AuditEntry] = []
        with self._store.transaction():
            yield entries
            for entry in entries:
                self._audit.append(entry)

    def _end_stored(
        self, live: _Live, reason: TaskEndReason, now: datetime, entries: list[AuditEntry]
    ) -> list[GrantId]:
        """Record the end of a task and the revocation of its grants (inside `_atomic`)."""
        task = live.state.task
        revoked = [grant.id for grant in live.grants]
        for grant in revoked:
            self._store.revoke_grant(grant, now)
            entries.append(GrantRevoked(at=now, task=task, grant=grant, reason="task_ended"))
        self._store.end_task(task, now)
        entries.append(TaskEndedEntry(at=now, task=task, reason=reason, revoked=tuple(revoked)))
        return revoked

    def _apply_ended(self, live: _Live) -> None:
        """Make an ended task unusable in memory and stop its runs."""
        self._live.pop(live.state.task, None)
        live.state = live.state.end()
        live.grants.clear()
        for future in list(live.running):
            future.cancel()

    def _owned(self, owner: Owner, task: TaskId) -> _Live:
        live = self._live.get(task)
        if live is None or live.owner != owner:
            raise _Refuse("unknown_task", "no such task")
        return live

    def _grants_for(self, live: _Live) -> list[Grant]:
        return [*live.grants, *self._store.tool_grants()]

    # --- tasks ---------------------------------------------------------------------------------

    async def _start_task(self, owner: Owner, request: StartTask) -> ResponseBody:
        previous_live: _Live | None = None
        previous: TaskState | None = None
        if request.previous is not None:
            previous_live = self._live.get(request.previous)
            if previous_live is not None:
                if previous_live.owner != owner:
                    raise _Refuse("unknown_task", "no such task")
                previous = previous_live.state
            else:
                record = self._store.task(request.previous)
                if record is None:
                    raise _Refuse("unknown_task", "no such task")
                previous = TaskState(task=record.task, taint=record.taint, ended=True)
        if len(self._live) - (previous_live is not None) >= self._config.max_live_tasks:
            raise _Refuse("busy", "too many tasks are live")
        task = TaskId.new()
        state = (
            TaskState(task=task)
            if previous is None
            else previous.successor(task, fresh_context=request.fresh_context)
        )
        identity = await self._fs.call(partial(self._workspaces.create, task))
        try:
            now = self._now()
            replaced = (
                previous_live
                if previous_live is not None
                and self._live.get(previous_live.state.task) is previous_live
                else None
            )
            with self._atomic() as entries:
                if replaced is not None:
                    self._end_stored(replaced, TaskEndReason.REPLACED, now, entries)
                self._store.insert_task(
                    TaskRecord(
                        task=task,
                        taint=state.taint,
                        created_at=now,
                        ended_at=None,
                        workspace_present=True,
                        workspace=identity,
                    )
                )
                entries.append(
                    TaskStartedEntry(
                        at=now,
                        task=task,
                        previous=request.previous,
                        fresh_context=request.fresh_context,
                        taint=_taint_names(state.taint),
                    )
                )
        except BaseException:
            await self._discard_workspace(task)
            raise
        if replaced is not None:
            self._apply_ended(replaced)
        self._live[task] = _Live(owner=owner, state=state, workspace=identity)
        return TaskStarted(kind="task_started", task=task, taint=_taint_names(state.taint))

    async def _discard_workspace(self, task: TaskId) -> None:
        """Remove a workspace whose task could not be recorded. Best effort: a failure leaves an
        orphan that the next start removes."""
        with contextlib.suppress(WorkspaceError, FsTimeoutError):
            await self._fs.call(
                partial(self._workspaces.delete, task), deadline=self._config.delete_timeout
            )

    def _end_task(self, owner: Owner, request: EndTask) -> ResponseBody:
        live = self._owned(owner, request.task)
        with self._atomic() as entries:
            revoked = self._end_stored(live, TaskEndReason.REQUESTED, self._now(), entries)
        self._apply_ended(live)
        return TaskEndedReply(kind="task_ended", revoked_grants=len(revoked))

    # --- grants --------------------------------------------------------------------------------

    def _revoke_grant(self, owner: Owner, request: RevokeGrant) -> ResponseBody:
        live = self._owned(owner, request.task)
        found = next((grant for grant in live.grants if grant.id == request.grant), None)
        if found is None:
            raise _Refuse("not_found", "no such grant")
        with self._atomic() as entries:
            self._store.revoke_grant(found.id, self._now())
            entries.append(
                GrantRevoked(
                    at=self._now(), task=live.state.task, grant=found.id, reason="requested"
                )
            )
        live.grants.remove(found)
        return Revoked(kind="revoked")

    async def _resolve(self, text: str) -> ResolvedPath:
        resolved: ResolvedPath = await self._fs.call(partial(ResolvedPath.resolve, text))
        return resolved

    async def _rules(self) -> DenyRules:
        rules: DenyRules = await self._fs.call(partial(DenyRules.build, self._deny))
        return rules

    async def _request_grant(self, owner: Owner, request: RequestGrant) -> ResponseBody:
        self._owned(owner, request.task)
        capability, resolved = await self._access_target(request)
        rules = await self._rules()
        context = RunContext(run=request.run, trigger=RunTrigger.MODEL, tool=None)
        asked = AccessRequest(task=request.task, run=context, capability=capability, path=resolved)
        path_text = None if resolved is None else resolved.text
        # From here to the prompt there is no `await`: the decision is made on the state as it is.
        live = self._owned(owner, request.task)
        decision = decide(live.state, self._grants_for(live), asked, rules=rules, now=self._now())
        match decision:
            case Allow(grant=grant_id):
                covering = next((g for g in self._grants_for(live) if g.id == grant_id), None)
                self._audit.append(
                    self._decision(
                        request,
                        capability,
                        path_text,
                        "covered",
                        grant=grant_id,
                        scope=None if covering is None else _wire_scope(covering),
                        reason=None,
                    )
                )
                return GrantOutcome(
                    kind="grant_outcome",
                    granted=True,
                    grant=grant_id,
                    scope=None if covering is None else _wire_scope(covering),
                    message="already covered by a grant",
                )
            case Deny(reason=refusal):
                return self._denied(request, capability, path_text, refusal.kind, describe(refusal))
            case NeedsPrompt() as needs:
                prompt = GrantPrompt(
                    task=request.task,
                    run=request.run,
                    capability=needs.capability,
                    scopes=needs.scopes,
                    reason=request.reason,
                )
            case _:
                assert_never(decision)
        answer = await self._ask(prompt)
        if isinstance(answer, Denied):
            return self._denied(
                request, capability, path_text, answer.reason, "the user did not allow it"
            )
        rules = await self._rules()  # rebuilt at issuance (S16): the list may have grown
        live = self._owned(owner, request.task)
        if len(live.grants) >= MAX_GRANTS_PER_TASK:
            raise _Refuse("busy", "the task holds as many grants as it may")
        issuance = issue_grant(
            live.state,
            asked,
            scope=answer.scope,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=rules,
            now=self._now(),
            expires_at=answer.expires_at,
            grants=self._grants_for(live),
        )
        match issuance:
            case Deny(reason=refusal):
                return self._denied(request, capability, path_text, refusal.kind, describe(refusal))
            case Issued(grant=grant, task=state):
                scope = _wire_scope(grant)
                with self._atomic() as entries:
                    self._store.insert_grant(grant, live.state.task)
                    self._store.set_taint(live.state.task, state.taint)
                    entries.append(
                        self._decision(
                            request,
                            capability,
                            path_text,
                            "granted",
                            grant=grant.id,
                            scope=scope,
                            reason=None,
                            expires_at=grant.expires_at,
                        )
                    )
                live.grants.append(grant)
                live.state = state
                return GrantOutcome(
                    kind="grant_outcome",
                    granted=True,
                    grant=grant.id,
                    scope=scope,
                    message="granted by the user",
                )
            case _:
                assert_never(issuance)

    async def _access_target(
        self, request: RequestGrant
    ) -> tuple[CapabilityKind, ResolvedPath | None]:
        match request.access:
            case ReadAccess(path=text):
                return CapabilityKind.FS_READ, await self._resolve(text)
            case WriteAccess(path=text):
                return CapabilityKind.FS_WRITE, await self._resolve(text)
            case NetworkAccess():
                return CapabilityKind.NET_FULL, None
            case _ as unreachable:
                assert_never(unreachable)

    async def _ask(self, prompt: GrantPrompt) -> PromptAnswer:
        """The user's answer, or a denial: a timeout, a failure or an answer that is not allowed
        by what was offered all deny."""
        limit = asyncio.timeout(self._config.prompt_timeout.total_seconds())
        answer: object
        try:
            async with limit:
                answer = await self._prompts.ask(prompt)
        except TimeoutError:
            if limit.expired():
                return Denied(reason="prompt_timeout")
            _log.exception("the prompt interface failed")
            return Denied(reason="prompt_failed")
        except Exception:  # noqa: BLE001  (the prompt is code we do not own: its failure denies)
            _log.exception("the prompt interface failed")
            return Denied(reason="prompt_failed")
        if isinstance(answer, Allowed):
            if answer.scope not in prompt.scopes:
                return Denied(reason="scope_not_offered")
            return answer
        if isinstance(answer, Denied):
            return answer
        # A prompt written wrong can answer anything: only the two answers count.
        return Denied(reason="prompt_failed")  # type: ignore[unreachable]

    @staticmethod
    def _decision(
        request: RequestGrant,
        capability: CapabilityKind,
        path: str | None,
        outcome: Literal["covered", "granted", "denied"],
        *,
        grant: GrantId | None,
        scope: Scope | None,
        reason: str | None,
        expires_at: datetime | None = None,
    ) -> GrantDecision:
        return GrantDecision(
            at=utc_now(),
            task=request.task,
            run=request.run,
            capability=capability.value,
            path=None if path is None else cut(path, 4096),
            outcome=outcome,
            grant=grant,
            scope=scope,
            reason=None if reason is None else cut(reason),
            model_reason=cut(request.reason),
            expires_at=expires_at,
        )

    def _denied(
        self,
        request: RequestGrant,
        capability: CapabilityKind,
        path: str | None,
        why: str,
        message: str,
    ) -> ResponseBody:
        self._audit.append(
            self._decision(request, capability, path, "denied", grant=None, scope=None, reason=why)
        )
        return GrantOutcome(
            kind="grant_outcome", granted=False, grant=None, scope=None, message=message
        )

    # --- the task's own workspace --------------------------------------------------------------

    async def _read_workspace_file(self, owner: Owner, request: ReadWorkspaceFile) -> ResponseBody:
        live = self._owned(owner, request.task)
        data: bytes = await self._fs.call(
            partial(
                self._read_in_workspace,
                live.state.task,
                live.workspace,
                request.path,
                request.max_bytes,
            )
        )
        return FileContent(kind="file_content", size=len(data), data=_encode(data))

    def _read_in_workspace(
        self, task: TaskId, identity: FsIdentity, rel: WorkspaceRelPath, max_bytes: int
    ) -> bytes:
        with self._workspaces.open_workspace(task, identity) as root:
            return read_workspace_file(root, rel, max_bytes=ByteCount(max_bytes))

    async def _list_workspace(self, owner: Owner, request: ListWorkspace) -> ResponseBody:
        live = self._owned(owner, request.task)
        listing: Listing = await self._fs.call(
            partial(self._list_in_workspace, live.state.task, live.workspace, request.path)
        )
        return _listing(listing)

    def _list_in_workspace(
        self, task: TaskId, identity: FsIdentity, rel: WorkspaceRelPath | None
    ) -> Listing:
        with self._workspaces.open_workspace(task, identity) as root:
            if rel is None:
                return list_directory(root)
            with open_in_workspace(root, rel, want=FileKind.DIRECTORY) as directory:
                return list_directory(directory)

    # --- files under a grant -------------------------------------------------------------------

    async def _read_file(self, owner: Owner, request: ReadFile) -> ResponseBody:
        allow, stored, rules = await self._authorise_file(
            owner, request.task, request.run, request.path, "read_file"
        )
        data: bytes = await self._fs.call(
            partial(self._read_granted, stored, rules, allow, request.max_bytes)
        )
        return FileContent(kind="file_content", size=len(data), data=_encode(data))

    @staticmethod
    def _read_granted(stored: StoredPath, rules: DenyRules, allow: Allow, max_bytes: int) -> bytes:
        limit = ByteCount(max_bytes)
        with open_granted(stored, rules=rules) as root:
            if allow.within_grant is None:
                return read_capped(root, limit)
            return read_workspace_file(
                root, allow.within_grant, max_bytes=limit, expect=allow.target
            )

    async def _list_dir(self, owner: Owner, request: ListDir) -> ResponseBody:
        allow, stored, rules = await self._authorise_file(
            owner, request.task, request.run, request.path, "list_dir"
        )
        listing: Listing = await self._fs.call(partial(self._list_granted, stored, rules, allow))
        return _listing(listing)

    @staticmethod
    def _list_granted(stored: StoredPath, rules: DenyRules, allow: Allow) -> Listing:
        with open_granted(stored, rules=rules) as root:
            if allow.within_grant is None:
                return list_directory(root)
            with open_in_workspace(
                root, allow.within_grant, want=FileKind.DIRECTORY, expect=allow.target
            ) as directory:
                return list_directory(directory)

    async def _authorise_file(
        self,
        owner: Owner,
        task: TaskId,
        run: RunId,
        text: str,
        operation: Literal["read_file", "list_dir"],
    ) -> tuple[Allow, StoredPath, DenyRules]:
        """Decide a read or listing under a grant. On success, the consumption of a once grant,
        the task's taint and the audit entry are recorded, and the grant to open is returned."""
        self._owned(owner, task)
        resolved = await self._resolve(text)
        rules = await self._rules()
        live = self._owned(owner, task)  # the task may have ended while the file system answered
        context = RunContext(run=run, trigger=RunTrigger.MODEL, tool=None)
        asked = AccessRequest(
            task=task, run=context, capability=CapabilityKind.FS_READ, path=resolved
        )
        now = self._now()
        decision = decide(live.state, self._grants_for(live), asked, rules=rules, now=now)
        match decision:
            case Allow() as allow:
                stored = self._record_allowed(live, run, resolved, allow, operation, now)
                return allow, stored, rules
            case Deny(reason=refusal):
                self._audit.append(
                    self._file_entry(
                        task, run, operation, resolved, "refused", None, False, refusal.kind
                    )
                )
                raise _Refuse(_reason_of_refusal(refusal), describe(refusal))
            case NeedsPrompt():
                self._audit.append(
                    self._file_entry(
                        task, run, operation, resolved, "refused", None, False, "not_granted"
                    )
                )
                raise _Refuse("not_granted", "no grant covers that: request access first")
            case _:
                assert_never(decision)

    @staticmethod
    def _file_entry(
        task: TaskId,
        run: RunId,
        operation: Literal["read_file", "list_dir"],
        resolved: ResolvedPath,
        outcome: Literal["allowed", "refused"],
        grant: GrantId | None,
        consumed: bool,
        reason: str | None,
    ) -> FileAccess:
        return FileAccess(
            at=utc_now(),
            task=task,
            run=run,
            operation=operation,
            path=cut(resolved.text, 4096),
            outcome=outcome,
            grant=grant,
            consumed=consumed,
            reason=None if reason is None else cut(reason),
        )

    def _record_allowed(
        self,
        live: _Live,
        run: RunId,
        resolved: ResolvedPath,
        allow: Allow,
        operation: Literal["read_file", "list_dir"],
        now: datetime,
    ) -> StoredPath:
        """Persist what an `Allow` implies before anything is opened (S16): the consumption of a
        once grant, the task's taint and the audit entry. Returns the path to open."""
        grant = next((g for g in self._grants_for(live) if g.id == allow.grant), None)
        if grant is None or not isinstance(grant.capability, FsRead):
            raise _Refuse("not_granted", "no grant covers that: request access first")
        stored = grant.capability.path
        spent: OnceGrant | None = None
        if allow.consumes:
            if not isinstance(grant, OnceGrant):
                raise _Refuse("not_granted", "no grant covers that: request access first")
            spent = grant.consume(run)
        taint = TaskState(task=live.state.task, taint=allow.task_taint)
        with self._atomic() as entries:
            if spent is not None:
                self._store.replace_grant(spent)
            if allow.task_taint != live.state.taint:
                self._store.set_taint(live.state.task, allow.task_taint)
            entries.append(
                self._file_entry(
                    live.state.task,
                    run,
                    operation,
                    resolved,
                    "allowed",
                    grant.id,
                    spent is not None,
                    None,
                )
            )
        if spent is not None:
            live.grants[live.grants.index(grant)] = spent
        live.state = taint
        return stored

    # --- sandbox runs --------------------------------------------------------------------------

    async def _run_in_sandbox(self, owner: Owner, request: RunInSandbox) -> ResponseBody:
        live = self._owned(owner, request.task)
        flow = asyncio.ensure_future(self._run_flow(owner, request))
        live.running.add(flow)
        flow.add_done_callback(live.running.discard)
        try:
            return await flow
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise  # this request itself is being cancelled: the connection is closing
            return Refused(
                kind="refused", reason="unknown_task", message="the task ended during the run"
            )

    async def _run_flow(self, owner: Owner, request: RunInSandbox) -> ResponseBody:
        config = self._config
        variables, allowlist = sandbox_environment(
            [(pair.name, pair.value) for pair in request.env], path=config.sandbox_path
        )
        live = self._owned(owner, request.task)
        if request.run in live.runs:
            raise _Refuse("invalid_request", "that run ID was already used")
        if len(live.runs) >= config.max_runs_per_task:
            raise _Refuse("busy", "the task has used all the runs it may")
        if self._slots.locked():
            raise _Refuse("busy", "too many runs are in progress")
        async with self._slots:
            await self._require_limits(request)
            rules = await self._rules()
            live = self._owned(owner, request.task)
            context = RunContext(run=request.run, trigger=RunTrigger.MODEL, tool=None)
            selection = select_grants(
                live.state, context, self._grants_for(live), rules=rules, now=self._now()
            )
            mounts = disjoint_mounts(
                [
                    Mount(grant.id, grant.capability.path)
                    for grant in selection.applicable
                    if isinstance(grant.capability, FsRead)
                ]
            )
            paths: _RunPaths = await self._fs.call(
                partial(
                    self._open_run_paths, live.state.task, live.workspace, tuple(mounts), rules
                ),
                discard=_close_paths,
            )
            try:
                policy, limits = self._policy(request, paths, mounts, variables, allowlist)
                self._authorise_run(request, live, context, selection, mounts, rules)
            except BaseException:
                paths.close()
                raise
            return await self._run_authorised(request, policy, limits, paths)

    async def _require_limits(self, request: RunInSandbox) -> None:
        """Refuse a run on a host that cannot enforce the limits the target requires (#74)."""
        if not self._config.require_scope_limits:
            return
        try:
            async with asyncio.timeout(_PROBE_WAIT.total_seconds()):
                plan = await self._sandbox.limits_plan()
        except TimeoutError:
            self._audit_refused_run(request, "limits_probe_timeout")
            raise _Refuse("timeout", "the host's limits could not be checked in time") from None
        missing = [
            kind for kind in plan.unenforced if kind in (LimitKind.PIDS, LimitKind.MEMORY_TOTAL)
        ]
        if missing:
            self._audit_refused_run(request, "limits_unenforced")
            raise _Refuse(
                "unavailable",
                "this host cannot enforce the sandbox's process and memory limits",
            )

    def _audit_refused_run(self, request: RunInSandbox, reason: str) -> None:
        self._audit.append(
            SandboxRun(
                at=self._now(),
                task=request.task,
                run=request.run,
                outcome="refused",
                reason=cut(reason),
                program=cut(request.command[0]),
                arguments=len(request.command) - 1,
                env_names=tuple(pair.name for pair in request.env),
                mounted=(),
                consumed=(),
                wall_seconds=request.wall_seconds,
                require_landlock=self._config.require_landlock,
            )
        )

    def _open_run_paths(
        self,
        task: TaskId,
        identity: FsIdentity,
        mounts: tuple[Mount, ...],
        rules: DenyRules,
    ) -> _RunPaths:
        """Open the workspace and every granted path by identity, with no symlink followed.
        Any failure closes what was opened and refuses the run (the user is asked again)."""
        opened: list[OpenedPath] = []
        try:
            workspace = self._workspaces.open_workspace(task, identity)
            opened.append(workspace)
            granted: list[OpenedPath] = []
            for mount in mounts:
                granted.append(open_granted(mount.path, rules=rules))
                opened.append(granted[-1])
        except BaseException:
            for each in opened:
                each.close()
            raise
        return _RunPaths(workspace=workspace, mounts=tuple(granted))

    def _policy(
        self,
        request: RunInSandbox,
        paths: _RunPaths,
        mounts: Sequence[Mount],
        variables: tuple[EnvVar, ...],
        allowlist: frozenset[EnvName],
    ) -> tuple[SandboxPolicy, SandboxLimits]:
        limits = dataclasses.replace(
            self._config.limits,
            wall_time=timedelta(seconds=request.wall_seconds),
            cpu_time=timedelta(seconds=request.cpu_seconds),
            max_output_bytes=ByteCount(request.max_output_bytes),
        )
        try:
            policy = SandboxPolicy(
                command=request.command,
                workspace=paths.workspace,
                runtime=self._config.runtime,
                limits=limits,
                grants=tuple(
                    GrantMount(
                        source=opened,
                        target=MountPath(mount.path.path),
                        access=GrantAccess.READ,
                    )
                    for mount, opened in zip(mounts, paths.mounts, strict=True)
                ),
                env=variables,
                env_allowlist=allowlist,
                require_landlock=self._config.require_landlock,
            )
            policy.check_descriptors()
        except InvalidValueError:  # InvalidPolicyError is one
            self._audit_refused_run(request, "policy_refused")
            raise _Refuse("invalid_request", "the sandbox policy was refused") from None
        return policy, limits

    def _authorise_run(
        self,
        request: RunInSandbox,
        live: _Live,
        context: RunContext,
        selection: GrantSelection,
        mounts: Sequence[Mount],
        rules: DenyRules,
    ) -> None:
        """Record that the run is authorised, before it starts: the once grants it uses are
        consumed, the task's taint is raised to what the applied grants imply, and the audit
        entry is written, in one transaction (S16: persist before acting)."""
        if self._live.get(live.state.task) is not live:
            raise _Refuse("unknown_task", "the task ended")
        now = self._now()
        again = select_grants(live.state, context, self._grants_for(live), rules=rules, now=now)
        if [g.id for g in again.applicable] != [g.id for g in selection.applicable] or (
            again.task_taint != selection.task_taint
        ):
            raise _Refuse("busy", "the grants changed while the run was being set up: try again")
        once = [grant for grant in selection.applicable if isinstance(grant, OnceGrant)]
        spent = [grant.consume(request.run) for grant in once]
        with self._atomic() as entries:
            for grant in spent:
                self._store.replace_grant(grant)
            if selection.task_taint != live.state.taint:
                self._store.set_taint(live.state.task, selection.task_taint)
            entries.append(
                SandboxRun(
                    at=now,
                    task=live.state.task,
                    run=request.run,
                    outcome="authorised",
                    reason=None,
                    program=cut(request.command[0]),
                    arguments=len(request.command) - 1,
                    env_names=tuple(pair.name for pair in request.env),
                    mounted=tuple(mount.grant for mount in mounts),
                    consumed=tuple(grant.id for grant in spent),
                    wall_seconds=request.wall_seconds,
                    require_landlock=self._config.require_landlock,
                )
            )
        live.runs.add(request.run)
        for old, new in zip(once, spent, strict=True):
            live.grants[live.grants.index(old)] = new
        live.state = TaskState(task=live.state.task, taint=selection.task_taint)

    async def _run_authorised(
        self,
        request: RunInSandbox,
        policy: SandboxPolicy,
        limits: SandboxLimits,
        paths: _RunPaths,
    ) -> ResponseBody:
        outcome: Literal["completed", "failed", "cancelled"] = "cancelled"
        reason: str | None = None
        result: SandboxResult | None = None
        deadline = limits.wall_time + self._config.run_grace
        try:
            async with asyncio.timeout(deadline.total_seconds()):
                result = await self._sandbox.run(policy)
            outcome = "completed"
        except TimeoutError:
            outcome, reason = "failed", "timeout"
            raise _Refuse("timeout", "the sandbox did not finish in time") from None
        except (SandboxUnavailable, SandboxUnsupported):
            outcome, reason = "failed", "unavailable"
            raise _Refuse("unavailable", "this host cannot run the sandbox safely") from None
        except SandboxLaunchError:
            outcome, reason = "failed", "launch_failed"
            raise _Refuse("failed", "the sandbox could not be set up") from None
        except SandboxTeardownError:
            outcome, reason = "failed", "teardown_failed"
            _log.error("a sandbox could not be stopped")
            raise _Refuse("failed", "the sandbox could not be stopped") from None
        except InvalidPolicyError:
            outcome, reason = "failed", "policy_refused"
            raise _Refuse("invalid_request", "the sandbox policy was refused") from None
        finally:
            paths.close()
            self._audit_result(request, outcome, reason, result)
        return RunResult(
            kind="run_result",
            termination=_termination(result.termination),
            exit_code=result.exit_code,
            stdout=_encode(result.stdout),
            stderr=_encode(result.stderr),
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            wall_milliseconds=int(result.wall_time / timedelta(milliseconds=1)),
            unenforced=tuple(_limit(kind) for kind in result.limits.unenforced),
            landlock=_landlock(result.hardening.landlock),
            mounted_grants=len(policy.grants),
        )

    def _audit_result(
        self,
        request: RunInSandbox,
        outcome: Literal["completed", "failed", "cancelled"],
        reason: str | None,
        result: SandboxResult | None,
    ) -> None:
        """The second entry of a run. Its failure is logged: the run has happened either way."""
        try:
            self._audit.append(
                SandboxRunResult(
                    at=self._now(),
                    task=request.task,
                    run=request.run,
                    outcome=outcome,
                    reason=reason,
                    termination=None if result is None else _termination(result.termination),
                    exit_code=None if result is None else result.exit_code,
                    wall_milliseconds=None
                    if result is None
                    else int(result.wall_time / timedelta(milliseconds=1)),
                    unenforced=()
                    if result is None
                    else tuple(_limit(kind) for kind in result.limits.unenforced),
                    landlock=None if result is None else _landlock(result.hardening.landlock),
                )
            )
        except AuditError:
            _log.error("the result of a sandbox run could not be audited")


def _listing(listing: Listing) -> DirListing:
    return DirListing(
        kind="dir_listing",
        entries=tuple(
            DirEntry(name=str(entry.name), entry=entry.kind, size=entry.size)
            for entry in listing.entries
        ),
        truncated=listing.truncated,
        skipped=listing.skipped,
    )


def _pid() -> int:
    return os.getpid()
