"""The registry of IPC message bodies: what a peer can ask, and what it can answer.

Both registries are closed discriminated unions on `kind`, listed in `RequestBody` and
`ResponseBody` at the bottom of this module. Closed means that the wire accepts exactly these
classes and nothing else, that a handler can match on them exhaustively with `assert_never`, and
that the exported JSON Schema (`schemas/ipc.v1.json`) lists all of them.

To add a message (S17 and later steps do this; nothing else in `ipc` changes):

1. Subclass `Request` (or `Response`), mark the class `@final`, and give it
   `kind: Literal["<unique_name>"]` without a default, so that `kind` is required on the wire
   and in the schema. Fields use strict types, typed IDs from `tumbleweed.core.ids` and bounded
   strings and collections; never `dict[str, Any]`.
2. Add the class to `RequestBody` or `ResponseBody`.
3. Regenerate the schema: `uv run python -m tumbleweed.ipc.schema > schemas/ipc.v1.json`. A
   drift test fails until the committed file matches the registry.

The messages below are the transport's own diagnostics (liveness and an echo that carries
text). They are not business messages; the broker's and the shell's come with S17 and S18.
A request is always answered by exactly one response body or one error, and which response
type a request gets is the caller's expectation, checked by `Connection.call`.
"""

from typing import Annotated, Literal, final

from pydantic import BaseModel, ConfigDict, Field

ECHO_MAX_CHARS = 1024


class IpcMessage(BaseModel):
    """Base of every model that crosses the IPC boundary: strict, frozen, no extra fields."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Request(IpcMessage):
    """Base of request bodies: what a peer asks the other peer to do."""


class Response(IpcMessage):
    """Base of response bodies: what a peer answers a request with."""


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


RequestBody = Annotated[Ping | Echo, Field(discriminator="kind")]
"""Every request a peer may send. The closed union to extend (see the module docstring)."""

ResponseBody = Annotated[Pong | Echoed, Field(discriminator="kind")]
"""Every response a peer may send. The closed union to extend (see the module docstring)."""
