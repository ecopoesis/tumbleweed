"""One IPC connection: requests and responses in both directions over one stream.

A `Connection` is made by `connect` (the client) or `IpcServer` (the server) once the handshake
is done; both ends are the same class. Either peer calls the other with `call` and answers the
other's requests with its `RequestHandler`, concurrently and in any order: requests are told
apart by their `IpcRequestId`, which only has meaning on the connection it was sent on.

The connection answers the transport's own diagnostics, `ping` and `echo`, itself: a
`RequestHandler` never sees them (unless the connection is made with `answer_diagnostics=False`,
which exists for tests and for a proxy that must see every request), so a handler matches on
the requests of its own step only. A handler type is still `RequestBody`, which lists the
diagnostics too, until the registry is split per direction (#61).

What is bounded, and what happens at the bound (I9):

- Our requests in flight: `call` raises `TooManyRequestsError` at `max_inflight_calls`.
- The peer's requests in progress: past `max_inflight_handlers` the peer gets a
  `too_many_requests` error and the handler does not run. A request counts as in progress until
  its reply has been written, so a peer that does not read cannot make us run more. A request
  id that is still in progress is a protocol violation and ends the connection.
- Time: `call` waits `request_timeout` (per call, or the default of the limits), writing the
  request included, and raises `RequestTimeoutError`; a handler gets `handler_timeout` and the
  caller is told `timeout`. An answer that arrives after its caller gave up is dropped. Reads
  are bounded by `frame_timeout`, and waiting for the next frame by `idle_timeout` if set.
- Writes. Nothing is written once more than twice `max_frame_bytes` wait in the transport: the
  sender waits (a call within its timeout), and refusals and diagnostics, which the reader sends
  and must not wait for, end the connection instead. If the peer takes no byte for
  `write_timeout` while data waits, the connection ends. This holds however the senders are
  cancelled or time out meanwhile.
- Disconnect: when the peer closes, the stream breaks, the peer violates the protocol or either
  end calls `close`, every request waiting on the connection fails at once with
  `ConnectionClosedError`, every running handler is cancelled and the transport is closed.
  Nothing is left waiting.

Whatever the peer sends that is invalid ends the connection: a frame over the size limit, a
payload that is not a valid frame, a hello after the handshake, a reused request id. The peer is
sent a last `error` frame saying so, if it will take one. An answer for a request that is not
waiting (late, duplicated or invented) is ignored, because it cannot be told apart from an
answer that lost the race with a timeout.

The reader never waits for the peer, so it can always take in what the peer sends, answers
included. Handlers must not assume an order between requests. A handler that wants to call the
peer back while it handles a request uses the connection it is given.
"""

import asyncio
import logging
from datetime import timedelta
from types import TracebackType
from typing import NoReturn, Protocol, Self, assert_never, final

from pydantic import ValidationError

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import IpcRequestId
from tumbleweed.ipc.errors import (
    ConnectionClosedError,
    ErrorCode,
    FrameTooLargeError,
    FramingError,
    IdleTimeoutError,
    IpcError,
    MalformedFrameError,
    ProtocolViolationError,
    RemoteError,
    RequestRefused,
    RequestTimeoutError,
    TooManyRequestsError,
    UnexpectedResponseError,
)
from tumbleweed.ipc.framing import close_writer, encode_frame, read_frame, write_bytes
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.messages import Echo, Echoed, Ping, Pong, RequestBody, Response, ResponseBody
from tumbleweed.ipc.wire import (
    MAX_ERROR_MESSAGE_CHARS,
    ErrorFrame,
    Frame,
    Hello,
    RequestFrame,
    ResponseFrame,
    decode_message,
    encode_message,
)

_logger = logging.getLogger(__name__)

# How long closing waits for cancelled handlers to unwind, and for a last error frame to go out.
_HANDLER_UNWIND = timedelta(seconds=2)
_GOODBYE = timedelta(seconds=1)
_UNHANDLED = "the request could not be handled"


