"""`WhisperServerBackend` against a local stub server: the request it makes, the answers it
accepts, and every way a server can try to make it read too much, wait too long or show something
it said (I9, I6). No network beyond the loopback.
"""

import gzip
import io
import logging
import socket
import time
import wave
from collections.abc import Iterator
from typing import Protocol

import pytest

from tests.voice.stub_server import JSON, Reply, StubServer
from tests.voice.support import limits, max_bytes, pcm
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.endpoint import SttEndpoint
from tumbleweed.voice.errors import SttResponseError, SttUnavailableError
from tumbleweed.voice.limits import VoiceLimits
from tumbleweed.voice.whisper import WhisperServerBackend


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
