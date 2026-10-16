"""Domain exceptions of the IPC layer, and the closed set of codes a peer can answer with.

Every message here is built from sizes, names and our own text. Nothing a peer sent is repeated
in an exception message beyond a short `excerpt`, because these messages end up in logs and in
the UI, and a peer controls everything up to the frame limit.
"""

from enum import StrEnum

from tumbleweed.core.errors import InvalidValueError, TumbleweedError, excerpt


class ErrorCode(StrEnum):
    """Why a peer refused a request, or the whole connection. Part of the wire format."""

    INCOMPATIBLE_PROTOCOL = "incompatible_protocol"
    """The handshake found no protocol version both peers speak."""
    BAD_HANDSHAKE = "bad_handshake"
    """The first frame was not a valid hello."""
    PROTOCOL_VIOLATION = "protocol_violation"
    """The peer broke the wire format; the connection is closed after this error."""
    UNSUPPORTED_REQUEST = "unsupported_request"
    """This peer does not serve that request."""
    TOO_MANY_REQUESTS = "too_many_requests"
    """This peer is already handling as many of the caller's requests as it allows (I9)."""
    TIMEOUT = "timeout"
    """The handler did not answer in time, or the connection was idle for too long."""
    INTERNAL = "internal"
    """The handler failed. Its error is logged by the callee, never sent."""


class IpcError(TumbleweedError):
    """Base class of every error the IPC layer raises on purpose."""


class FramingError(IpcError):
    """The byte stream broke the wire format. The connection cannot resynchronise after it."""


class FrameTooLargeError(FramingError):
    """A frame is over the size limit, announced by a peer or about to be sent by us."""


class TruncatedFrameError(FramingError):
    """The stream ended in the middle of a frame."""


class FrameTimeoutError(FramingError):
    """A frame did not arrive in full in time: a slow or stalled peer."""


class IdleTimeoutError(IpcError):
    """The peer sent nothing for longer than the connection's idle timeout."""


class ProtocolViolationError(IpcError):
    """A well-framed message that the protocol does not allow."""


class MalformedFrameError(ProtocolViolationError):
    """A frame's payload is not valid UTF-8 JSON of one of the protocol's frames."""


class HandshakeError(IpcError):
    """The handshake did not complete."""


class IncompatibleProtocolError(HandshakeError):
    """The peer speaks a protocol version we do not (or says that we speak one it does not)."""

    def __init__(self, message: str, *, peer_version: int | None) -> None:
        super().__init__(message)
        self.peer_version = peer_version


class PeerAuthenticationError(IpcError):
    """The peer's credentials could not be established or are not acceptable."""


class PeerCredentialsError(PeerAuthenticationError):
    """The kernel could not tell us who is on the other end (fail closed)."""


class PeerRejectedError(PeerAuthenticationError):
    """The peer runs as another user."""


class IpcConnectError(IpcError):
    """No connection to the peer's socket could be made."""


class ConnectionClosedError(IpcError):
    """The connection is closed, or closed while a request was waiting on it.

    The reason it ended is the `__cause__` where there is a distinct error.
    """


class RequestTimeoutError(IpcError):
    """A request got no answer within its timeout."""


class TooManyRequestsError(IpcError):
    """We already have as many requests in flight on this connection as the limit allows."""


class UnexpectedResponseError(IpcError):
    """The peer answered a request with a response of another type than the caller expected."""


class RemoteError(IpcError):
    """The peer answered a request, or the whole connection, with a structured error."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"peer answered {code.value}: {excerpt(message)}")
        self.code = code
        self.message = message


class RequestRefused(IpcError):  # noqa: N818  (not named *Error: it answers a request, it is no failure)
    """Raised by a request handler to answer with a structured error instead of a response.

    The message is sent to the caller (cut to the wire limit, and any character that is not
    valid UTF-8 written as an escape), so it must be fit for that: no secrets, no paths the
    caller may not see. Domain refusals ("denied by policy") belong in response types; this is
    for conditions of the transport and of the handler itself.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        given: tuple[object, object] = (code, message)  # a handler may pass anything
        if not isinstance(given[0], ErrorCode) or not isinstance(given[1], str):
            raise InvalidValueError("RequestRefused takes an ErrorCode and a str")
        super().__init__(f"{code.value}: {excerpt(message)}")
        self.code = code
        self.message = message


class SocketError(IpcError):
    """A socket directory or socket path cannot be used: wrong owner or mode, a symlink, ..."""


class AddressInUseError(SocketError):
    """Another process holds the claim on this socket path."""
