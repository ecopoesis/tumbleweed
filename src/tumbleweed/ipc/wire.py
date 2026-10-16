"""The frames of the IPC protocol and their JSON codec.

One frame is the payload of one length-prefixed record (see `framing`): a JSON object whose
`kind` is one of

- `hello`: the handshake. The client speaks first with its protocol version; the server answers
  with its own `hello` or with an `error` frame (`incompatible_protocol`, `bad_handshake`) and
  closes. Versions must be equal. Any other frame before the handshake is complete is refused.
- `request`: an `id` (an `IpcRequestId`, chosen by the sender and unique among its requests
  still in flight) and a `body`, one of `messages.RequestBody`.
- `response`: the `id` of the request it answers and a `body`, one of `messages.ResponseBody`.
- `error`: the answer to the request `id`, or, with `id` null, the sender's last word before it
  closes the connection. It carries one of the `ErrorCode`s and a short message.

Both peers send requests and answer them on one connection at the same time; ids are scoped to
the connection and the direction. All of it is strict: exact JSON types, no unknown fields, no
duplicate keys in any object (pydantic would silently take the last), nothing but UTF-8.

The protocol version names the wire format. While the shell and the broker are built from one
tree and always upgraded together, new message kinds are added to a version and the committed
schema (`schemas/ipc.v1.json`) is regenerated; the first time two different builds have to
interoperate, the version is bumped and a new schema file started.
"""

import json
from typing import Annotated, Final, Literal, NewType, final

from pydantic import Field, TypeAdapter, ValidationError

from tumbleweed.core.ids import IpcRequestId
from tumbleweed.ipc.errors import ErrorCode, MalformedFrameError
from tumbleweed.ipc.messages import IpcMessage, RequestBody, ResponseBody

ProtocolVersion = NewType("ProtocolVersion", int)

PROTOCOL_VERSION: Final = ProtocolVersion(1)
MAX_ERROR_MESSAGE_CHARS: Final = 256
_MAX_VERSION: Final = 65535


@final
class Hello(IpcMessage):
    """The handshake: the protocol version the sender speaks."""

    kind: Literal["hello"]
    version: Annotated[ProtocolVersion, Field(strict=True, ge=1, le=_MAX_VERSION)]


@final
class RequestFrame(IpcMessage):
    """A request: `body` is for the receiver to handle, and to answer under the same `id`."""

    kind: Literal["request"]
    id: IpcRequestId
    body: RequestBody


@final
class ResponseFrame(IpcMessage):
    """The successful answer to the request with the same `id`."""

    kind: Literal["response"]
    id: IpcRequestId
    body: ResponseBody


@final
class ErrorFrame(IpcMessage):
    """The failed answer to the request `id`, or (`id` null) the end of the connection."""

    kind: Literal["error"]
    id: IpcRequestId | None
    code: ErrorCode
    message: str = Field(max_length=MAX_ERROR_MESSAGE_CHARS)


Frame = Annotated[Hello | RequestFrame | ResponseFrame | ErrorFrame, Field(discriminator="kind")]
"""Everything that can travel in one frame."""

FRAME_ADAPTER: Final[TypeAdapter[Frame]] = TypeAdapter(Frame)
"""Validates (and serializes) a frame; `schema` exports its JSON Schema."""


def encode_message(frame: Frame) -> bytes:
    """Serialize a frame to compact UTF-8 JSON (not yet length-prefixed).

    A frame is built from validated models, which cannot hold text that is not UTF-8 (a lone
    surrogate is refused when the model is made), so this does not fail for a real frame.
    """
    return FRAME_ADAPTER.dump_json(frame, warnings="error")


def decode_message(payload: bytes) -> Frame:
    """Parse and validate one frame's payload, strictly.

    Raises `MalformedFrameError` with a message that never repeats anything from the payload.
    """
    try:
        frame = FRAME_ADAPTER.validate_json(payload, strict=True)
    except ValidationError as exc:
        first = exc.errors(include_input=False, include_url=False, include_context=False)[0]
        # `from None`: a ValidationError holds the whole input, and tracebacks print it.
        raise MalformedFrameError(
            f"not a valid frame: {first['type']} ({exc.error_count()} errors)"
        ) from None
    _require_unique_keys(payload)
    return frame


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise MalformedFrameError("not a valid frame: an object has a key more than once")
    return dict(pairs)


def _require_unique_keys(payload: bytes) -> None:
    """Refuse duplicate object keys, which pydantic-core would resolve by taking the last.

    Runs after validation, so the payload is known to be a shallow, valid frame (the parser
    that validated it also bounds the nesting depth), which the standard parser can scan.
    """
    try:
        json.loads(payload, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise MalformedFrameError("not a valid frame: the JSON is not well formed") from None
