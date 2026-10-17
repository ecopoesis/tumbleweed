"""The registry of IPC message bodies: what a peer can ask, and what it can answer.

Both registries are closed discriminated unions on `kind`, listed in `RequestBody` and
`ResponseBody` at the bottom of this module. Closed means that the wire accepts exactly these
classes and nothing else, that a handler can match on them exhaustively with `assert_never`, and
that the exported JSON Schema (`schemas/ipc.v1.json`) lists all of them.

To add a message (S17 and later steps do this; nothing else in `ipc` changes):

1. Subclass `Request` (or `Response`, from `tumbleweed.ipc.base`), mark the class `@final`, and give it
   `kind: Literal["<unique_name>"]` without a default, so that `kind` is required on the wire
   and in the schema. Fields use strict types, typed IDs from `tumbleweed.core.ids` and bounded
   strings and collections; never `dict[str, Any]`.
2. Add the class to `RequestBody` or `ResponseBody`.
3. Regenerate the schema: `uv run python -m tumbleweed.ipc.schema > schemas/ipc.v1.json`. A
   drift test fails until the committed file matches the registry.

The messages below are the transport's own diagnostics (liveness and an echo that carries
text). They are not business messages, and `Connection` answers them itself: a request handler
never sees them (unless the connection is made with `answer_diagnostics=False`, a seam that
tests use). The broker's messages (S17) are defined in `tumbleweed.ipc.broker_messages` and
listed below; the shell's come with S18. A request is always answered by exactly one response body
or one error, and which response type a request gets is the caller's expectation, checked by
`Connection.call`.
"""

from typing import Annotated, Literal, final

from pydantic import Field

from tumbleweed.ipc.base import IpcMessage, Request, Response
from tumbleweed.ipc.broker_messages import (
    DirListing,
    EndTask,
    FileContent,
    GrantOutcome,
    ListDir,
    ListWorkspace,
    ReadFile,
    ReadWorkspaceFile,
    Refused,
    RequestGrant,
    Revoked,
    RevokeGrant,
    RunInSandbox,
    RunResult,
    StartTask,
    TaskEnded,
    TaskStarted,
)

ECHO_MAX_CHARS = 1024


@final
class Ping(Request):
    """Are you there? Answered with `Pong`."""

    kind: Literal["ping"]


@final
class Pong(Response):
    """The answer to `Ping`."""

    kind: Literal["pong"]


@final
class Echo(Request):
    """Send back this text. Answered with `Echoed`. A diagnostic and a test vehicle."""

    kind: Literal["echo"]
    text: str = Field(max_length=ECHO_MAX_CHARS)


@final
class Echoed(Response):
    """The answer to `Echo`: the same text."""

    kind: Literal["echoed"]
    text: str = Field(max_length=ECHO_MAX_CHARS)


RequestBody = Annotated[
    Ping
    | Echo
    | StartTask
    | EndTask
    | RequestGrant
    | RevokeGrant
    | ReadWorkspaceFile
    | ReadFile
    | ListWorkspace
    | ListDir
    | RunInSandbox,
    Field(discriminator="kind"),
]
"""Every request a peer may send. The closed union to extend (see the module docstring)."""

ResponseBody = Annotated[
    Pong
    | Echoed
    | TaskStarted
    | TaskEnded
    | GrantOutcome
    | Revoked
    | FileContent
    | DirListing
    | RunResult
    | Refused,
    Field(discriminator="kind"),
]
"""Every response a peer may send. The closed union to extend (see the module docstring)."""

__all__ = [
    "ECHO_MAX_CHARS",
    "Echo",
    "Echoed",
    "IpcMessage",
    "Ping",
    "Pong",
    "Request",
    "RequestBody",
    "Response",
    "ResponseBody",
]