class RequestHandler(Protocol):
    """Answers the requests a peer sends.

    Return the response, or raise `RequestRefused` to answer with a structured error. Any other
    exception is logged here and the caller is told `internal`, without the exception's text.
    A handler cancelled by something other than the connection closing is answered the same
    way. Match on `request` exhaustively and end with `assert_never(request)`; the diagnostics
    in `RequestBody` do not reach a handler (see the module docstring).
    """

    async def __call__(self, request: RequestBody, connection: "Connection", /) -> ResponseBody: ...


def _error(request_id: IpcRequestId | None, code: ErrorCode, message: str) -> ErrorFrame:
    """An error frame that cannot fail to be built, whatever `code` and `message` are.

    The message is cut to the wire limit and made valid UTF-8 first (a lone surrogate, as in
    `os.fsdecode(b"\xff")`, is written out as an escape). If the frame still cannot be built the
    peer gets a plain `internal`.
    """
    try:
        text = message.encode("utf-8", "backslashreplace").decode("utf-8")
        return ErrorFrame(
            kind="error", id=request_id, code=code, message=text[:MAX_ERROR_MESSAGE_CHARS]
        )
    except (ValidationError, AttributeError, TypeError):
        return ErrorFrame(kind="error", id=request_id, code=ErrorCode.INTERNAL, message=_UNHANDLED)


