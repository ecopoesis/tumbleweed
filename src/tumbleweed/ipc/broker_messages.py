"""The messages the shell sends the broker, and what it answers (S17).

Registered in `tumbleweed.ipc.messages`. These are wire types: they hold the IDs of
`tumbleweed.core.ids`, bounded text and closed `Literal`s, and nothing of the broker's own
types (`tumbleweed.ipc` never imports `tumbleweed.broker`; the broker maps these onto its policy
types, and validates again whatever the schema cannot say, such as what a path text names).

Everything is bounded so that a request and the largest answer fit one frame (1 MiB, I9): file
and output bytes travel as base64 text, at most `MAX_FILE_BYTES` and `MAX_OUTPUT_BYTES` before
encoding.

Every request is answered by its own response type or by `Refused`, the one response that says
why the broker would not. A refusal is an answer, not a transport failure: it carries a closed
`RefusalReason` and fixed text, never a path or anything else the request said. A request the
broker cannot make sense of at all (a body the schema refuses) is the connection's error, not
`Refused`.
"""

from typing import Annotated, Final, Literal, final

from pydantic import AwareDatetime, Field

from tumbleweed.core.ids import GrantId, RunId, SnapshotId, TaskId
from tumbleweed.core.types import WorkspaceRelPath
from tumbleweed.ipc.base import IpcMessage, Request, Response

MAX_REASON_CHARS: Final = 500
MAX_PATH_CHARS: Final = 4096
MAX_FILE_BYTES: Final = 512 * 1024
MAX_OUTPUT_BYTES: Final = 256 * 1024
MAX_DIR_ENTRIES: Final = 1000
MAX_COMMAND_ARGS: Final = 256
MAX_ARG_CHARS: Final = 32 * 1024
MAX_ENV_PAIRS: Final = 8
MAX_ENV_VALUE_CHARS: Final = 256
MAX_MESSAGE_CHARS: Final = 300
MAX_PAYLOAD_CHARS: Final = 256 * 1024
MAX_WALL_SECONDS: Final = 120
"""The longest a sandbox run may be asked to last (the broker's own ceiling, I9)."""

_NAME_CHARS: Final = 255


def _base64_chars(raw_bytes: int) -> int:
    return 4 * ((raw_bytes + 2) // 3)


Taint = Literal["fs", "net"]
Scope = Literal["once", "task"]

RefusalReason = Literal[
    "unknown_task",  # not a live task of this connection
    "invalid_request",  # the content is not acceptable (a path, a command, an environment)
    "denied",  # the static rules or the taint rule refuse it
    "not_granted",  # no grant covers it; request access first
    "not_found",
    "unsafe",  # a symlink, a special file, not the object that was granted, a tampered package
    "too_large",
    "busy",  # the broker is at one of its limits, for now
    "exhausted",  # the task has used all the runs it may: waiting will not help
    "unavailable",  # this host cannot do it safely
    "timeout",  # the file system took too long
    "failed",  # the sandbox could not be set up
    "conflict",  # the tool store already has a tool with that ID, and it is not this one (S23)
    "no_undo",  # S21: a run that can change files may not start: nothing could be saved for undo
    "workspace_full",  # S21: the task's workspace holds more than it may
    "not_undoable",  # S21: nothing to undo that can be: ran with no undo, already undone
    "unknown_prompt",  # not a prompt this connection was sent, or one already answered or closed
]


# --- tasks -----------------------------------------------------------------------------------


@final
class StartTask(Request):
    """End `previous` (if it is a live task of this connection) and start a new task.

    The new task starts with the taint of `previous`, unless `fresh_context` (I12): the broker
    owns taint, the caller only says whether the model's context carries over.
    """

    kind: Literal["start_task"]
    previous: TaskId | None
    fresh_context: bool


@final
class TaskStarted(Response):
    kind: Literal["task_started"]
    task: TaskId
    taint: Annotated[tuple[Taint, ...], Field(max_length=1)]


@final
class EndTask(Request):
    """End a task: its once and task grants are revoked, its runs stopped."""

    kind: Literal["end_task"]
    task: TaskId


@final
class TaskEnded(Response):
    kind: Literal["task_ended"]
    revoked_grants: Annotated[int, Field(ge=0, le=1_000_000)]


# --- grants ----------------------------------------------------------------------------------


@final
class ReadAccess(IpcMessage):
    """`fs.read` of the file or directory at the absolute path `path`."""

    kind: Literal["fs.read"]
    path: Annotated[str, Field(min_length=1, max_length=MAX_PATH_CHARS)]


@final
class WriteAccess(IpcMessage):
    """`fs.write` of `path` (S21): asked for the user to decide, for the task only. A run that
    mounts it starts only after the broker has saved what it may change (I13)."""

    kind: Literal["fs.write"]
    path: Annotated[str, Field(min_length=1, max_length=MAX_PATH_CHARS)]


@final
class NetworkAccess(IpcMessage):
    """`net.full`. Can be granted (and taints the task); the sandbox gets a network in S22."""

    kind: Literal["net.full"]


@final
class PickAccess(IpcMessage):
    """`fs.read` of whatever the user picks: no path is named, the user chooses a file or a folder
    in the trusted picker (S18) and the choice is the grant. A pick is a read: never a write."""

    kind: Literal["fs.pick"]
    choose: Literal["file", "folder"]


RequestedAccess = Annotated[
    ReadAccess | WriteAccess | NetworkAccess | PickAccess, Field(discriminator="kind")
]


MAX_SNAPSHOT_PATHS: Final = 16
SnapshotCause = Literal[
    "too_many_bytes",
    "too_many_files",
    "too_deep",
    "special_file",
    "unreadable",
    "unsupported",
    "timeout",
    "store_full",
    "io",
]
"""Why nothing could be saved, as the prompt tells the user (the broker's `SnapshotFault`)."""


@final
class NoUndoAccess(IpcMessage):
    """What the broker asks in the "no undo" prompt (S21, I13): `paths` (canonical, the places a
    run may change) could not be saved, `cause` says why, and the user may let this one run go
    ahead with no undo (`purpose` `run`), or, for an undo, `paths` are what is there now and could
    not be saved first: the user may let the undo go ahead without being able to undo it
    (`purpose` `undo`). Only the broker sends it, in a `ShowPrompt`; a model cannot request it."""

    kind: Literal["no_undo"]
    purpose: Literal["run", "undo"] = "run"
    paths: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=MAX_PATH_CHARS)], ...],
        Field(min_length=1, max_length=MAX_SNAPSHOT_PATHS),
    ]
    cause: SnapshotCause


