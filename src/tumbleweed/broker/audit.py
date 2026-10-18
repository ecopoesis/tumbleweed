"""The audit log: an append-only JSONL file with one typed entry per privileged event (I7).

Every grant decision, sandbox run and revocation is a line of `audit.jsonl` in the broker's data
directory. A line is the JSON of one `AuditEntry`: a pydantic model with a `kind` discriminator
and a format version `v`. The entries are closed (`extra="forbid"`) for the readers of this
version; a later step adds a field with a default (a content hash, say) or a new kind, and
reads old lines unchanged. The entries of the tool store (S23) name a package by its content hash;
nothing else here hashes content.

What the file guarantees, and what it does not:

- Appended, never rewritten: opened `O_APPEND | O_NOFOLLOW` as a regular file of ours with one
  link, mode 0600, in a 0700 directory. Each entry goes out in one `write` that is retried if
  short; one that fails halfway is cut off again (`ftruncate`) so that the file never holds half
  a line. The broker never seeks, truncates or rewrites it otherwise.
- Written **before** the act it records, inside the database transaction that makes the act
  (`BrokerService`), so an act whose entry cannot be written does not happen
  (`AuditError`). A run has two entries for that reason: `sandbox_run`, the authorisation,
  before it starts, and `sandbox_run_result`, how it ended.
- Not fsynced: an entry survives a crash of the broker, not a loss of power. Not tamper
  evident either: anything running as the user can edit the file (ARCHITECTURE section 8 puts
  same-uid processes out of scope), and nothing rotates it (#82).
- Bounded: an entry is at most `MAX_ENTRY_BYTES`, because every field is bounded. Text that
  came from outside (a model's reason, a path) is cut to its field's limit; the file is JSONL, so
  the whole line is ASCII (`ensure_ascii`), so that no character that a reader
  might take for a line break (a newline, U+0085, U+2028, U+2029) can start a second entry.
"""

import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, Self, final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from tumbleweed.broker.errors import AuditError
from tumbleweed.broker.privatefs import open_private_directory, open_private_file
from tumbleweed.core.ids import GrantId, RunId, TaskId, ToolId
from tumbleweed.core.types import ContentHash, ToolVersion

_log = logging.getLogger(__name__)

AUDIT_FILE_NAME: Final = "audit.jsonl"
MAX_ENTRY_BYTES: Final = 64 * 1024
FORMAT_VERSION: Final = 1
_TEXT: Final = 500
_PATH: Final = 4096
MAX_AUDITED_IDS: Final = 64
MAX_ROLLED_BACK_KINDS: Final = 128
_MAX_LIST: Final = MAX_AUDITED_IDS


class _AuditModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    v: Literal[1] = FORMAT_VERSION
    at: AwareDatetime


type Text = Annotated[str, Field(max_length=_TEXT)]
type PathText = Annotated[str, Field(max_length=_PATH)]
type Capability = Literal["fs.read", "fs.write", "net.full"]
type GrantIds = Annotated[tuple[GrantId, ...], Field(max_length=_MAX_LIST)]


def cut(text: str, limit: int = _TEXT) -> str:
    """`text` cut to `limit` characters (what an entry field of that size holds)."""
    return text[:limit]


class DaemonStart(_AuditModel):
    """The broker started, and what it requires of the host. A relaxed requirement is a
    development setting that the log must show."""

    kind: Literal["daemon_start"] = "daemon_start"
    pid: Annotated[int, Field(ge=0)]
    require_landlock: bool
    require_scope_limits: bool


class TaskStarted(_AuditModel):
    kind: Literal["task_started"] = "task_started"
    task: TaskId
    previous: TaskId | None
    fresh_context: bool
    taint: Annotated[tuple[Literal["fs", "net"], ...], Field(max_length=1)]


class TaskEndReason(StrEnum):
    REQUESTED = "requested"
    REPLACED = "replaced"  # `start_task` named it as the task it replaces
    CONNECTION_CLOSED = "connection_closed"
    STARTUP = "startup"  # a task left live by a broker that stopped


class TaskEnded(_AuditModel):
    kind: Literal["task_ended"] = "task_ended"
    task: TaskId
    reason: TaskEndReason
    revoked: GrantIds  # the first `MAX_AUDITED_IDS`; `revoked_count` says how many in all
    revoked_count: Annotated[int, Field(ge=0)] | None = None


