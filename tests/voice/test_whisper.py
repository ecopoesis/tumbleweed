"""`WhisperServerBackend` against a local stub server: the request it makes, the answers it
accepts, and every way a server can try to make it read too much, wait too long or show something
it said (I9, I6). No network beyond the loopback.
"""

import gzip
import io
import logging
import socket
import threading
import time
import wave
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Protocol

import httpcore2
import httpx2
import pytest

from tests.voice.stub_server import JSON, DripServer, Reply, StubServer
from tests.voice.support import limits, max_bytes, pcm
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.voice import budget
from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.endpoint import SttEndpoint
from tumbleweed.voice.errors import SttCancelledError, SttResponseError, SttUnavailableError
from tumbleweed.voice.limits import VoiceLimits
from tumbleweed.voice.stt import AbortSignal
from tumbleweed.voice.whisper import WhisperServerBackend, build_http_client


def utterance(seconds: float = 1.0) -> Utterance:
    return Utterance(pcm(seconds), max_bytes=max_bytes(30))


class Make(Protocol):
    def __call__(
        self,
        *,
        url: str | None = None,
        language: str = "auto",
        voice_limits: VoiceLimits | None = None,
    ) -> WhisperServerBackend: ...


@pytest.fixture
def make(stub: StubServer) -> Iterator[Make]:
    made: list[WhisperServerBackend] = []

    def build(
        *, url: str | None = None, language: str = "auto", voice_limits: VoiceLimits | None = None
    ) -> WhisperServerBackend:
        backend = WhisperServerBackend(
            SttEndpoint(url or stub.url),
            language=language,
            limits=voice_limits or limits(request_timeout_seconds=5),
        )
        made.append(backend)
        return backend

    yield build
    for backend in made:
        backend.close()


