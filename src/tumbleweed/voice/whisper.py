"""`WhisperServerBackend`: an `SttBackend` over HTTP, for whisper.cpp's `whisper-server`.

The server (`scripts/whisper-server`, README) runs natively, with Metal on a Mac, and takes
`POST /inference` with a multipart form: `file` (a WAV), `response_format=json`, `language`. It
answers `{"text": "..."}`. The URL is configurable (`SttEndpoint`), so a VM can use its host.

HTTP library: `httpx2`, the one the Anthropic SDK already brings (DECISIONS D3, D20, #43), so the
project carries one HTTP stack. It is a *synchronous* client because `transcribe` runs on the voice
worker thread (`VoiceInput`), never on the GTK main thread.

What a server can make us do is bounded (I9), on the bytes as they arrive:

- the request timeout bounds every wait of the library (connect, write, each read), and a deadline
  of the same length for the whole request is checked as each piece of the answer arrives, so a
  server that sends a byte at a time cannot hold the worker for long: at most the timeout, plus
  one more wait of at most the timeout for the piece it was in the middle of;
- the body is read with `iter_raw` (before any decoding) and counted, at most `max_response_bytes`;
  it is asked for with `Accept-Encoding: identity` and a response with any other encoding is refused
  without reading it, because a decompressing layer would make the count meaningless;
- redirects are not followed, and the environment's proxy variables are ignored (`trust_env`
  False): the audio goes where the config says and nowhere else;
- the answer must be `application/json` with a `text` string; it is parsed from bytes by a strict
  pydantic model (invalid UTF-8 or a lone surrogate is refused), and what it says is untrusted text
  (`stt.clean_transcript`).

Failures become `SttError`s with fixed messages. A server's own words (its `error` field, a status
line) are never kept, shown or logged, and neither is the library's exception text, which can name
headers: only the status code, the exception's type and the endpoint's `display` form (no path, no
credentials) are.
"""

import io
import logging
import time
import wave
from typing import Final, final

import httpx2
from pydantic import BaseModel, ConfigDict, ValidationError

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.endpoint import SttEndpoint
from tumbleweed.voice.errors import SttResponseError, SttUnavailableError
from tumbleweed.voice.limits import BYTES_PER_SAMPLE, CHANNELS, SAMPLE_RATE_HZ, VoiceLimits
from tumbleweed.voice.stt import is_language_code

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT: Final = 5.0
POOL_TIMEOUT: Final = 5.0
_IDENTITY_ENCODINGS = frozenset({"", "identity"})


@final
class _WhisperAnswer(BaseModel):
    """The one field of the answer we use.

    `extra="ignore"`, unlike our other boundary models, on purpose: the answer is the server's, not
    ours, and a newer whisper-server that adds a field must not break dictation. The field we read
    is strict, and nothing else is looked at.
    """

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    text: str


def wav_bytes(utterance: Utterance) -> bytes:
    """`utterance` as a WAV file, in memory: the one place audio is wrapped for the server."""
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(BYTES_PER_SAMPLE)
        wav.setframerate(SAMPLE_RATE_HZ)
        wav.writeframes(utterance.pcm)
    return out.getvalue()


def build_http_client(
    limits: VoiceLimits, *, transport: httpx2.BaseTransport | None = None
) -> httpx2.Client:
    """The HTTP client, as the module docstring describes it. `transport` replaces the network."""
    seconds = limits.request_timeout.total_seconds()
    return httpx2.Client(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx2.Timeout(
            connect=CONNECT_TIMEOUT, read=seconds, write=seconds, pool=POOL_TIMEOUT
        ),
        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
    )


@final
class WhisperServerBackend:
    """Posts utterances to a `whisper-server` and returns the raw text it answers with."""

    def __init__(
        self,
        endpoint: SttEndpoint,
        *,
        language: str,
        limits: VoiceLimits,
        client: httpx2.Client | None = None,
    ) -> None:
        raw: object = endpoint
        if not isinstance(raw, SttEndpoint):
            raise InvalidValueError("the speech backend needs an SttEndpoint")
        if not is_language_code(language):
            raise InvalidValueError('the language is "auto" or a two or three letter code')
        self._endpoint = endpoint
        self._language = language
        self._limits = limits
        self._client = client if client is not None else build_http_client(limits)
        if endpoint.sends_audio_in_clear:
            logger.warning(
                "audio is sent to %s over plain http: anyone on that network can listen",
                endpoint.display,
            )

    def transcribe(self, utterance: Utterance) -> str:
        deadline = time.monotonic() + self._limits.request_timeout.total_seconds()
        files = {"file": ("utterance.wav", wav_bytes(utterance), "audio/wav")}
        data = {"response_format": "json", "temperature": "0.0", "language": self._language}
        try:
            request = self._client.build_request(
                "POST", self._endpoint.inference_url, files=files, data=data
            )
            # `send(stream=True)`: the body is read by `_read_body`, counted, not buffered by the
            # library. (Not the client's `.stream()`: tests/agent/test_chokepoint.py reserves that
            # name for model calls.)
            response = self._client.send(request, stream=True)
            try:
                body = self._read_body(response, deadline)
            finally:
                response.close()
        except httpx2.TimeoutException:
            raise SttUnavailableError("The speech server did not answer in time.") from None
        except httpx2.TransportError as exc:
            logger.warning("speech server %s: %s", self._endpoint.display, type(exc).__name__)
            raise SttUnavailableError(
                f"The speech server at {self._endpoint.display} could not be reached."
            ) from None
        except httpx2.HTTPError as exc:  # a decoding error, a stream that broke: nothing to show
            logger.warning("speech server %s: %s", self._endpoint.display, type(exc).__name__)
            raise SttResponseError("The speech server's answer could not be read.") from None
        return self._parse(body)

    def _read_body(self, response: httpx2.Response, deadline: float) -> bytes:
        limit = self._limits.max_response_bytes
        if response.status_code != 200:
            # Redirects included: they are not followed. The body is not read, and not shown.
            raise SttResponseError(
                f"The speech server answered with status {response.status_code}."
            )
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in _IDENTITY_ENCODINGS:
            raise SttResponseError("The speech server's answer uses an encoding that is refused.")
        media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise SttResponseError("The speech server's answer is not JSON.")
        declared = response.headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > limit:
            raise SttResponseError("The speech server's answer is larger than allowed.")
        body = bytearray()
        for chunk in response.iter_raw():  # as they arrive: a chunk size would batch a slow drip
            if time.monotonic() > deadline:
                raise SttUnavailableError("The speech server did not answer in time.")
            body += chunk
            if len(body) > limit:
                raise SttResponseError("The speech server's answer is larger than allowed.")
        return bytes(body)

    @staticmethod
    def _parse(body: bytes) -> str:
        try:
            answer = _WhisperAnswer.model_validate_json(body)
        except ValidationError:
            raise SttResponseError("The speech server's answer was not understood.") from None
        return answer.text

    def close(self) -> None:
        try:
            self._client.close()
        except httpx2.HTTPError as exc:
            logger.warning("closing the speech client: %s", type(exc).__name__)