class GrantDecision(_AuditModel):
    """The decision on a request for access: already covered, granted by the user, or denied.

    `reason` is the refusal's kind (`static_deny`, `taint_conflict`, ...) or, for a denial by
    the user, what the prompt said (`user_denied`, `prompt_timeout`, `prompt_failed`).
    `model_reason` is what the model said it wants the access for: its words, cut to length,
    kept so that a reader of the log can see what the user was told.
    """

    kind: Literal["grant_decision"] = "grant_decision"
    task: TaskId
    run: RunId
    capability: Capability
    path: PathText | None
    outcome: Literal["covered", "granted", "denied"]
    grant: GrantId | None
    scope: Literal["once", "task"] | None
    reason: Text | None
    model_reason: Text | None = None
    expires_at: AwareDatetime | None = None


class FileAccess(_AuditModel):
    """A read or listing under a grant of a task: allowed (`grant`, and whether a once grant
    was `consumed`) or refused. The path is the canonical text the decision was made on."""

    kind: Literal["file_access"] = "file_access"
    task: TaskId
    run: RunId
    operation: Literal["read_file", "list_dir"]
    path: PathText
    outcome: Literal["allowed", "refused"]
    grant: GrantId | None
    consumed: bool
    reason: Text | None


class GrantRevoked(_AuditModel):
    kind: Literal["grant_revoked"] = "grant_revoked"
    task: TaskId | None
    grant: GrantId
    reason: Literal["requested", "task_ended"]


class SandboxRun(_AuditModel):
    """A sandbox run was authorised (written before it starts) or refused.

    `command` is the program only (the first argument) and the count of the rest: arguments can
    hold whatever the model wrote and are not kept. `mounted` are the grants whose paths were
    bound into the run, `consumed` the once grants it used up.
    """

    kind: Literal["sandbox_run"] = "sandbox_run"
    task: TaskId
    run: RunId
    outcome: Literal["authorised", "refused"]
    reason: Text | None
    program: Text
    arguments: Annotated[int, Field(ge=0)]
    env_names: Annotated[tuple[Annotated[str, Field(max_length=64)], ...], Field(max_length=16)]
    mounted: GrantIds
    consumed: GrantIds
    wall_seconds: Annotated[int, Field(ge=0)]
    require_landlock: bool


class SandboxRunResult(_AuditModel):
    """How an authorised run ended: `completed` (the program ended, or was stopped at a limit;
    `termination` says which), `failed` (the sandbox could not be set up), or `cancelled`."""

    kind: Literal["sandbox_run_result"] = "sandbox_run_result"
    task: TaskId
    run: RunId
    outcome: Literal["completed", "failed", "cancelled"]
    reason: Text | None
    termination: Literal["exited", "timed_out", "output_limit"] | None
    exit_code: int | None
    wall_milliseconds: Annotated[int, Field(ge=0)] | None
    unenforced: Annotated[tuple[str, ...], Field(max_length=3)]
    landlock: Literal["applied", "unavailable", "unknown"] | None


class RolledBack(_AuditModel):
    """The act the entries just before this one recorded did not happen: its transaction could
    not be committed after they were written. `entries` are the kinds of the first
    `MAX_ROLLED_BACK_KINDS` of them, in order, and `count` how many there were.

    It follows a failed commit, and is also written when the end of a task whose connection had
    gone could not be committed: in that case the task is ended in memory (it is unusable, its
    runs are stopped) while the database still says live, until the next start ends it on record
    (`task_ended` with reason `startup`).
    """

    kind: Literal["rolled_back"] = "rolled_back"
    entries: Annotated[
        tuple[Annotated[str, Field(max_length=32)], ...], Field(max_length=MAX_ROLLED_BACK_KINDS)
    ]
    count: Annotated[int, Field(ge=0)]


class WorkspaceDeleted(_AuditModel):
    """Retention: a task's workspace was deleted after its retention period."""

    kind: Literal["workspace_deleted"] = "workspace_deleted"
    task: TaskId
    outcome: Literal["deleted", "missing", "failed"]
    reason: Text | None


