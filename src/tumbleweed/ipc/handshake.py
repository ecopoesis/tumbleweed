"""The handshake that opens every connection: the peers agree on the protocol version.

The client speaks first with `Hello(version)`. The server answers with its own `Hello` if the
versions are equal, and otherwise with an `ErrorFrame` (`incompatible_protocol`, or
`bad_handshake` for anything that is not a valid hello) and closes: the peer gets a structured
refusal, and both ends raise a `HandshakeError`. Until the handshake is done the only frame a
server reads is one hello of at most `MIN_FRAME_BYTES`, within the handshake timeout.
"""

import asyncio
import contextlib
from datetime import timedelta

from tumbleweed.core.errors import excerpt
from tumbleweed.ipc.errors import (
    ErrorCode,
    HandshakeError,
    IncompatibleProtocolError,
    IpcError,
)
from tumbleweed.ipc.framing import encode_frame, read_frame, write_bytes
from tumbleweed.ipc.limits import MIN_FRAME_BYTES, IpcLimits
from tumbleweed.ipc.wire import (
    PROTOCOL_VERSION,
    ErrorFrame,
    Frame,
    Hello,
    decode_message,
    encode_message,
)

_REFUSAL_SECONDS = timedelta(seconds=1)


def _encode(frame: Frame) -> bytes:
    return encode_frame(encode_message(frame), max_bytes=MIN_FRAME_BYTES)


async def _read(reader: asyncio.StreamReader, limits: IpcLimits) -> Frame:
    payload = await read_frame(
        reader, max_bytes=MIN_FRAME_BYTES, frame_timeout=limits.frame_timeout
    )
    if payload is None:
        raise HandshakeError("the peer closed the connection during the handshake")
    return decode_message(payload)


async def client_handshake(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, limits: IpcLimits
) -> None:
    """Say hello and check the answer. Raises `HandshakeError` (or a subclass) on failure."""
    try:
        async with asyncio.timeout(limits.handshake_timeout.total_seconds()):
            await write_bytes(
                writer,
                _encode(Hello(kind="hello", version=PROTOCOL_VERSION)),
                within=limits.write_timeout,
            )
            answer = await _read(reader, limits)
    except TimeoutError:
        raise HandshakeError("the peer did not answer the hello in time") from None
    except OSError as exc:
        raise HandshakeError(f"connection lost during the handshake: {exc.strerror}") from None
    except HandshakeError:
        raise
    except IpcError as exc:
        raise HandshakeError(f"the peer's answer to the hello is invalid: {exc}") from None
    if isinstance(answer, Hello):
        if answer.version != PROTOCOL_VERSION:
            raise IncompatibleProtocolError(
                f"the peer speaks protocol {answer.version}, we speak {PROTOCOL_VERSION}",
                peer_version=int(answer.version),
            )
        return
    if isinstance(answer, ErrorFrame) and answer.id is None:
        text = f"the peer refused the handshake ({answer.code.value}): {excerpt(answer.message)}"
        if answer.code is ErrorCode.INCOMPATIBLE_PROTOCOL:
            raise IncompatibleProtocolError(text, peer_version=None)
        raise HandshakeError(text)
    raise HandshakeError(f"the peer answered the hello with a frame of kind {answer.kind!r}")


async def _refuse(writer: asyncio.StreamWriter, code: ErrorCode, message: str) -> None:
    """Tell the peer why it is being refused, if it will listen for a moment."""
    frame = ErrorFrame(kind="error", id=None, code=code, message=message)
    with contextlib.suppress(TimeoutError, OSError):
        await write_bytes(writer, _encode(frame), within=_REFUSAL_SECONDS)


async def server_handshake(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, limits: IpcLimits
) -> None:
    """Read the client's hello and answer it. Raises `HandshakeError` (or a subclass).

    A peer whose first frame is not a valid hello, or whose version differs, is sent a
    structured refusal before the error is raised; the caller closes the connection.
    """
    try:
        async with asyncio.timeout(limits.handshake_timeout.total_seconds()):
            try:
                hello = await _read(reader, limits)
            except HandshakeError:
                raise
            except IpcError as exc:  # a framing error, or a payload that is not a frame
                await _refuse(writer, ErrorCode.BAD_HANDSHAKE, "the first frame must be a hello")
                raise HandshakeError(f"no valid hello: {exc}") from None
            if not isinstance(hello, Hello):
                await _refuse(writer, ErrorCode.BAD_HANDSHAKE, "the first frame must be a hello")
                raise HandshakeError(f"the first frame was of kind {hello.kind!r}, not a hello")
            if hello.version != PROTOCOL_VERSION:
                await _refuse(
                    writer,
                    ErrorCode.INCOMPATIBLE_PROTOCOL,
                    f"this peer speaks protocol {PROTOCOL_VERSION}, not {hello.version}",
                )
                raise IncompatibleProtocolError(
                    f"the peer speaks protocol {hello.version}, we speak {PROTOCOL_VERSION}",
                    peer_version=int(hello.version),
                )
            await write_bytes(
                writer,
                _encode(Hello(kind="hello", version=PROTOCOL_VERSION)),
                within=limits.write_timeout,
            )
    except TimeoutError:
        raise HandshakeError("the peer did not complete the handshake in time") from None
    except OSError as exc:
        raise HandshakeError(f"connection lost during the handshake: {exc.strerror}") from None