@final
class RequestGrant(Request):
    """Ask for `access` for `run` of `task`. The user decides, in a trusted prompt or picker (I2).

    `reason` is model text: the broker bounds it and shows it as quoted, never as a fact.
    """

    kind: Literal["request_grant"]
    task: TaskId
    run: RunId
    access: RequestedAccess
    reason: Annotated[str, Field(max_length=MAX_REASON_CHARS)]


@final
class GrantOutcome(Response):
    """The answer to `RequestGrant`: granted (`grant`, `scope`), already covered, or denied.

    `message` is fixed text that names the rule, never the path. `picked` is the canonical path of
    what the user chose in the picker, for a picker grant only (D26 (6), amending D25 (7)): the
    user chose it to give it to the assistant, and without it a granted folder has no name to
    list. It is the broker's resolved text, and the receiver frames it as data.
    """

    kind: Literal["grant_outcome"]
    granted: bool
    grant: GrantId | None
    scope: Scope | None
    message: Annotated[str, Field(max_length=MAX_MESSAGE_CHARS)]
    picked: Annotated[str, Field(min_length=1, max_length=MAX_PATH_CHARS)] | None = None


@final
class RevokeGrant(Request):
    kind: Literal["revoke_grant"]
    task: TaskId
    grant: GrantId


@final
class Revoked(Response):
    kind: Literal["revoked"]


# --- files -----------------------------------------------------------------------------------


@final
class ReadWorkspaceFile(Request):
    """Read a regular file of the task's own workspace. No symlink is followed (I11)."""

    kind: Literal["read_workspace_file"]
    task: TaskId
    path: WorkspaceRelPath
    max_bytes: Annotated[int, Field(ge=1, le=MAX_FILE_BYTES)] = MAX_FILE_BYTES


@final
class ReadFile(Request):
    """Read a regular file under a grant of the task, named by its absolute path."""

    kind: Literal["read_file"]
    task: TaskId
    run: RunId
    path: Annotated[str, Field(min_length=1, max_length=MAX_PATH_CHARS)]
    max_bytes: Annotated[int, Field(ge=1, le=MAX_FILE_BYTES)] = MAX_FILE_BYTES


@final
class FileContent(Response):
    """What was read: `size` bytes, as base64 text in `data`."""

    kind: Literal["file_content"]
    size: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)]
    data: Annotated[str, Field(max_length=_base64_chars(MAX_FILE_BYTES))]


@final
class ListWorkspace(Request):
    """List a directory of the task's workspace (its root when `path` is null)."""

    kind: Literal["list_workspace"]
    task: TaskId
    path: WorkspaceRelPath | None


@final
class ListDir(Request):
    """List a directory under a grant of the task, named by its absolute path."""

    kind: Literal["list_dir"]
    task: TaskId
    run: RunId
    path: Annotated[str, Field(min_length=1, max_length=MAX_PATH_CHARS)]


@final
class DirEntry(IpcMessage):
    """One name in a directory. A symlink is reported as one and never followed."""

    name: Annotated[str, Field(min_length=1, max_length=_NAME_CHARS)]
    entry: Literal["file", "directory", "symlink", "other"]
    size: Annotated[int, Field(ge=0)] | None


@final
class DirListing(Response):
    """The names in a directory, sorted, at most `MAX_DIR_ENTRIES`. `skipped` counts names left
    out because they hold control or format characters (they cannot be shown safely)."""

    kind: Literal["dir_listing"]
    entries: Annotated[tuple[DirEntry, ...], Field(max_length=MAX_DIR_ENTRIES)]
    truncated: bool
    skipped: Annotated[int, Field(ge=0)]