class ToolSaved(_AuditModel):
    """A version of a tool was stored: saved from `task` (the task whose taint it records), or
    imported from a bundle (no task; `taint` is what the bundle claims). Written before the version
    is committed. A bundle that holds a version the store already has changes nothing and has no
    entry."""

    kind: Literal["tool_saved"] = "tool_saved"
    tool: ToolId
    version: ToolVersion
    hash: ContentHash
    size: Annotated[int, Field(ge=0)]
    taint: Annotated[tuple[Literal["fs", "net"], ...], Field(max_length=2)]  # effective (I12)
    origin: Literal["saved", "imported"]
    task: TaskId | None
    claimed_taint: Annotated[tuple[Literal["fs", "net"], ...], Field(max_length=1)] = ()


class ToolExported(_AuditModel):
    """A bundle of a stored version was handed out. Written before it is, so that a bundle that
    left the broker has an entry. Its taint is recorded: it is what the package may embed."""

    kind: Literal["tool_exported"] = "tool_exported"
    tool: ToolId
    version: ToolVersion
    hash: ContentHash
    taint: Annotated[tuple[Literal["fs", "net"], ...], Field(max_length=2)]  # effective (I12)


class ToolImportRefused(_AuditModel):
    """A bundle was refused, with a closed reason; nothing of it is kept (it is hostile input).
    `integrity` is a stored package that failed its check while the import looked at it, `error`
    any other failure of the store or the disk."""

    kind: Literal["tool_import_refused"] = "tool_import_refused"
    reason: Literal["invalid", "too_large", "conflict", "full", "integrity", "error"]


type AuditEntry = Annotated[
    DaemonStart
    | TaskStarted
    | TaskEnded
    | GrantDecision
    | FileAccess
    | GrantRevoked
    | SandboxRun
    | SandboxRunResult
    | WorkspaceDeleted
    | ToolSaved
    | ToolExported
    | ToolImportRefused
    | RolledBack,
    Field(discriminator="kind"),
]

_ENTRIES: Final[TypeAdapter[AuditEntry]] = TypeAdapter(AuditEntry)


def parse_entry(line: bytes) -> AuditEntry:
    """The entry on one line of the log. `AuditError` if it is not one (fixed message)."""
    try:
        return _ENTRIES.validate_json(line, strict=True)
    except ValidationError:
        raise AuditError("an audit line is not a valid entry") from None


def read_entries(path: Path) -> list[AuditEntry]:
    """Every entry of the log at `path`, in order (for inspection and tests)."""
    return [parse_entry(line) for line in path.read_bytes().splitlines() if line]


@final
class AuditLog:
    """The open log. `AuditLog.open(directory)`; `append` one entry per event; `close`."""

    def __init__(self, directory_fd: int, file_fd: int) -> None:
        self._directory_fd = directory_fd
        self._file_fd = file_fd

    @classmethod
    def open(cls, directory: Path) -> Self:
        """Open (creating if needed) `directory/audit.jsonl`. `PrivateFsError` if it is unsafe."""
        directory_fd = open_private_directory(directory)
        try:
            file_fd = open_private_file(directory_fd, AUDIT_FILE_NAME, append=True)
        except BaseException:
            os.close(directory_fd)
            raise
        return cls(directory_fd, file_fd)

    def append(self, entry: AuditEntry) -> None:
        """Write one entry. `AuditError` if it cannot be written whole (nothing is left of it)."""
        try:
            line = _ENTRIES.dump_json(entry, warnings="error", ensure_ascii=True) + b"\n"
        except (ValidationError, ValueError, TypeError):
            raise AuditError("an audit entry could not be encoded") from None
        if len(line) > MAX_ENTRY_BYTES:
            raise AuditError("an audit entry is too large")
        try:
            size = os.fstat(self._file_fd).st_size
            try:
                self._write_all(line)
            except OSError:
                self._cut_back(size)
                raise
        except OSError as exc:
            _log.error("the audit log cannot be written: %s", exc.strerror)
            raise AuditError("the audit log cannot be written") from None

    def _write_all(self, line: bytes) -> None:
        view = memoryview(line)
        while view:
            written = os.write(self._file_fd, view)
            view = view[written:]

    def _cut_back(self, size: int) -> None:
        try:
            os.ftruncate(self._file_fd, size)
        except OSError:
            _log.error("a failed audit write could not be cut back; the log may end in a fragment")

    def close(self) -> None:
        """Close the descriptors. Idempotent."""
        file_fd, self._file_fd = self._file_fd, -1
        directory_fd, self._directory_fd = self._directory_fd, -1
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)