class TestTheRequest:
    def test_the_utterance_is_posted_as_a_wav_to_the_inference_path(
        self, stub: StubServer, make: Make
    ) -> None:
        backend = make(language="en")

        assert backend.transcribe(utterance(0.5)) == " hello world\n"

        (request,) = stub.requests
        assert (request.method, request.path) == ("POST", "/inference")
        parts = request.parts()
        assert parts["response_format"] == b"json"
        assert parts["language"] == b"en"
        with wave.open(io.BytesIO(parts["file"])) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
            assert wav.readframes(wav.getnframes()) == pcm(0.5)

    def test_a_path_prefix_in_the_url_is_kept(self, stub: StubServer, make: Make) -> None:
        make(url=stub.url + "/stt/").transcribe(utterance())

        assert stub.requests[0].path == "/stt/inference"

    def test_it_asks_for_no_compression_and_names_itself_plainly(
        self, stub: StubServer, make: Make
    ) -> None:
        make().transcribe(utterance())

        headers = stub.requests[0].headers
        assert headers["accept-encoding"] == "identity"
        assert headers["accept"] == "application/json"
        assert "authorization" not in headers
        assert "cookie" not in headers

    def test_the_environments_proxy_is_not_used(
        self, stub: StubServer, make: Make, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audio goes where the config says, whatever HTTP_PROXY says."""
        proxy = StubServer()
        proxy.start()
        try:
            monkeypatch.setenv("HTTP_PROXY", proxy.url)
            monkeypatch.setenv("http_proxy", proxy.url)
            monkeypatch.delenv("NO_PROXY", raising=False)
            monkeypatch.delenv("no_proxy", raising=False)

            make().transcribe(utterance())

            assert len(stub.requests) == 1
            assert proxy.requests == []
        finally:
            proxy.close()

    @pytest.mark.parametrize("language", ["", "EN", "english", "e", "auto ", "../x", "e\nn"])
    def test_a_language_that_is_not_a_code_is_refused(
        self, stub: StubServer, language: str
    ) -> None:
        with pytest.raises(InvalidValueError, match="language"):
            WhisperServerBackend(SttEndpoint(stub.url), language=language, limits=limits())

    def test_only_an_endpoint_is_accepted(self) -> None:
        with pytest.raises(InvalidValueError, match="SttEndpoint"):
            WhisperServerBackend(
                "http://127.0.0.1:8080",  # type: ignore[arg-type]  # the point
                language="auto",
                limits=limits(),
            )


class TestTheAnswer:
    def test_unknown_fields_of_a_newer_server_are_ignored(
        self, stub: StubServer, make: Make
    ) -> None:
        stub.reply = Reply(body=b'{"text": "hi", "language": "en", "duration": 1.5}')

        assert make().transcribe(utterance()) == "hi"

    def test_unicode_text_comes_back_intact(self, stub: StubServer, make: Make) -> None:
        stub.reply = Reply(body='{"text": "caf\\u00e9 日本語"}'.encode())

        assert make().transcribe(utterance()) == "café 日本語"

    @pytest.mark.parametrize(
        "body",
        [
            b"",
            b"not json",
            b"[]",
            b'"text"',
            b"{}",
            b'{"text": null}',
            b'{"text": 5}',
            b'{"text": ["a"]}',
            b'{"text": "ok"',
            b'\xff\xfe{"text": "x"}',
            b'{"text": "caf\xe9"}',  # Latin-1 byte in what must be UTF-8
            b'{"text": "\\ud800"}',  # a lone surrogate, as JSON can say it
        ],
        ids=lambda body: body[:20].decode("latin-1"),
    )
    def test_an_answer_that_is_not_the_strict_shape_is_refused(
        self, stub: StubServer, make: Make, body: bytes
    ) -> None:
        stub.reply = Reply(body=body)

        with pytest.raises(SttResponseError, match="not understood"):
            make().transcribe(utterance())

    def test_a_wrong_content_type_is_refused(self, stub: StubServer, make: Make) -> None:
        stub.reply = Reply(headers={"Content-Type": "text/html"}, body=b'{"text": "hi"}')

        with pytest.raises(SttResponseError, match="not JSON"):
            make().transcribe(utterance())

    def test_json_with_a_charset_parameter_is_accepted(self, stub: StubServer, make: Make) -> None:
        stub.reply = Reply(headers={"Content-Type": "application/json; charset=utf-8"})

        assert make().transcribe(utterance()) == " hello world\n"

    @pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
    def test_an_error_status_fails_with_fixed_words_never_the_servers(
        self, stub: StubServer, make: Make, status: int
    ) -> None:
        stub.reply = Reply(
            status=status, body=b'{"error": "SECRET-TOKEN-xyz leaked from the server"}'
        )

        with pytest.raises(SttResponseError) as raised:
            make().transcribe(utterance())

        assert str(raised.value) == f"The speech server answered with status {status}."
        assert "SECRET" not in str(raised.value)

    def test_a_redirect_is_not_followed(self, stub: StubServer, make: Make) -> None:
        elsewhere = StubServer()
        elsewhere.start()
        try:
            stub.reply = Reply(
                status=307, headers={"Location": elsewhere.url + "/inference"}, body=b""
            )

            with pytest.raises(SttResponseError, match="status 307"):
                make().transcribe(utterance())

            assert elsewhere.requests == []  # the audio was not sent on to where it pointed
        finally:
            elsewhere.close()


class TestSizeLimits:
    def test_an_answer_of_exactly_the_limit_is_read(self, stub: StubServer, make: Make) -> None:
        body = b'{"text": "' + b"a" * 1500 + b'"}'
        stub.reply = Reply(body=body)

        text = make(voice_limits=limits(max_response_bytes=len(body))).transcribe(utterance())

        assert text == "a" * 1500

    def test_an_answer_one_byte_over_the_declared_limit_is_refused_before_reading(
        self, stub: StubServer, make: Make
    ) -> None:
        body = b'{"text": "' + b"a" * 2000 + b'"}'
        stub.reply = Reply(body=body)

        with pytest.raises(SttResponseError, match="larger than allowed"):
            make(voice_limits=limits(max_response_bytes=len(body) - 1)).transcribe(utterance())

    def test_an_answer_that_does_not_say_its_size_is_counted_as_it_arrives(
        self, stub: StubServer, make: Make
    ) -> None:
        chunks = [b'{"text": "'] + [b"a" * 1024] * 20 + [b'"}']
        stub.reply = Reply(chunks=chunks, length=False)
        stub.reply.headers["Connection"] = "close"

        with pytest.raises(SttResponseError, match="larger than allowed"):
            make(voice_limits=limits(max_response_bytes=4096)).transcribe(utterance())

    def test_a_lying_content_length_does_not_get_past_the_count(
        self, stub: StubServer, make: Make
    ) -> None:
        stub.reply = Reply(
            chunks=[b"x" * 8192], length=False, headers={**JSON, "Content-Length": "10"}
        )

        # httpx stops at the declared length or the server's data, whichever the protocol says;
        # either way nothing over the limit is accepted, and what is read is not JSON.
        with pytest.raises((SttResponseError, SttUnavailableError)):
            make(voice_limits=limits(max_response_bytes=1024)).transcribe(utterance())

    @pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "zstd", "gzip, gzip", "x"])
    def test_a_compressed_answer_is_refused_unread(
        self, stub: StubServer, make: Make, encoding: str
    ) -> None:
        stub.reply = Reply(
            headers={**JSON, "Content-Encoding": encoding}, body=gzip.compress(b'{"text": "hi"}')
        )

        with pytest.raises(SttResponseError, match="encoding that is refused"):
            make().transcribe(utterance())

    def test_identity_encoding_is_fine(self, stub: StubServer, make: Make) -> None:
        stub.reply = Reply(headers={**JSON, "Content-Encoding": "identity"})

        assert make().transcribe(utterance()) == " hello world\n"


class TestWaiting:
    def test_a_server_that_never_answers_times_out(self, stub: StubServer, make: Make) -> None:
        stub.reply = Reply(delay=30.0)
        backend = make(voice_limits=limits(request_timeout_seconds=1))
        started = time.monotonic()

        with pytest.raises(SttUnavailableError, match="did not answer in time"):
            backend.transcribe(utterance())

        assert time.monotonic() - started < 4

    def test_a_server_that_drips_bytes_is_cut_off_by_the_total_deadline(
        self, stub: StubServer, make: Make
    ) -> None:
        """Each byte arrives inside the read timeout, so only the deadline stops it."""
        body = b'{"text": "' + b"a" * 60 + b'"}'
        stub.reply = Reply(chunks=[bytes([b]) for b in body], chunk_delay=0.25, length=False)
        stub.reply.headers["Connection"] = "close"
        backend = make(voice_limits=limits(request_timeout_seconds=1))
        started = time.monotonic()

        with pytest.raises(SttUnavailableError, match="did not answer in time"):
            backend.transcribe(utterance())

        assert time.monotonic() - started < 4

    def test_a_refused_connection_names_the_server_without_its_path(
        self, make: Make, caplog: pytest.LogCaptureFixture
    ) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]  # closed again: nothing listens there
        backend = make(url=f"http://127.0.0.1:{port}/private-prefix")

        with caplog.at_level(logging.DEBUG), pytest.raises(SttUnavailableError) as raised:
            backend.transcribe(utterance())

        assert (
            str(raised.value)
            == f"The speech server at http://127.0.0.1:{port} could not be reached."
        )
        assert "private-prefix" not in caplog.text + str(raised.value)

    def test_a_server_that_hangs_up_is_an_error_not_a_crash(
        self, stub: StubServer, make: Make
    ) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            backend = make(url=f"http://127.0.0.1:{port}")

            import threading

            def accept_and_close() -> None:
                connection, _ = listener.accept()
                connection.close()

            thread = threading.Thread(target=accept_and_close, daemon=True)
            thread.start()
            with pytest.raises((SttUnavailableError, SttResponseError)):
                backend.transcribe(utterance())
            thread.join(timeout=5)


class TestWhatIsLogged:
    def test_plain_http_to_another_host_warns_once_without_the_path(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            WhisperServerBackend(
                SttEndpoint("http://10.0.0.5:8080/secret-path"), language="auto", limits=limits()
            ).close()

        assert "http://10.0.0.5:8080" in caplog.text
        assert "plain http" in caplog.text
        assert "secret-path" not in caplog.text

    def test_the_loopback_and_https_do_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            for url in ("http://127.0.0.1:8080", "http://localhost:8080", "https://10.0.0.5"):
                WhisperServerBackend(SttEndpoint(url), language="auto", limits=limits()).close()

        assert caplog.text == ""


class TestTheWholeExchangeIsBounded:
    """`request_timeout` bounds the whole exchange, whoever is slow: the library's timeouts apply
    to each operation, and a byte that arrives starts a new one."""

    def test_a_server_that_reads_a_few_bytes_at_a_time_cannot_hold_the_worker(
        self, make: Make, stub: StubServer
    ) -> None:
        stub.reply = Reply(delay=0.0)
        stub.read_slowly = 0.05  # seconds per 512 bytes of the request, far under what it needs
        backend = make(voice_limits=limits(request_timeout_seconds=1))
        big = Utterance(pcm(30), max_bytes=max_bytes(30))
        started = time.monotonic()

        with pytest.raises(SttUnavailableError, match="did not answer in time"):
            backend.transcribe(big)

        assert time.monotonic() - started < 4


class TestAHostileContentLength:
    @pytest.mark.parametrize("length", ["9" * 5000, "-1", "1e9", " 10 ", "١٢"])
    def test_a_length_that_is_not_a_sane_number_is_an_error_not_a_crash(
        self, stub: StubServer, make: Make, length: str
    ) -> None:
        stub.reply = Reply(headers={**JSON, "Content-Length": length}, length=False)

        with pytest.raises((SttResponseError, SttUnavailableError)):
            make().transcribe(utterance())


class TestWithoutANetwork:
    """The same client over a transport of the test's own, for what a real socket cannot show."""

    @staticmethod
    def backend(transport: httpx2.BaseTransport, seconds: int = 1) -> WhisperServerBackend:
        voice_limits = limits(request_timeout_seconds=seconds)
        return WhisperServerBackend(
            SttEndpoint("http://127.0.0.1:1"),
            language="auto",
            limits=voice_limits,
            client=build_http_client(voice_limits, transport=transport),
        )

    def test_a_content_length_of_thousands_of_digits_is_refused_not_parsed(self) -> None:
        class Hostile(httpx2.BaseTransport):
            def handle_request(self, request: httpx2.Request) -> httpx2.Response:
                request.read()
                return httpx2.Response(
                    200,
                    headers={**JSON, "Content-Length": "9" * 5000},
                    content=b'{"text": "x"}',
                )

        with pytest.raises(SttResponseError, match="larger than allowed"):
            self.backend(Hostile()).transcribe(utterance())

    def test_a_good_answer_over_the_same_transport_is_read(self) -> None:
        class Good(httpx2.BaseTransport):
            def handle_request(self, request: httpx2.Request) -> httpx2.Response:
                request.read()
                return httpx2.Response(
                    200, headers=JSON, stream=httpx2.ByteStream(b'{"text": "fine"}')
                )

        assert self.backend(Good()).transcribe(utterance()) == "fine"


class TestADrippingServer:
    @pytest.mark.parametrize("what", ["status line and headers", "body"])
    def test_a_byte_at_a_time_is_cut_off_at_the_deadline(self, what: str) -> None:
        body = b'{"text": "' + b"a" * 40 + b'"}'
        head = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        server = DripServer(head + body, drip_from=0 if what != "body" else len(head))
        server.start()
        try:
            backend = WhisperServerBackend(
                SttEndpoint(server.url), language="auto", limits=limits(request_timeout_seconds=1)
            )
            started = time.monotonic()

            with pytest.raises(SttUnavailableError, match="did not answer in time"):
                backend.transcribe(utterance())

            # One header byte every 0.2 s would take 10 s for the head and 8 s for the body.
            assert time.monotonic() - started < 2.5
            backend.close()
        finally:
            server.close()

    def test_a_wait_that_would_end_after_the_deadline_is_cut_to_it(self) -> None:
        """One byte at 0.7 s, the next at 1.4 s: the second read starts with 0.3 s left, though the
        library would let it wait the whole timeout."""
        server = DripServer(b"HT", drip_from=0, interval=0.7)
        server.start()
        try:
            backend = WhisperServerBackend(
                SttEndpoint(server.url), language="auto", limits=limits(request_timeout_seconds=1)
            )
            started = time.monotonic()

            with pytest.raises(SttUnavailableError, match="did not answer in time"):
                backend.transcribe(utterance())

            assert time.monotonic() - started < 1.35
            backend.close()
        finally:
            server.close()

    def test_a_name_that_never_resolves_is_cut_off_at_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = threading.Event()

        def stuck(*args: object, **kwargs: object) -> list[object]:
            release.wait(timeout=10)
            raise OSError("never")

        monkeypatch.setattr(socket, "getaddrinfo", stuck)
        backend = WhisperServerBackend(
            SttEndpoint("http://stt.invalid:8080"),
            language="auto",
            limits=limits(request_timeout_seconds=1),
        )
        started = time.monotonic()
        try:
            with pytest.raises(SttUnavailableError, match="did not answer in time"):
                backend.transcribe(utterance())

            assert time.monotonic() - started < 2.5
        finally:
            release.set()
            backend.close()

    @pytest.mark.parametrize("address", ["127.0.0.1", "10.0.2.2", "::1", "fe80::1"])
    def test_an_address_needs_no_lookup(
        self, address: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def never(*args: object, **kwargs: object) -> list[object]:
            raise AssertionError("looked up an IP address")

        monkeypatch.setattr(socket, "getaddrinfo", never)
        spent = budget.RequestBudget(timedelta(seconds=5), AbortSignal())

        assert budget._resolve(address, 80, spent) == address  # noqa: SLF001


class TestAbandoning:
    def test_a_request_waiting_for_the_server_stops_at_once(
        self, stub: StubServer, make: Make
    ) -> None:
        stub.reply = Reply(delay=30.0)
        backend = make(voice_limits=limits(request_timeout_seconds=30))
        signal = AbortSignal()
        threading.Timer(0.3, signal.set).start()
        started = time.monotonic()

        with pytest.raises(SttCancelledError):
            backend.transcribe(utterance(), signal)

        assert time.monotonic() - started < 3

    def test_a_request_dripping_its_answer_stops_at_once_too(self, make: Make) -> None:
        server = DripServer(b"HTTP/1.1 200 OK\r\n" + b"X-Pad: " + b"a" * 2000, drip_from=0)
        server.start()
        try:
            backend = WhisperServerBackend(
                SttEndpoint(server.url), language="auto", limits=limits(request_timeout_seconds=30)
            )
            signal = AbortSignal()
            threading.Timer(0.3, signal.set).start()
            started = time.monotonic()

            with pytest.raises(SttCancelledError):
                backend.transcribe(utterance(), signal)

            assert time.monotonic() - started < 3
            backend.close()
        finally:
            server.close()

    def test_a_signal_that_is_already_set_sends_nothing(self, stub: StubServer, make: Make) -> None:
        signal = AbortSignal()
        signal.set()

        with pytest.raises(SttCancelledError):
            make().transcribe(utterance(), signal)

        assert stub.requests == []

    def test_an_abort_does_not_spoil_the_next_request(self, stub: StubServer, make: Make) -> None:
        backend = make()
        signal = AbortSignal()
        signal.set()
        with pytest.raises(SttCancelledError):
            backend.transcribe(utterance(), signal)

        assert backend.transcribe(utterance(), AbortSignal()) == " hello world\n"


class TestConnectionsAreNotLeaked:
    def test_more_failures_than_the_pool_holds_are_followed_by_a_success(
        self, stub: StubServer, make: Make
    ) -> None:
        """A refused response is closed: a pool of a hundred would be full after a hundred."""
        backend = make(voice_limits=limits(request_timeout_seconds=5))
        stub.reply = Reply(status=500, body=b"no")
        for _ in range(120):
            with pytest.raises(SttResponseError):
                backend.transcribe(utterance(0.1))
        stub.reply = Reply()

        assert backend.transcribe(utterance()) == " hello world\n"


class TestTheBudget:
    def test_it_cuts_an_operations_time_to_what_is_left(self) -> None:
        budget_ = budget.RequestBudget(timedelta(seconds=10), AbortSignal())

        assert budget_.bound(None) is not None
        assert 9 < (budget_.bound(None) or 0) <= 10
        assert budget_.bound(2.0) == 2.0

    def test_nothing_is_left_after_the_deadline_or_an_abort(self) -> None:
        late = budget.RequestBudget(timedelta(seconds=0), AbortSignal())
        signal = AbortSignal()
        early = budget.RequestBudget(timedelta(seconds=10), signal)
        signal.set()

        with pytest.raises(ValueError, match="used up"):
            late.bound(1.0)
        with pytest.raises(ValueError, match="used up"):
            early.bound(1.0)


class TestTls:
    """The handshake and the reads and writes after it run through the budget too."""

    @staticmethod
    def certificate(directory: Path) -> tuple[Path, Path]:
        import shutil
        import subprocess

        openssl = shutil.which("openssl")
        if openssl is None:
            pytest.skip("openssl makes the test certificate; there is none on this host")
        key, cert = directory / "key.pem", directory / "cert.pem"
        subprocess.run(  # noqa: S603  (argv list, no shell, a program found on PATH)
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return key, cert

    def test_a_tls_exchange_works_and_a_silent_peer_is_cut_off_at_the_deadline(
        self, tmp_path: Path
    ) -> None:
        import ssl

        key, cert = self.certificate(tmp_path)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        done = threading.Event()

        def serve() -> None:
            for index in range(2):
                connection, _ = listener.accept()
                with server_context.wrap_socket(connection, server_side=True) as tls:
                    if index == 0:
                        tls.recv(1)
                        tls.sendall(b"y")
                    else:
                        done.wait(timeout=10)  # says nothing

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        client_context = ssl.create_default_context(cafile=str(cert))
        network = budget.BudgetBackend()
        port = listener.getsockname()[1]
        try:
            network.begin(timedelta(seconds=1), AbortSignal())
            stream = network.connect_tcp("127.0.0.1", port, 5.0)
            secure = stream.start_tls(client_context, "localhost", 5.0)
            secure.write(b"x", 5.0)
            assert secure.read(1, 5.0) == b"y"
            secure.close()

            second = network.connect_tcp("127.0.0.1", port, 5.0).start_tls(
                client_context, "localhost", 5.0
            )
            started = time.monotonic()
            with pytest.raises(httpcore2.ReadTimeout):
                second.read(1, 30.0)  # the library would wait 30 s
            assert time.monotonic() - started < 2
            second.close()
        finally:
            done.set()
            listener.close()
            thread.join(timeout=5)
