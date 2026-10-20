"""The registry of IPC message bodies: what a peer can ask, and what it can answer.

Both registries are closed discriminated unions on `kind`, listed in `RequestBody` and
`ResponseBody` at the bottom of this module. Closed means that the wire accepts exactly these
classes and nothing else, that a handler can match on them exhaustively with `assert_never`, and
that the exported JSON Schema (`schemas/ipc.v1.json`) lists all of them.

To add a message (S17 and later steps do this; nothing else in `ipc` changes):

1. Subclass `Request` (or `Response`, from `tumbleweed.ipc.base`), mark the class `@final`, and
   give it `kind: Literal["<unique_name>"]` without a default, so that `kind` is required on the
   wire and in the schema. Fields use strict types, typed IDs from `tumbleweed.core.ids` and bounded
   strings and collections; never `dict[str, Any]`.
2. Add the class to `RequestBody` or `ResponseBody`.
3. Regenerate the schema: `uv run python -m tumbleweed.ipc.schema > schemas/ipc.v1.json`. A
   drift test fails until the committed file matches the registry.

The messages below are the transport's own diagnostics (liveness and an echo that carries
text). They are not business messages, and `Connection` answers them itself: a request handler
never sees them (unless the connection is made with `answer_diagnostics=False`, a seam that
tests use). The broker's messages (S17) are defined in `tumbleweed.ipc.broker_messages` and
listed below, the trusted prompt's (S18) in `tumbleweed.ipc.prompt_messages`. A request is always
answered by exactly one response body or one error, and which response type a request gets is the
caller's expectation, checked by `Connection.call`.

Requests go one way each (#61): `BROKER_SERVES` are what the shell asks the broker (including
`AnswerPrompt`, the user's answer), `SHELL_SERVES` what the broker asks the shell (`ShowPrompt`,
`ClosePrompt`). `RequestBody` is the wire's union of both, which is all a frame can say; a
connection made with a `Role` (the broker's daemon and the shell's client always are) answers a
request that its own role does not serve with `unsupported_request` before any handler sees it,
and refuses to send one its peer does not serve. So a process that reaches the broker's socket
cannot make the broker "show" a prompt or the shell "answer" one.
"""

from enum import StrEnum
from typing import Annotated, Final, Literal, final

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
from tumbleweed.ipc.prompt_messages import (
    AnswerPrompt,
    ClosePrompt,
    PromptAnswered,
    PromptClosed,
    PromptShown,
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
    | RunInSandbox
    | ListTools
    | ShowTool
    | ExportTool
    | ImportTool
    | AnswerPrompt
    | ShowPrompt
    | ClosePrompt,
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
    | ToolList
    | ToolShown
    | ToolBundle
    | ToolImported
    | PromptShown
    | PromptClosed
    | PromptAnswered
    | Refused,
    Field(discriminator="kind"),
]
"""Every response a peer may send. The closed union to extend (see the module docstring)."""


class Role(StrEnum):
    """Which end of the broker connection a `Connection` is, and so which requests it serves."""

    BROKER = "broker"
    SHELL = "shell"


BROKER_SERVES: Final[tuple[type[Request], ...]] = (
    StartTask,
    EndTask,
    RequestGrant,
    RevokeGrant,
    ReadWorkspaceFile,
    ReadFile,
    ListWorkspace,
    ListDir,
    RunInSandbox,
    ListTools,
    ShowTool,
    ExportTool,
    ImportTool,
    AnswerPrompt,
)
"""The requests the broker serves: what the shell (and `tumbleweed-tools`) asks it."""

SHELL_SERVES: Final[tuple[type[Request], ...]] = (ShowPrompt, ClosePrompt)
"""The requests the shell serves: what the broker asks it."""

DIAGNOSTICS: Final[tuple[type[Request], ...]] = (Ping, Echo)
"""Both ends answer these, whatever their role."""


def serves(role: Role) -> tuple[type[Request], ...]:
    """The request types an end with `role` serves, diagnostics apart."""
    return BROKER_SERVES if role is Role.BROKER else SHELL_SERVES


def sends(role: Role) -> tuple[type[Request], ...]:
    """The request types an end with `role` may send: what the other end serves."""
    return SHELL_SERVES if role is Role.BROKER else BROKER_SERVES


__all__ = [
    "BROKER_SERVES",
    "DIAGNOSTICS",
    "ECHO_MAX_CHARS",
    "SHELL_SERVES",
    "Echo",
    "Echoed",
    "IpcMessage",
    "Ping",
    "Pong",
    "Request",
    "RequestBody",
    "Response",
    "ResponseBody",
    "Role",
    "sends",
    "serves",
]
