"""The `[voice]` table of the configuration (`tumbleweed.agent.config` reads the file).

    [voice]
    enabled = true                          # false: no microphone button, no push-to-talk key
    stt_url = "http://127.0.0.1:8080"       # whisper-server (TUMBLEWEED_STT_URL); a VM: its host
    language = "auto"                       # or a code such as "en"
    edit_before_send = false                # true: the transcript waits in the input to be edited
                                            # (always, over plain http to another host)

    [voice.limits]
    max_utterance_seconds = 30              # recording stops by itself here (up to 120)
    request_timeout_seconds = 30            # the whole exchange with the speech server (up to 120)
    max_response_bytes = 65536              # most of the server's answer that is read

It holds no secret, and the URL is validated here (`SttEndpoint`: http or https, no credentials,
query or fragment) so that a bad one is refused when the file is read, with a message that does not
repeat it.
"""

from typing import Final, final

from pydantic import BaseModel, ConfigDict, field_validator

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.voice.endpoint import SttEndpoint
from tumbleweed.voice.limits import VoiceLimits
from tumbleweed.voice.stt import is_language_code

DEFAULT_STT_URL: Final = "http://127.0.0.1:8080"


@final
class VoiceConfig(BaseModel):
    """The `[voice]` table."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    enabled: bool = True
    stt_url: str = DEFAULT_STT_URL
    language: str = "auto"
    edit_before_send: bool = False
    limits: VoiceLimits = VoiceLimits()

    @field_validator("stt_url")
    @classmethod
    def _stt_url_is_safe(cls, value: str) -> str:
        try:
            SttEndpoint(value)
        except InvalidValueError as exc:
            raise ValueError(str(exc)) from None  # our own text: it never repeats the URL
        return value

    @field_validator("language")
    @classmethod
    def _language_is_a_code(cls, value: str) -> str:
        if not is_language_code(value):
            raise ValueError('language is "auto" or a two or three letter code such as "en"')
        return value

    @property
    def endpoint(self) -> SttEndpoint:
        return SttEndpoint(self.stt_url)