@final
class Connection:
    """A handshaken connection. Made by `connect` and `IpcServer`, not directly."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        limits: IpcLimits,
        handler: RequestHandler | None,
        answer_diagnostics: bool = True,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._transport = writer.transport
        self._limits = limits
        self._handler = handler
        self._diagnostics = answer_diagnostics
        self._pending: dict[IpcRequestId, asyncio.Future[Response]] = {}
        self._inflight: set[IpcRequestId] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._buffer_cap = 2 * limits.max_frame_bytes
        # `drain` then waits only while more than the cap is queued, which is when senders wait.
        self._transport.set_write_buffer_limits(high=self._buffer_cap)
        self._written = 0
        self._stall_timer: asyncio.TimerHandle | None = None
        self._stall_mark = 0
        self._stalled = False
        self._blocked = 0
        self._close_reason: IpcError | None = None
        self._closed = asyncio.Event()
        self._started = False
        self._runner = asyncio.get_running_loop().create_task(self._run(), name="ipc-connection")

    @property
    def is_closed(self) -> bool:
        """Whether the connection is closed or closing: no new request will be accepted."""
        return self._close_reason is not None

    @property
    def is_finished(self) -> bool:
        """Whether the connection is closed and everything on it has been released."""
        return self._closed.is_set()

    @property
    def close_reason(self) -> IpcError | None:
        """Why the connection ended, or `None` while it is open."""
        return self._close_reason

    @property
    def buffered_bytes(self) -> int:
        """Bytes written to the connection that the peer has not taken yet."""
        return self._transport.get_write_buffer_size()

    async def wait_closed(self) -> None:
        """Wait until the connection has ended and everything on it has been released."""
        await self._closed.wait()

    async def close(self) -> None:
        """Close the connection: fail what is waiting on it, cancel handlers, close the stream."""
        self._abort(ConnectionClosedError("the connection was closed locally"))
        await self.wait_closed()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def call[R: Response](
        self,
        request: RequestBody,
        expect: type[R],
        *,
        request_timeout: timedelta | None = None,
    ) -> R:
        """Send `request` to the peer and return its response, which must be an `expect`.

        The timeout covers everything, including waiting to write the request to a peer that is
        slow to read. Raises `RemoteError` if the peer answers with an error,
        `RequestTimeoutError` if there is no answer in time, `TooManyRequestsError` at the
        in-flight limit, `UnexpectedResponseError` if the response is of another type,
        `ConnectionClosedError` if the connection is closed or closes meanwhile, and
        `FrameTooLargeError` (nothing is sent) if the request is over the frame limit.
        """
        limit: object = self._limits.request_timeout if request_timeout is None else request_timeout
        if type(limit) is not timedelta or limit <= timedelta(0):
            raise InvalidValueError(f"request_timeout must be a positive timedelta, got {limit!r}")
        self._raise_if_closed()
        if len(self._pending) >= self._limits.max_inflight_calls:
            raise TooManyRequestsError(
                f"{len(self._pending)} requests are already in flight "
                f"(limit {self._limits.max_inflight_calls})"
            )
        request_id = IpcRequestId.new()
        try:
            frame = RequestFrame(kind="request", id=request_id, body=request)
        except ValidationError:
            raise TypeError(f"not a registered request body: {type(request).__name__}") from None
        answer: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = answer
        try:
            async with asyncio.timeout(limit.total_seconds()):
                await self._send(frame)
                response = await answer
        except TimeoutError:
            raise RequestTimeoutError(f"no answer within {limit.total_seconds():g} s") from None
        finally:
            del self._pending[request_id]
            if answer.done() and not answer.cancelled():
                answer.exception()  # retrieved, so that asyncio does not log it as lost
        if not isinstance(response, expect):
            raise UnexpectedResponseError(
                f"expected a {expect.__name__}, the peer answered with a {type(response).__name__}"
            )
        return response

    def _raise_if_closed(self) -> None:
        if self._close_reason is not None:
            raise ConnectionClosedError(
                f"the connection is closed: {self._close_reason}"
            ) from self._close_reason

    # Writing. `write` on the transport is synchronous and takes a whole frame, so frames never
    # interleave and no lock is needed. What bounds the queue is `_wait_for_room` (senders
    # wait), `_reply_now` (the reader's sends never wait: past the cap they end the connection)
    # and the stall watch, which does not depend on any sender still being there.

    async def _send(self, frame: Frame) -> None:
        """Write a frame, waiting while the peer is far behind; raises when the connection ends."""
        # Encoded before anything is written, so a frame over the limit is refused cleanly.
        data = encode_frame(encode_message(frame), max_bytes=self._limits.max_frame_bytes)
        await self._wait_for_room()
        self._write(data)
        await self._wait_for_room()

    async def _wait_for_room(self) -> None:
        self._raise_if_closed()
        self._blocked += 1
        try:
            await self._writer.drain()
        except OSError as exc:
            self._lost(exc)
        finally:
            self._blocked -= 1
        self._raise_if_closed()

    def _lost(self, exc: OSError) -> NoReturn:
        error = ConnectionClosedError(f"the connection was lost: {exc.strerror}")
        self._abort(error)
        raise error from None

    def _reply_now(self, frame: ResponseFrame | ErrorFrame) -> None:
        """Write a reply without ever waiting (for the reader); the connection ends instead."""
        try:
            data = encode_frame(encode_message(frame), max_bytes=self._limits.max_frame_bytes)
        except FrameTooLargeError:
            unsendable = _error(frame.id, ErrorCode.INTERNAL, "the response is unsendable")
            data = encode_frame(encode_message(unsendable), max_bytes=self._limits.max_frame_bytes)
        if self.buffered_bytes > self._buffer_cap:
            raise ConnectionClosedError("the peer is not reading from the connection")
        self._write(data)

    def _write(self, data: bytes) -> None:
        self._writer.write(data)
        self._written += len(data)
        if self._stall_timer is None and self.buffered_bytes > 0:
            self._stall_mark = self._written - self.buffered_bytes
            self._stall_timer = asyncio.get_running_loop().call_later(
                self._limits.write_timeout.total_seconds(), self._check_stall
            )

    def _check_stall(self) -> None:
        """End the connection if the peer took not one byte while data waited for it."""
        self._stall_timer = None
        buffered = self.buffered_bytes
        if buffered == 0 or self._close_reason is not None:
            return
        taken = self._written - buffered
        if taken > self._stall_mark:  # slow, but reading: look again later
            self._stall_mark = taken
            self._stall_timer = asyncio.get_running_loop().call_later(
                self._limits.write_timeout.total_seconds(), self._check_stall
            )
            return
        self._stalled = True
        self._abort(ConnectionClosedError("the peer is not reading from the connection"))

    def _abort(self, reason: IpcError) -> None:
        """Start closing, for `reason` unless the connection is already closing."""
        if self._close_reason is not None:
            return
        self._close_reason = reason
        # A runner that has not started reads the reason when it does; one that is running, but
        # not the caller, is woken by a cancellation; the caller itself unwinds by raising.
        if self._started and asyncio.current_task() is not self._runner:
            self._runner.cancel()

    async def _run(self) -> None:
        """Read frames until the connection ends, then release everything on it."""
        self._started = True
        try:
            if self._close_reason is None:
                await self._read_loop()
        except asyncio.CancelledError:
            # Ours, from `_abort` (the reason is set), or the event loop shutting down.
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            self._abort(ConnectionClosedError("the connection was cancelled"))
        except IpcError as exc:
            self._abort(exc)
        except Exception:
            _logger.exception("IPC connection failed unexpectedly")
            self._abort(ConnectionClosedError("the connection failed unexpectedly"))
        await self._release()

    async def _read_loop(self) -> None:
        limits = self._limits
        while True:
            try:
                payload = await read_frame(
                    self._reader,
                    max_bytes=limits.max_frame_bytes,
                    frame_timeout=limits.frame_timeout,
                    idle_timeout=limits.idle_timeout,
                )
            except OSError as exc:
                raise ConnectionClosedError(f"the connection was lost: {exc.strerror}") from None
            except IdleTimeoutError:
                if self._pending or self._inflight:
                    continue  # quiet, but a request is in progress: not idle
                raise
            if payload is None:
                raise ConnectionClosedError("the peer closed the connection")
            frame = decode_message(payload)
            match frame:
                case RequestFrame():
                    self._accept_request(frame)
                case ResponseFrame():
                    self._deliver(frame.id, frame.body)
                case ErrorFrame():
                    if frame.id is None:
                        raise RemoteError(frame.code, frame.message)
                    self._deliver_error(frame.id, RemoteError(frame.code, frame.message))
                case Hello():
                    raise ProtocolViolationError("the peer sent a hello after the handshake")
                case _:
                    assert_never(frame)

    def _deliver(self, request_id: IpcRequestId, body: ResponseBody) -> None:
        answer = self._pending.get(request_id)
        if answer is None or answer.done():
            _logger.debug("dropped an answer to no waiting request (%s)", request_id)
            return
        answer.set_result(body)

    def _deliver_error(self, request_id: IpcRequestId, error: RemoteError) -> None:
        answer = self._pending.get(request_id)
        if answer is None or answer.done():
            _logger.debug("dropped an error answering no waiting request (%s)", request_id)
            return
        answer.set_exception(error)

    def _accept_request(self, frame: RequestFrame) -> None:
        """Answer a request at once if it is a diagnostic or a refusal, else start its handler."""
        if frame.id in self._inflight:
            raise ProtocolViolationError("the peer reused a request id that is still in progress")
        if self._diagnostics:
            match frame.body:
                case Ping():
                    pong = Pong(kind="pong")
                    self._reply_now(ResponseFrame(kind="response", id=frame.id, body=pong))
                    return
                case Echo(text=text):
                    echoed = Echoed(kind="echoed", text=text)
                    self._reply_now(ResponseFrame(kind="response", id=frame.id, body=echoed))
                    return
        handler = self._handler
        if handler is None:
            self._reply_now(
                _error(frame.id, ErrorCode.UNSUPPORTED_REQUEST, "this peer serves no requests")
            )
            return
        if len(self._inflight) >= self._limits.max_inflight_handlers:
            self._reply_now(
                _error(
                    frame.id,
                    ErrorCode.TOO_MANY_REQUESTS,
                    f"{len(self._inflight)} requests are already in progress "
                    f"(limit {self._limits.max_inflight_handlers})",
                )
            )
            return
        self._inflight.add(frame.id)
        task = asyncio.get_running_loop().create_task(
            self._serve(frame, handler), name=f"ipc-request-{frame.id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, frame: RequestFrame, handler: RequestHandler) -> None:
        """Run the handler for one request and send what it answers.

        The request stays in progress, and counts against the cap, until the reply has been
        written (or the connection has ended), however this ends.
        """
        try:
            reply = await self._answer(frame, handler)
            await self._reply(frame.id, reply)
        finally:
            self._inflight.discard(frame.id)

    async def _answer(self, frame: RequestFrame, handler: RequestHandler) -> Frame:
        deadline = asyncio.timeout(self._limits.handler_timeout.total_seconds())
        try:
            async with deadline:
                body = await handler(frame.body, self)
            return ResponseFrame(kind="response", id=frame.id, body=body)
        except TimeoutError:
            if deadline.expired():
                return _error(frame.id, ErrorCode.TIMEOUT, "the request took too long")
            _logger.exception("IPC request handler failed")
        except RequestRefused as exc:
            return _error(frame.id, exc.code, exc.message)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # this very task is being cancelled: the connection is closing
            # The handler waited for something that was cancelled by someone else. The caller
            # is owed an answer all the same.
            _logger.exception("IPC request handler was cancelled from outside")
        except Exception:
            _logger.exception("IPC request handler failed")
        return _error(frame.id, ErrorCode.INTERNAL, _UNHANDLED)

    async def _reply(self, request_id: IpcRequestId, reply: Frame) -> None:
        """Send a reply. A send that fails ends the connection, never leaves a request dangling."""
        try:
            try:
                await self._send(reply)
            except (FrameTooLargeError, MalformedFrameError):
                _logger.warning("a response to request %s could not be sent", request_id)
                await self._send(
                    _error(request_id, ErrorCode.INTERNAL, "the response is unsendable")
                )
        except ConnectionClosedError:
            pass  # the peer is gone; `_release` has what is left to do
        except Exception:
            # The stream may hold half a frame, or the codec failed: nothing more can be trusted.
            _logger.exception("an IPC reply to request %s could not be sent", request_id)
            self._abort(ConnectionClosedError("a reply could not be sent"))

    async def _release(self) -> None:
        """Fail what waits on the connection, stop the handlers, say goodbye, close the stream."""
        try:
            reason = self._close_reason
            if reason is None:  # `_run` always sets one first
                reason = ConnectionClosedError("the connection ended")
                self._close_reason = reason
            _logger.log(
                logging.DEBUG if isinstance(reason, ConnectionClosedError) else logging.WARNING,
                "closing IPC connection: %s",
                reason,
            )
            if self._stall_timer is not None:
                self._stall_timer.cancel()
                self._stall_timer = None
            for answer in self._pending.values():
                if not answer.done():
                    failure = ConnectionClosedError(f"the connection closed: {reason}")
                    failure.__cause__ = reason
                    answer.set_exception(failure)
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                _done, stuck = await asyncio.wait(tasks, timeout=_HANDLER_UNWIND.total_seconds())
                for task in stuck:
                    _logger.warning(
                        "an IPC request handler did not stop within %g s of its connection "
                        "closing: %s",
                        _HANDLER_UNWIND.total_seconds(),
                        task.get_name(),
                    )
            goodbye = self._goodbye(reason)
            if goodbye is not None:
                await self._say_goodbye(goodbye)
            if self._stalled or self._blocked:
                # Nobody is reading, so a flush would wait for nothing; and senders stuck behind
                # the full buffer are woken only when the transport is gone.
                self._transport.abort()
            await close_writer(self._writer)
        finally:
            self._closed.set()

    @staticmethod
    def _goodbye(reason: IpcError) -> ErrorFrame | None:
        """The last frame to send the peer for this reason, if it was the peer's doing."""
        if isinstance(reason, FramingError | ProtocolViolationError):
            return _error(None, ErrorCode.PROTOCOL_VIOLATION, str(reason))
        if isinstance(reason, IdleTimeoutError):
            return _error(None, ErrorCode.TIMEOUT, str(reason))
        return None

    async def _say_goodbye(self, goodbye: ErrorFrame) -> None:
        if self.buffered_bytes > self._buffer_cap:
            return  # the peer is not reading: it would not get it
        try:
            data = encode_frame(encode_message(goodbye), max_bytes=self._limits.max_frame_bytes)
            await write_bytes(self._writer, data, within=_GOODBYE)
        except (TimeoutError, OSError, IpcError):
            pass  # best effort: the peer may be gone, or not reading