# --- sandbox runs ----------------------------------------------------------------------------


@final
class EnvPair(IpcMessage):
    """A variable the caller sets in the sandbox. Only the broker's allowlist is accepted."""

    name: Annotated[str, Field(min_length=1, max_length=64)]
    value: Annotated[str, Field(max_length=MAX_ENV_VALUE_CHARS)]


@final
class RunInSandbox(Request):
    """Run `command` in a sandbox of `task`: its workspace, and every read grant that applies."""

    kind: Literal["run_in_sandbox"]
    task: TaskId
    run: RunId
    command: Annotated[
        tuple[Annotated[str, Field(max_length=MAX_ARG_CHARS)], ...],
        Field(min_length=1, max_length=MAX_COMMAND_ARGS),
    ]
    env: Annotated[tuple[EnvPair, ...], Field(max_length=MAX_ENV_PAIRS)] = ()
    wall_seconds: Annotated[int, Field(ge=1, le=MAX_WALL_SECONDS)] = 30
    cpu_seconds: Annotated[int, Field(ge=1, le=MAX_WALL_SECONDS)] = 30
    max_output_bytes: Annotated[int, Field(ge=1, le=MAX_OUTPUT_BYTES)] = 64 * 1024
    payload: Annotated[str, Field(max_length=MAX_PAYLOAD_CHARS)] | None = None
    """Text the program reads as the file `/tw-payload` (S20): its code and inputs. Never an
    argument, so it is not on the host's process list."""


@final
class RunResult(Response):
    """How a run that started ended. `unenforced` names the limits this host could not apply
    and `landlock` whether Landlock confined the program: both are the launcher's own report."""

    kind: Literal["run_result"]
    termination: Literal["exited", "timed_out", "output_limit"]
    exit_code: int | None
    stdout: Annotated[str, Field(max_length=_base64_chars(MAX_OUTPUT_BYTES))]
    stderr: Annotated[str, Field(max_length=_base64_chars(MAX_OUTPUT_BYTES))]
    stdout_truncated: bool
    stderr_truncated: bool
    wall_milliseconds: Annotated[int, Field(ge=0)]
    unenforced: Annotated[
        tuple[Literal["memory_total", "pids", "cpu_rate"], ...], Field(max_length=3)
    ]
    landlock: Literal["applied", "unavailable", "unknown"]
    mounted_grants: Annotated[int, Field(ge=0, le=64)]


# --- snapshots and undo (S21) ---------------------------------------------------------------


@final
class UndoLastChange(Request):
    """Undo the most recent not-yet-undone write-enabled run of `task` (the model's tool)."""

    kind: Literal["undo_last_change"]
    task: TaskId


@final
class UndoSnapshot(Request):
    """Undo one snapshot, named in the list the chrome shows (the user's action, never a model
    tool)."""

    kind: Literal["undo_snapshot"]
    snapshot: SnapshotId


@final
class UndoResult(Response):
    """An undo was done: `snapshot` was put back after `safety` saved what was there (so the undo
    can be undone), `paths` places were restored (`written` entries written back, `removed` that
    were new taken away)."""

    kind: Literal["undo_result"]
    snapshot: SnapshotId
    safety: SnapshotId | None
    paths: Annotated[int, Field(ge=1, le=MAX_SNAPSHOT_PATHS)]
    written: Annotated[int, Field(ge=0)]
    removed: Annotated[int, Field(ge=0)]
    skipped: Annotated[int, Field(ge=0)] = 0
    """Entries the saved state had that could not be recreated (a socket or a device)."""
    superseded: Annotated[int, Field(ge=0)] = 0
    """Later changes of other runs on the same places that are no longer undone one by one."""


@final
class ListSnapshots(Request):
    """The newest snapshots of every task, newest first (the chrome's undo list)."""

    kind: Literal["list_snapshots"]
    limit: Annotated[int, Field(ge=1, le=100)] = 50


@final
class SnapshotInfo(IpcMessage):
    """One snapshot, for the list: what it saved (canonical paths, which are data to show, never
    to believe), when, and whether it can be put back (`undoable`) or already was."""

    snapshot: SnapshotId
    kind: Literal["run", "undo"]
    task: TaskId
    taken_at: AwareDatetime
    paths: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=MAX_PATH_CHARS)], ...],
        Field(min_length=1, max_length=MAX_SNAPSHOT_PATHS),
    ]
    undoable: bool
    undone: bool
    superseded: bool = False


@final
class SnapshotList(Response):
    kind: Literal["snapshot_list"]
    snapshots: Annotated[tuple[SnapshotInfo, ...], Field(max_length=100)]


# --- refusal ---------------------------------------------------------------------------------


@final
class Refused(Response):
    """The broker would not: `reason` is a closed set, `message` fixed text."""

    kind: Literal["refused"]
    reason: RefusalReason
    message: Annotated[str, Field(max_length=MAX_MESSAGE_CHARS)]
