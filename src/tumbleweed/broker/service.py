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

**The tool store (S23).** `list_tools`, `show_tool`, `export_tool` and `import_tool` are answered
here from `ToolStore`; none of them belongs to a task. Each is one synchronous block, so a version
that is opened is the one that is audited and returned, and a store that changes meanwhile cannot
make the two differ. An export is audited before the bundle is returned and an import before it
commits (inside `_atomic`, like every act that changes state); listing, showing and searching are
reads of the broker's own store and are not decisions. `save_tool` is the same block for a tool
saved from a task, whose taint is the broker's own record of it (I12); it has no IPC request until
S24 gives the model a `save_tool`.

**Refusals never carry input.** A `Refused` message is fixed text: paths, commands and the
model's reason are never repeated in a response, a log line or an exception message, and no
`ValidationError` text ever leaves this module.
"""

import asyncio
import base64
import contextlib
import dataclasses
import itertools
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Final, Literal, Protocol, assert_never, final

from tumbleweed.broker.audit import (
    MAX_AUDITED_IDS,
    MAX_ROLLED_BACK_KINDS,
    AuditEntry,
    AuditLog,
    DaemonStart,
    FileAccess,
    GrantDecision,
    GrantRevoked,
    RolledBack,
    SandboxRun,
    SandboxRunResult,
    TaskEndReason,
    ToolImportRefused,
    ToolSaved,
    WorkspaceDeleted,
    cut,
)
from tumbleweed.broker.audit import TaskEnded as TaskEndedEntry
from tumbleweed.broker.audit import TaskStarted as TaskStartedEntry
from tumbleweed.broker.audit import ToolExported as ToolExportedEntry
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
    ToolBlobError,
    ToolBlobWriteError,
    ToolConflictError,
    ToolNotFoundError,
    ToolStoreFullError,
    ToolTaintConflictError,
    ToolVersionExhaustedError,
    UnknownTaskError,
    WorkspaceError,
    describe_fault,
)
from tumbleweed.broker.fdio import FileKind, OpenedPath, read_capped
from tumbleweed.broker.fsworker import FsWorker
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
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.owner import Owner
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
from tumbleweed.broker.store import BrokerStore, TaskRecord, ToolRecord
from tumbleweed.broker.task import TaintKind, TaskState
from tumbleweed.broker.toolstore import StoredTool, ToolStore, taint_names
from tumbleweed.broker.workspace import open_in_workspace, read_workspace_file
from tumbleweed.broker.workspaces import Listing, Workspaces, list_directory
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import GrantId, RunId, TaskId, ToolId
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
    PickAccess,
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
from tumbleweed.ipc.prompt_messages import (
    AllowDecision,
    AnswerPrompt,
    ClosePrompt,
    DenyDecision,
    PromptAnswered,
    ShowPrompt,
)
from tumbleweed.ipc.tool_messages import (
    ExportTool,
    ImportTool,
    ListTools,
    ShowTool,
    ToolBundle,
    ToolImported,
    ToolList,
    ToolShown,
    ToolSummary,
)
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
from tumbleweed.tools import (
    ToolDraft,
    ToolPackageError,
    ToolTooLargeError,
    export_bundle,
    parse_bundle,
)

_log = logging.getLogger(__name__)

MAX_GRANTS_PER_TASK: Final = 64
_MAX_ENDED_ALLOWANCES: Final = 1024
_DENIAL_MESSAGES: Final = {
    "rate_limited": "too many permission requests for this task; wait a while before asking again",
    "rate_limited_connection": "too many permission requests in all; wait a while before asking",
    "prompt_timeout": "the user did not answer in time",
    "no_prompt_ui": "there is nobody to ask here",
    "prompt_busy": "the user could not be asked just now: too many questions are waiting",
    "prompt_failed": "the user could not be asked: the prompt did not work",
    "scope_not_offered": "the answer was not one the prompt offered, so nothing was granted",
    "user_denied": "the user did not allow it",
}
_DENIED: Final = "the request was denied"
_PROBE_WAIT: Final = timedelta(seconds=15)
_SWEEP_BATCH: Final = 100
_UNAVAILABLE_AUDIT: Final = "the audit log is unavailable, so nothing is done"


class Sandbox(Protocol):
    """What the service needs of a sandbox runner (`tumbleweed.sandbox.runner.SandboxRunner`)."""

    async def run(self, policy: SandboxPolicy) -> SandboxResult: ...

    async def limits_plan(self) -> LimitsPlan: ...

    async def aclose(self) -> None: ...


def _denial_message(reason: str) -> str:
    """What the model is told when a request is denied: fixed text for the reasons that are ours
    (never the user's words, never a path)."""
    return _DENIAL_MESSAGES.get(reason, _DENIED)


class _Refuse(Exception):  # noqa: N818  (it is an answer, not a failure)
    """Internal: stop the request and answer `Refused(reason, message)`. Never leaves the module."""

    def __init__(self, reason: RefusalReason, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.code = code or reason  # what the audit log says: more exact than the reason


@final
class _Budget:
    """How many prompts may be asked in a window, on a monotonic clock (a wall clock that jumps
    would free or hold a task by the size of the jump). A prompt is counted when it is asked."""

    def __init__(self, limit: int, window: timedelta) -> None:
        self._limit = limit
        self._window = window.total_seconds()
        self._asked: deque[float] = deque()

    def exhausted(self, now: float) -> bool:
        while self._asked and now - self._asked[0] >= self._window:
            self._asked.popleft()
        return len(self._asked) >= self._limit

    def count(self, now: float) -> None:
        self._asked.append(now)


@dataclass(slots=True)
class _Live:
    """A live task: its policy state, who owns it and what it holds."""

    owner: Owner
    state: TaskState
    workspace: FsIdentity
    # What the task's lineage may still ask: shared with the task that replaces it (a model that
    # starts a new task does not get a new allowance).
    prompts: _Budget
    grants: list[Grant] = field(default_factory=list)
    runs: set[RunId] = field(default_factory=set)
    pending_runs: set[RunId] = field(default_factory=set)  # reserved, not yet authorised
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


def _ids(ids: Iterable[GrantId]) -> tuple[GrantId, ...]:
    """The first `MAX_AUDITED_IDS` of some grant IDs, which is all an audit entry holds (the entry
    says how many there were where that matters): an entry can never be too long to build."""
    return tuple(itertools.islice(ids, MAX_AUDITED_IDS))


def _retry_after(failures: int) -> timedelta:
    """How long to leave a workspace that could not be deleted: an hour, doubling, up to a day."""
    return min(timedelta(hours=1) * (1 << max(0, min(failures, 8))), timedelta(days=1))


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


def _why(error: Exception) -> str:
    """A short fixed code for why a request was refused, for the audit log."""
    if isinstance(error, _Refuse):
        return error.code
    if isinstance(error, PathError):
        return error.fault.value
    if isinstance(error, FsTimeoutError):
        return "timeout"
    return "invalid_request"


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
        tools: ToolStore,
        audit_log: AuditLog,
        workspaces: Workspaces,
        fs: FsWorker,
        sandbox: Sandbox,
        prompts: PromptInterface,
        environ: Mapping[str, str],
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._monotonic = monotonic
        self._owner_prompts: dict[Owner, _Budget] = {}  # one connection's ceiling over its tasks
        # The allowances of tasks that ended, for `prompt_window`, so that ending a task and then
        # starting one with `previous` does not start over: task -> (owner, allowance, ended).
        self._ended_prompts: dict[TaskId, tuple[Owner, _Budget, float]] = {}
        self._store = store
        self._tools = tools
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
        self._starting = 0  # tasks being made: counted against the cap before the first await
        self._creating: set[TaskId] = set()  # their workspaces, which are not orphans

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
            try:
                self._end_stale(record, now)
            except Exception:  # noqa: BLE001  (one task that cannot be ended must not stop the start)
                # Not live in memory, so unusable whatever the database says; tried again at the
                # next start.
                _log.exception("a task left live by a stopped broker could not be ended on record")
        # Orphans and the first sweep can take as long as deleting takes: not before serving.
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
        self._tools.close()
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
            prompts=self._new_budget(self._config.prompts_per_window),
        )
        with self._atomic() as entries:
            self._end_stored(live, TaskEndReason.STARTUP, now, entries)

    # --- retention -----------------------------------------------------------------------------

    async def sweep(self) -> int:
        """Delete the workspaces of tasks that ended more than the retention period ago. Returns
        how many were deleted. A workspace that cannot be deleted is audited and tried again later,
        after a pause that doubles with every failure (an hour, up to a day), so that a few
        undeletable ones cannot hold up the rest."""
        now = self._now()
        cutoff = now - self._config.retention
        deleted = 0
        for record in self._store.workspaces_to_delete(cutoff, now)[:_SWEEP_BATCH]:
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
                else:
                    self._store.delete_failed(record.task, now + _retry_after(record.failures))
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
            # Judged now, not when the names were listed: a task being made has its workspace
            # before it has a record, and that is not an orphan.
            if task in known or task in self._creating or self._store.task(task) is not None:
                continue
            with contextlib.suppress(WorkspaceError, FsTimeoutError):
                await self._fs.call(
                    partial(self._workspaces.delete, task), deadline=self._config.delete_timeout
                )

    async def _sweep_forever(self) -> None:
        """Remove orphans and sweep at once (in the background, so that `start` is not held up by
        deleting), then every `sweep_interval`."""
        first = True
        while True:
            try:
                if first:
                    await self._remove_orphans()
                await self.sweep()
            except Exception:  # noqa: BLE001  (the sweep outlives anything: it is tried again)
                _log.exception("the retention sweep failed; it is tried again at the next one")
            first = False
            await asyncio.sleep(self._config.sweep_interval.total_seconds())

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
        except ToolNotFoundError:
            return Refused(kind="refused", reason="not_found", message="no such tool or version")
        except ToolConflictError:
            return Refused(
                kind="refused", reason="conflict", message="a tool with that ID is already stored"
            )
        except ToolStoreFullError:
            return Refused(kind="refused", reason="busy", message="the tool store is full")
        except ToolTaintConflictError:
            return Refused(
                kind="refused",
                reason="denied",
                message="the task's taint cannot be combined with the tool's",
            )
        except ToolVersionExhaustedError:
            return Refused(
                kind="refused", reason="too_large", message="the tool has no version number left"
            )
        except ToolBlobWriteError:
            return Refused(
                kind="refused", reason="unavailable", message="the tool store cannot be written"
            )
        except ToolBlobError:
            return Refused(
                kind="refused",
                reason="unsafe",
                message="the stored tool failed its integrity check and is not used",
            )
        except ToolPackageError:
            return Refused(
                kind="refused",
                reason="invalid_request",
                message="the tool package is not acceptable",
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
            case ListTools():
                return self._list_tools(request)
            case ShowTool():
                return self._show_tool(request)
            case ExportTool():
                return self._export_tool(request)
            case ImportTool():
                return self._import_tool(request)
            case AnswerPrompt():
                return self._answer_prompt(owner, request)
            case ShowPrompt() | ClosePrompt() | Ping() | Echo():
                raise RequestRefused(ErrorCode.UNSUPPORTED_REQUEST, "not a broker request")
            case _:
                assert_never(request)

    def connection_closed(self, owner: Owner) -> None:
        """End every task of a connection that has gone (revoking its grants, stopping its runs)."""
        for live in [live for live in self._live.values() if live.owner == owner]:
            try:
                with self._atomic() as entries:
                    self._end_stored(live, TaskEndReason.CONNECTION_CLOSED, self._now(), entries)
            except Exception:  # noqa: BLE001  (whatever it is, the task must still end in memory)
                # The connection is gone whatever happens: the task must not stay usable and its
                # runs must stop. The database still says live, and the next start ends it.
                _log.exception("a task of a closed connection could not be ended on record")
            finally:
                self._apply_ended(live)
        # The connection's allowances go with it (a reconnect starts a new one: #146's follow-up).
        self._owner_prompts.pop(owner, None)
        self._ended_prompts = {t: k for t, k in self._ended_prompts.items() if k[0] != owner}

    # --- the tool store ------------------------------------------------------------------------

    def _list_tools(self, request: ListTools) -> ResponseBody:
        records = self._tools.find(request.query, request.limit)
        return ToolList(kind="tool_list", tools=tuple(_tool_summary(record) for record in records))

    def _show_tool(self, request: ShowTool) -> ResponseBody:
        opened = self._tools.open(request.tool, request.version)
        return ToolShown(
            kind="tool_shown",
            summary=_tool_summary(opened.record),
            document=opened.canonical.data.decode("utf-8"),
        )

    def _export_tool(self, request: ExportTool) -> ResponseBody:
        """Hand out the bundle of a verified version, once the export is on record: the entry is
        written first, so a bundle that left the broker always has one."""
        opened = self._tools.open(request.tool, request.version)
        bundle = export_bundle(opened.canonical)
        record = opened.record
        with self._atomic() as entries:
            entries.append(
                ToolExportedEntry(
                    at=self._now(),
                    tool=record.tool,
                    version=record.version,
                    hash=record.hash,
                    taint=taint_names(record.taint),
                )
            )
        return ToolBundle(
            kind="tool_bundle", summary=_tool_summary(record), bundle=bundle.decode("utf-8")
        )

    def _import_tool(self, request: ImportTool) -> ResponseBody:
        """Add the tool in a bundle (hostile input) to the store. A bundle that is refused is
        recorded with a closed reason and nothing of its content; one the store already holds,
        byte for byte, changes nothing and is not recorded."""
        try:
            canonical = parse_bundle(request.bundle.encode("utf-8"))
        except UnicodeEncodeError:  # a lone surrogate: not text, so not a bundle
            self._audit_import_refused("invalid")
            raise _Refuse("invalid_request", "the bundle is not acceptable") from None
        except ToolTooLargeError:
            self._audit_import_refused("too_large")
            raise _Refuse("too_large", "the bundle is too large") from None
        except ToolPackageError:
            self._audit_import_refused("invalid")
            raise _Refuse("invalid_request", "the bundle is not acceptable") from None
        now = self._now()
        try:
            with self._atomic() as entries:
                stored = self._tools.import_(canonical, now)
                if stored.created:
                    entries.append(_saved_entry(stored, now, task=None))
        except ToolConflictError:
            self._audit_import_refused("conflict")
            raise
        except ToolStoreFullError:
            self._audit_import_refused("full")
            raise
        except ToolBlobError:
            self._audit_import_refused("integrity", best_effort=True)
            raise
        except (ToolBlobWriteError, StoreError):
            self._audit_import_refused("error", best_effort=True)
            raise
        return ToolImported(
            kind="tool_imported", summary=_tool_summary(stored.record), created=stored.created
        )

    def _audit_import_refused(
        self,
        reason: Literal["invalid", "too_large", "conflict", "full", "integrity", "error"],
        *,
        best_effort: bool = False,
    ) -> None:
        """Record a refused import. With `best_effort` (a failure of the store or the disk, which
        may be why the log cannot be written either) a failure to record is logged, and the
        refusal the caller is about to raise is what the request gets."""
        try:
            with self._atomic() as entries:
                entries.append(ToolImportRefused(at=self._now(), reason=reason))
        except (AuditError, StoreError):
            if not best_effort:
                raise
            _log.error("a refused tool import could not be recorded")

    def save_tool(
        self, owner: Owner, task: TaskId, draft: ToolDraft, *, tool: ToolId | None = None
    ) -> StoredTool:
        """Store `draft` as a new tool, or as the next version of `tool`, saved from `task`.

        The package records the task's taint as the broker has it, read in the same block that
        stores the version (I12), so a grant that arrived a moment ago is in it. Raises
        `UnknownTaskError` if `task` is not a live task of `owner`, `ToolNotFoundError`,
        `ToolStoreFullError`, `ToolPackageError`, `AuditError` and `StoreError` as the store and
        the audit log do. There is no IPC request for it yet: S24's `save_tool` is the caller.
        """
        try:
            live = self._owned(owner, task)
        except _Refuse:
            raise UnknownTaskError("no such task") from None
        now = self._now()
        with self._atomic() as entries:
            stored = self._tools.save(draft, tool=tool, taint=live.state.taint, at=now)
            entries.append(_saved_entry(stored, now, task=task))
        return stored

    # --- atomic sections and the end of a task -------------------------------------------------

    @contextmanager
    def _atomic(self) -> Iterator[list[AuditEntry]]:
        """A database transaction whose audit entries are written just before it commits.

        Append the entries to the list the block is given. If one cannot be written, the
        transaction is rolled back and `AuditError` propagates: the act did not happen. If the
        commit itself fails after the entries were written, they would say something that did not
        happen: a `rolled_back` entry follows them, naming their kinds (best effort: if it cannot
        be written either, that is logged).
        """
        entries: list[AuditEntry] = []
        written: list[str] = []
        try:
            with self._store.transaction():
                yield entries
                for entry in entries:
                    self._audit.append(entry)
                    written.append(entry.kind)
        except BaseException:
            if written:
                try:
                    self._audit.append(
                        RolledBack(
                            at=self._now(),
                            entries=tuple(written[:MAX_ROLLED_BACK_KINDS]),
                            count=len(written),
                        )
                    )
                except (AuditError, ValueError):  # a ValidationError is one
                    _log.error("entries were written for an act that was rolled back")
            raise

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
        entries.append(
            TaskEndedEntry(
                at=now,
                task=task,
                reason=reason,
                revoked=_ids(revoked),
                revoked_count=len(revoked),
            )
        )
        return revoked

    def _apply_ended(self, live: _Live) -> None:
        """Make an ended task unusable in memory and stop its runs."""
        self._live.pop(live.state.task, None)
        self._remember_allowance(live)
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

    def _previous_state(
        self, owner: Owner, previous: TaskId | None
    ) -> tuple[_Live | None, TaskState | None]:
        """The task that `start_task` replaces, as it is *now*: live and the owner's (its state is
        read here, so what a request on it did a moment ago is in it), or an ended one's record.
        `unknown_task` for any other. Synchronous: callers read it where they record."""
        if previous is None:
            return None, None
        live = self._live.get(previous)
        if live is not None:
            if live.owner != owner:
                raise _Refuse("unknown_task", "no such task")
            return live, live.state
        record = self._store.task(previous)
        if record is None:
            raise _Refuse("unknown_task", "no such task")
        return None, TaskState(task=record.task, taint=record.taint, ended=True)

    async def _start_task(self, owner: Owner, request: StartTask) -> ResponseBody:
        replaced, _ = self._previous_state(owner, request.previous)
        if len(self._live) + self._starting - (replaced is not None) >= self._config.max_live_tasks:
            raise _Refuse("busy", "too many tasks are live")
        task = TaskId.new()
        # Counted against the cap before the first await, so that tasks started together cannot
        # all pass the check; the workspace is not an orphan while it is being made.
        self._starting += 1
        self._creating.add(task)
        try:
            # If this request is cancelled, or gives up, while the directory is being made, the
            # directory is removed when the call finally returns.
            def remove(_identity: FsIdentity) -> None:
                with contextlib.suppress(WorkspaceError):
                    self._workspaces.delete(task)

            identity: FsIdentity = await self._fs.call(
                partial(self._workspaces.create, task), discard=remove
            )
            try:
                return self._record_new_task(owner, request, task, identity)
            except BaseException:
                await self._discard_workspace(task)
                raise
        finally:
            self._starting -= 1
            self._creating.discard(task)

    def _record_new_task(
        self, owner: Owner, request: StartTask, task: TaskId, identity: FsIdentity
    ) -> ResponseBody:
        """Record a task whose workspace exists. There is no `await` from reading the state of
        the task it replaces to recording the new one, so the taint inherited is the taint at the
        moment of recording: a grant that completed while the workspace was being made counts
        (I12)."""
        replaced, previous = self._previous_state(owner, request.previous)
        if len(self._live) - (replaced is not None) >= self._config.max_live_tasks:
            raise _Refuse("busy", "too many tasks are live")
        state = (
            TaskState(task=task)
            if previous is None
            else previous.successor(task, fresh_context=request.fresh_context)
        )
        now = self._now()
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
        if replaced is not None:
            self._apply_ended(replaced)
        budget = (
            replaced.prompts
            if replaced is not None
            else self._ended_allowance(owner, request.previous)
            or self._new_budget(self._config.prompts_per_window)
        )
        self._live[task] = _Live(owner=owner, state=state, workspace=identity, prompts=budget)
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

    def _answer_prompt(self, owner: Owner, request: AnswerPrompt) -> ResponseBody:
        """Hand the user's answer to the prompt interface, which takes it only from the connection
        the prompt was sent to, and only once (I5): any other is `unknown_prompt`."""
        decision = request.decision
        answer: PromptAnswer
        match decision:
            case AllowDecision():
                answer = Allowed(scope=GrantScope(decision.scope), picked=decision.picked)
            case DenyDecision():
                answer = Denied(reason=decision.reason)
            case _:
                assert_never(decision)
        if not self._prompts.answer(owner, request.prompt, answer):
            raise _Refuse("unknown_prompt", "no such prompt")
        return PromptAnswered(kind="prompt_answered")

    def _remember_allowance(self, live: _Live) -> None:
        """Keep an ended task's allowance for `prompt_window` (at most `_MAX_ENDED_ALLOWANCES`)."""
        now = self._monotonic()
        window = self._config.prompt_window.total_seconds()
        self._ended_prompts = {
            task: kept for task, kept in self._ended_prompts.items() if now - kept[2] < window
        }
        while len(self._ended_prompts) >= _MAX_ENDED_ALLOWANCES:
            del self._ended_prompts[next(iter(self._ended_prompts))]  # the oldest
        self._ended_prompts[live.state.task] = (live.owner, live.prompts, now)

    def _ended_allowance(self, owner: Owner, previous: TaskId | None) -> _Budget | None:
        """The allowance of `previous`, an ended task of `owner`'s that ended within the window."""
        kept = None if previous is None else self._ended_prompts.get(previous)
        if kept is None or kept[0] != owner:
            return None
        if self._monotonic() - kept[2] >= self._config.prompt_window.total_seconds():
            return None
        return kept[1]

    def _new_budget(self, limit: int) -> _Budget:
        return _Budget(limit, self._config.prompt_window)

    def _rate_limited(self, live: _Live) -> str | None:
        """Why the task may not be asked now, or None (I5: 5 in 10 minutes, by default): the rest
        are denied without asking, and the model is told which limit it hit. A task that asks for
        permission over and over is the user's attention under attack, or a loop. The allowance is
        the lineage's (a task that replaces another, or one that replaces an ended one within the
        window, has what is left of it) and the connection has a ceiling over all its tasks, so that
        tasks started from nothing do not multiply it. Does not count this one."""
        now = self._monotonic()
        if live.prompts.exhausted(now):
            return "rate_limited"
        if self._owner_budget(live.owner).exhausted(now):
            return "rate_limited_connection"
        return None

    def _count_prompt(self, live: _Live) -> None:
        now = self._monotonic()
        live.prompts.count(now)
        self._owner_budget(live.owner).count(now)

    def _owner_budget(self, owner: Owner) -> _Budget:
        budget = self._owner_prompts.get(owner)
        if budget is None:
            budget = self._owner_prompts[owner] = self._new_budget(
                self._config.owner_prompts_per_window
            )
        return budget

    async def _request_grant(self, owner: Owner, request: RequestGrant) -> ResponseBody:
        self._owned(owner, request.task)
        access = request.access
        if isinstance(access, PickAccess):
            return await self._request_pick(owner, request, access)
        capability, resolved = await self._access_target(access)
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
        if self._held(live) >= MAX_GRANTS_PER_TASK:  # before asking: nobody is asked in vain
            self._audit_denied(request, capability, path_text, "grant_cap")
            raise _Refuse("busy", "the task holds as many grants as it may")
        if (limit := self._rate_limited(live)) is not None:
            return self._denied(request, capability, path_text, limit, _denial_message(limit))
        self._count_prompt(live)
        answer = await self._ask(prompt, owner)
        if isinstance(answer, Denied):
            return self._denied(
                request, capability, path_text, answer.reason, _denial_message(answer.reason)
            )
        try:
            return await self._issue_after_prompt(
                owner, request, asked, answer, path_text, GrantOrigin.PROMPT
            )
        except (_Refuse, PathError, FsTimeoutError, InvalidValueError) as error:
            # Whoever asked is told why, and the log says that the user's yes was not acted on.
            self._audit_denied(request, capability, path_text, _why(error))
            raise

    async def _request_pick(
        self, owner: Owner, request: RequestGrant, access: PickAccess
    ) -> ResponseBody:
        """The trusted picker: the user chooses a file or folder and the choice is the grant (S18).

        Nothing about the path can be decided before the user chooses, so before the prompt there
        are only the checks that do not need it (a net-tainted task can be given no file, the grant
        cap, the rate limit); the choice is then resolved and goes through the same issuance as any
        other grant (the deny rules, the write gate, the taint rule, the cap).
        """
        capability = CapabilityKind.FS_READ
        live = self._owned(owner, request.task)
        if TaintKind.NET in live.state.taint:  # whatever is picked is `fs`: do not ask in vain
            conflict = TaintConflict(held=TaintKind.NET, wanted=TaintKind.FS)
            return self._denied(request, capability, None, conflict.kind, describe(conflict))
        if self._held(live) >= MAX_GRANTS_PER_TASK:
            self._audit_denied(request, capability, None, "grant_cap")
            raise _Refuse("busy", "the task holds as many grants as it may")
        if (limit := self._rate_limited(live)) is not None:
            return self._denied(request, capability, None, limit, _denial_message(limit))
        self._count_prompt(live)
        prompt = GrantPrompt(
            task=request.task,
            run=request.run,
            pick=access.choose,
            scopes=frozenset({GrantScope.ONCE, GrantScope.TASK}),
            reason=request.reason,
        )
        answer = await self._ask(prompt, owner)
        if isinstance(answer, Denied):
            return self._denied(
                request, capability, None, answer.reason, _denial_message(answer.reason)
            )
        if answer.picked is None:  # a picker prompt is answered with a choice, or denied
            return self._denied(
                request, capability, None, "prompt_failed", _denial_message("prompt_failed")
            )
        picked_text: str | None = None  # what the audit says the user picked, once it is resolved
        try:
            resolved = await self._resolve(answer.picked)
            picked_text = resolved.text
            context = RunContext(run=request.run, trigger=RunTrigger.MODEL, tool=None)
            asked = AccessRequest(
                task=request.task, run=context, capability=capability, path=resolved
            )
            return await self._issue_after_prompt(
                owner, request, asked, answer, resolved.text, GrantOrigin.PICKER
            )
        except (_Refuse, PathError, FsTimeoutError, InvalidValueError) as error:
            self._audit_denied(request, capability, picked_text, _why(error))
            raise

    async def _issue_after_prompt(
        self,
        owner: Owner,
        request: RequestGrant,
        asked: AccessRequest,
        answer: Allowed,
        path_text: str | None,
        origin: GrantOrigin,
    ) -> ResponseBody:
        capability = asked.capability
        rules = await self._rules()  # rebuilt at issuance (S16): the list may have grown
        live = self._owned(owner, request.task)
        if self._held(live) >= MAX_GRANTS_PER_TASK:
            raise _Refuse("busy", "the task holds as many grants as it may")
        issuance = issue_grant(
            live.state,
            asked,
            scope=answer.scope,
            origin=origin,
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
        self, access: ReadAccess | WriteAccess | NetworkAccess
    ) -> tuple[CapabilityKind, ResolvedPath | None]:
        match access:
            case ReadAccess(path=text):
                return CapabilityKind.FS_READ, await self._resolve(text)
            case WriteAccess(path=text):
                return CapabilityKind.FS_WRITE, await self._resolve(text)
            case NetworkAccess():
                return CapabilityKind.NET_FULL, None
            case _ as unreachable:
                assert_never(unreachable)

    async def _ask(self, prompt: GrantPrompt, owner: Owner) -> PromptAnswer:
        """The user's answer, or a denial: a timeout, a failure or an answer that is not allowed
        by what was offered all deny."""
        limit = asyncio.timeout(self._config.prompt_timeout.total_seconds())
        answer: object
        try:
            async with limit:
                answer = await self._prompts.ask(prompt, owner)
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
            if (answer.picked is None) != (prompt.pick is None):
                # A choice answers the picker and nothing else: the user did not choose a path for
                # a prompt that named one, and a picker is not answered without a choice.
                return Denied(reason="prompt_failed")
            return answer
        if isinstance(answer, Denied):
            return answer
        # A prompt written wrong can answer anything: only the two answers count.
        return Denied(reason="prompt_failed")  # type: ignore[unreachable]  # the declared type is closed; a prompt may not be

    def _decision(
        self,
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
            at=self._now(),
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

    def _audit_denied(
        self, request: RequestGrant, capability: CapabilityKind, path: str | None, why: str
    ) -> None:
        """Record a denial that is about to be answered with a refusal. The refusal stands if the
        entry cannot be written."""
        try:
            self._audit.append(
                self._decision(
                    request, capability, path, "denied", grant=None, scope=None, reason=why
                )
            )
        except AuditError:
            _log.error("a denied grant could not be audited")

    @staticmethod
    def _held(live: _Live) -> int:
        """The grants a task holds against its cap: not the once grants that are used up."""
        return len(live.grants)  # used-up once grants are dropped when they are used up

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
        allow, stored, rules, resolved = await self._authorise_file(
            owner, request.task, request.run, request.path, "read_file"
        )
        try:
            data: bytes = await self._fs.call(
                partial(self._read_granted, stored, rules, allow, request.max_bytes)
            )
        except (PathError, FsTimeoutError) as error:
            self._audit_file_refused(request.task, request.run, "read_file", resolved, allow, error)
            raise
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
        allow, stored, rules, resolved = await self._authorise_file(
            owner, request.task, request.run, request.path, "list_dir"
        )
        try:
            listing: Listing = await self._fs.call(
                partial(self._list_granted, stored, rules, allow)
            )
        except (PathError, FsTimeoutError) as error:
            self._audit_file_refused(request.task, request.run, "list_dir", resolved, allow, error)
            raise
        return _listing(listing)

    def _audit_file_refused(
        self,
        task: TaskId,
        run: RunId,
        operation: Literal["read_file", "list_dir"],
        resolved: ResolvedPath,
        allow: Allow,
        error: Exception,
    ) -> None:
        """The decision allowed it (and said so) but opening what was granted refused: a swapped
        file, a vanished one, a hung mount. The entry that follows says what became of it."""
        try:
            self._audit.append(
                self._file_entry(
                    task, run, operation, resolved, "refused", allow.grant, False, _why(error)
                )
            )
        except AuditError:
            _log.error("a refused read could not be audited")

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
    ) -> tuple[Allow, StoredPath, DenyRules, ResolvedPath]:
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
                return allow, stored, rules, resolved
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

    def _file_entry(
        self,
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
            at=self._now(),
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
                self._store.replace_grant(spent)  # the record says by which run it was used up
                self._store.revoke_grant(spent.id, now)  # and it is over: nothing is left to revoke
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
            live.grants.remove(grant)  # a used-up grant is not held: not counted, not revoked
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
        """A run, from the checks to the result. Once the task is known every refusal before the
        run is authorised is recorded as a `sandbox_run` entry with outcome `refused`: a swapped
        grant that is refused is exactly what the log is for."""
        live = self._owned(owner, request.task)  # an unknown task is not a task to record against
        authorised = [False]
        try:
            return await self._run_checked(owner, request, live, authorised)
        except asyncio.CancelledError:
            if not authorised[0]:
                self._audit_refused_run(request, "cancelled")
            raise
        except (_Refuse, PathError, FsTimeoutError, InvalidRunError, InvalidValueError) as error:
            if not authorised[0]:  # after that, the result entry says how it ended
                self._audit_refused_run(request, _why(error))
            raise

    async def _run_checked(
        self, owner: Owner, request: RunInSandbox, live: _Live, authorised: list[bool]
    ) -> ResponseBody:
        config = self._config
        variables, allowlist = sandbox_environment(
            [(pair.name, pair.value) for pair in request.env], path=config.sandbox_path
        )
        # Reserved before the first await: requests that arrive together cannot all pass these
        # checks (a run ID is used once, a task has so many runs).
        if request.run in live.runs or request.run in live.pending_runs:
            raise _Refuse("invalid_request", "that run ID was already used", "run_id_reused")
        if len(live.runs) + len(live.pending_runs) >= config.max_runs_per_task:
            raise _Refuse("busy", "the task has used all the runs it may", "run_cap")
        if self._slots.locked():
            raise _Refuse("busy", "too many runs are in progress", "run_slots")
        live.pending_runs.add(request.run)
        try:
            return await self._run_reserved(owner, request, variables, allowlist, authorised)
        finally:
            live.pending_runs.discard(request.run)

    async def _run_reserved(
        self,
        owner: Owner,
        request: RunInSandbox,
        variables: tuple[EnvVar, ...],
        allowlist: frozenset[EnvName],
        authorised: list[bool],
    ) -> ResponseBody:
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
                authorised[0] = True
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
            raise _Refuse(
                "timeout", "the host's limits could not be checked in time", "limits_probe_timeout"
            ) from None
        missing = [
            kind for kind in plan.unenforced if kind in (LimitKind.PIDS, LimitKind.MEMORY_TOTAL)
        ]
        if missing:
            raise _Refuse(
                "unavailable",
                "this host cannot enforce the sandbox's process and memory limits",
                "limits_unenforced",
            )

    def _audit_refused_run(self, request: RunInSandbox, reason: str) -> None:
        """Record that a run was refused before it was authorised. The refusal stands, and is
        answered, if the entry cannot be written."""
        try:
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
        except AuditError:
            _log.error("a refused run could not be audited")

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
            raise _Refuse(
                "invalid_request", "the sandbox policy was refused", "policy_refused"
            ) from None
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
        # Defence in depth, not observable on its own: the reservation made at the start already
        # keeps a second run with this ID, and a run past the cap, from getting here. Re-checked
        # where the run is recorded so that this function is right whatever its caller did.
        if request.run in live.runs:
            raise _Refuse("invalid_request", "that run ID was already used", "run_id_reused")
        if len(live.runs) >= self._config.max_runs_per_task:
            raise _Refuse("busy", "the task has used all the runs it may", "run_cap")
        now = self._now()
        again = select_grants(live.state, context, self._grants_for(live), rules=rules, now=now)
        if [g.id for g in again.applicable] != [g.id for g in selection.applicable] or (
            again.task_taint != selection.task_taint
        ):
            raise _Refuse(
                "busy",
                "the grants changed while the run was being set up: try again",
                "grants_changed",
            )
        once = [grant for grant in selection.applicable if isinstance(grant, OnceGrant)]
        spent = [grant.consume(request.run) for grant in once]
        with self._atomic() as entries:
            for grant in spent:
                self._store.replace_grant(grant)
                self._store.revoke_grant(grant.id, now)
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
                    mounted=_ids(mount.grant for mount in mounts),
                    consumed=_ids(grant.id for grant in spent),
                    wall_seconds=request.wall_seconds,
                    require_landlock=self._config.require_landlock,
                )
            )
        live.runs.add(request.run)
        for old in once:
            live.grants.remove(old)  # used up by this run
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


def _tool_summary(record: ToolRecord) -> ToolSummary:
    return ToolSummary(
        claimed_taint=taint_names(record.claimed_taint),
        tool=record.tool,
        version=record.version,
        hash=record.hash,
        name=record.name,
        description=record.description,
        tags=tuple(str(tag) for tag in record.tags),
        taint=taint_names(record.taint),
        origin=record.origin,
        saved_at=record.saved_at,
    )


def _saved_entry(stored: StoredTool, at: datetime, *, task: TaskId | None) -> ToolSaved:
    record = stored.record
    return ToolSaved(
        at=at,
        tool=record.tool,
        version=record.version,
        hash=record.hash,
        size=int(record.size),
        taint=taint_names(record.taint),
        claimed_taint=taint_names(record.claimed_taint),
        origin=record.origin,
        task=task,
    )


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
