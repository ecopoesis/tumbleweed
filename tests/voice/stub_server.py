"""A whisper-server stand-in on the loopback (no network beyond it): records what it is sent and
answers as a test scripts it, including as a hostile server would.

Every wait in a handler is on `stop`, which the fixture sets at teardown, so no handler thread
outlives its test or holds the run.
"""

import email.parser
import email.policy
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import final

JSON = {"Content-Type": "application/json"}


@final
@dataclass
class Reply:
    """How the stub answers: status, headers, then `body` (or each of `chunks`, `chunk_delay`
    apart). `delay` is waited before anything is sent. `length` False omits Content-Length."""

    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: dict(JSON))
    body: bytes = b'{"text": " hello world\\n"}'
    chunks: list[bytes] | None = None
    delay: float = 0.0
    chunk_delay: float = 0.0
    length: bool = True


@final
@dataclass(frozen=True)
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def parts(self) -> dict[str, bytes]:
        """The multipart form's fields by name (the file's bytes under "file")."""
        content_type = self.headers["content-type"].encode()
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            b"Content-Type: " + content_type + b"\r\n\r\n" + self.body
        )
        found: dict[str, bytes] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            assert isinstance(name, str)
            payload = part.get_payload(decode=True)
            assert isinstance(payload, bytes)
            found[name] = payload
        return found


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False  # a handler waiting on `stop` must not hold server_close


@final
class StubServer:
    def __init__(self) -> None:
        self.reply = Reply()
        self.requests: list[Recorded] = []
        self.stop = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                stub.requests.append(
                    Recorded(
                        "POST",
                        self.path,
                        {key.lower(): value for key, value in self.headers.items()},
                        body,
                    )
                )
                stub._answer(self)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self._server = _Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.stop.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _answer(self, handler: BaseHTTPRequestHandler) -> None:
        reply = self.reply
        try:
            if self.stop.wait(reply.delay):
                return
            handler.send_response(reply.status)
            chunks = reply.chunks if reply.chunks is not None else [reply.body]
            for name, value in reply.headers.items():
                handler.send_header(name, value)
            if reply.length:
                handler.send_header("Content-Length", str(sum(len(chunk) for chunk in chunks)))
            handler.end_headers()
            for chunk in chunks:
                handler.wfile.write(chunk)
                handler.wfile.flush()
                if reply.chunk_delay and self.stop.wait(reply.chunk_delay):
                    return
        except (BrokenPipeError, ConnectionResetError):
            return  # the client gave up: that is what several tests are about

    def wait_for_request(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while not self.requests and time.monotonic() < deadline:
            time.sleep(0.005)
        return bool(self.requests)
