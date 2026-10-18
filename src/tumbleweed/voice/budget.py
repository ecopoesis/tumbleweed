"""One time budget for the whole network exchange of a request, and a way to abandon it (I9).

The HTTP library's timeouts are per operation: each read that gets a byte starts again, so a server
that sends its status line one byte at a time keeps the request alive for as long as it likes.
This module is the network backend the speech client's transport uses, and it wraps every operation
of the exchange (name lookup, connecting, the TLS handshake, each write, each read) so that it is
cut to what is left of the request's deadline (`min(the library's timeout, time left)`) and fails
once there is none: a deadline of `request_timeout` is thereby the bound of the whole exchange,
whoever is slow. The only wait it cannot shorten is the one inside the operating system's resolver
once it has been asked: the lookup runs on a throwaway daemon thread that the request stops waiting
for at the deadline (and which ends when the resolver does), and an IP address needs none.

Abandoning (`AbortSignal`, set when the user presses Esc or closes the shell) shuts down every
socket the request has open, which wakes a thread blocked in a read or a write at once; a request
whose signal is already set fails before it connects.

The class is the library's backend interface (`httpcore2`, the HTTP library's own transport, which
`httpx2` requires), written against its documented `NetworkBackend` and `NetworkStream`.
"""

import contextlib
import socket
import ssl
import threading
import time
from collections.abc import Iterable
from datetime import timedelta
from typing import Final, final

import httpcore2

from tumbleweed.voice.stt import AbortSignal

_SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)
_NO_TIME: Final = "the request's time is used up"


@final
class RequestBudget:
    """The deadline of one request and its abort signal."""

    def __init__(self, timeout: timedelta, abort: AbortSignal) -> None:
        self.deadline = time.monotonic() + timeout.total_seconds()
        self.abort = abort

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def bound(self, per_operation: float | None) -> float | None:
        """`per_operation` cut to what is left. Raises `ValueError` if nothing is: the caller
        turns it into the timeout error of its own operation; and for an abort."""
        left = self.remaining()
        if self.abort.is_set or left <= 0:
            raise ValueError(_NO_TIME)
        return left if per_operation is None else min(per_operation, left)


def _resolve(host: str, port: int, budget: RequestBudget) -> str:
    """The address `host` names, found within the budget. Raises `TimeoutError`."""
    try:
        socket.inet_pton(socket.AF_INET, host)
        return host
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return host
    except OSError:
        pass
    found: list[str] = []
    done = threading.Event()

    def look_up() -> None:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            found.append(str(infos[0][4][0]))
        except (OSError, IndexError):
            pass
        done.set()

    threading.Thread(target=look_up, name="tumbleweed-dns", daemon=True).start()
    left = budget.bound(None)
    if not done.wait(timeout=left) or not found:
        raise TimeoutError(host)  # no answer in time, or none at all: the connect fails either way
    return found[0]


@final
class _BudgetStream(httpcore2.NetworkStream):
    def __init__(self, inner: httpcore2.NetworkStream, owner: "BudgetBackend") -> None:
        self._inner = inner
        self._owner = owner

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            bounded = self._owner.budget().bound(timeout)
        except ValueError:
            raise httpcore2.ReadTimeout(_NO_TIME) from None
        return self._inner.read(max_bytes, bounded)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        try:
            bounded = self._owner.budget().bound(timeout)
        except ValueError:
            raise httpcore2.WriteTimeout(_NO_TIME) from None
        self._inner.write(buffer, bounded)

    def close(self) -> None:
        self._owner.forget(self)
        self._inner.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.NetworkStream:
        try:
            bounded = self._owner.budget().bound(timeout)
        except ValueError:
            raise httpcore2.ConnectTimeout(_NO_TIME) from None
        wrapped = _BudgetStream(
            self._inner.start_tls(ssl_context, server_hostname, bounded), self._owner
        )
        self._owner.forget(self)
        self._owner.track(wrapped)
        return wrapped

    def get_extra_info(self, info: str) -> object:
        return self._inner.get_extra_info(info)

    def shut_down(self) -> None:
        sock = self._inner.get_extra_info("socket")
        if isinstance(sock, socket.socket):
            # Already closed or never connected: then nothing is blocked on it.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


@final
class BudgetBackend(httpcore2.NetworkBackend):
    """The network backend of the speech client: one `RequestBudget` at a time (`begin`/`end`)."""

    def __init__(self) -> None:
        self._inner = httpcore2.SyncBackend()
        self._lock = threading.Lock()
        self._budget: RequestBudget | None = None
        self._streams: list[_BudgetStream] = []

    def begin(self, timeout: timedelta, abort: AbortSignal) -> RequestBudget:
        budget = RequestBudget(timeout, abort)
        with self._lock:
            self._budget = budget
        abort.on_set(self.abort_all)
        return budget

    def end(self, budget: RequestBudget) -> None:
        budget.abort.remove(self.abort_all)
        with self._lock:
            if self._budget is budget:
                self._budget = None

    def budget(self) -> RequestBudget:
        with self._lock:
            if self._budget is None:
                raise httpcore2.ConnectError("there is no request to give the network to")
            return self._budget

    def track(self, stream: _BudgetStream) -> None:
        with self._lock:
            self._streams.append(stream)

    def forget(self, stream: _BudgetStream) -> None:
        with self._lock:
            if stream in self._streams:
                self._streams.remove(stream)

    def abort_all(self) -> None:
        """Shut down every socket the request has open; whatever blocks on one wakes up."""
        with self._lock:
            streams = list(self._streams)
        for stream in streams:
            stream.shut_down()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[_SocketOption] | None = None,
    ) -> httpcore2.NetworkStream:
        budget = self.budget()
        try:
            address = _resolve(host, port, budget)
            bounded = budget.bound(timeout)
        except (ValueError, TimeoutError):
            raise httpcore2.ConnectTimeout(_NO_TIME) from None
        stream = _BudgetStream(
            self._inner.connect_tcp(address, port, bounded, local_address, socket_options), self
        )
        self.track(stream)
        return stream

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[_SocketOption] | None = None,
    ) -> httpcore2.NetworkStream:
        raise httpcore2.ConnectError("unix sockets are not used")

    def sleep(self, seconds: float) -> None:
        time.sleep(min(seconds, 1.0))
