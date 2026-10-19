"""The `[voice]` table of the configuration (`tumbleweed.agent.config` reads the file).

    [voice]
    enabled = true                          # false: no microphone button, no push-to-talk key,
                                            # and no speech output either (it needs voice input)
    stt_url = "http://127.0.0.1:8080"       # whisper-server (TUMBLEWEED_STT_URL); a VM: its host
    language = "auto"                       # or a code such as "en"
    edit_before_send = false                # true: the transcript waits in the input to be edited
                                            # (always, over plain http to another host)

    [voice.limits]
    max_utterance_seconds = 30              # recording stops by itself here (up to 120)
    request_timeout_seconds = 30            # the whole exchange with the speech server (up to 120)
    max_response_bytes = 65536              # most of the server's answer that is read

    [voice.tts]                             # speech output (S07); off until both paths are set
    enabled = true
    piper_path = "/usr/local/bin/piper"     # absolute: the piper program, run with an argv list
    voice_model = "/home/me/.local/share/tumbleweed/piper/en_US-lessac.onnx"
    sample_rate_hz = 22050                  # the voice model's (its .onnx.json says): 8000 to 48000
    max_sentence_chars = 400                # one synthesis request, at most 1000
    max_reply_chars = 4000                  # most of one reply that is spoken, up to 20000
    synthesis_timeout_seconds = 20          # per sentence (up to 120)
    max_audio_seconds = 60                  # most audio one sentence may become (up to 300)

    [voice.vad]                             # speech detection (S08); off until models_dir is set
    models_dir = "/home/me/.local/share/tumbleweed/wake"    # where scripts/wake-models put them
    threshold = 0.5                         # speech probability at or above which audio is speech
    end_silence_ms = 800                    # this much silence after speech ends a hands-free turn
    min_speech_ms = 128                     # less than this of speech is noise, not speech
    wait_for_speech_seconds = 6             # a turn that hears no speech this long is given up

    [voice.wake]                            # the wake word (S08): hands-free mode
    enabled = true
    models_dir = "/home/me/.local/share/tumbleweed/wake"    # names it again: hands-free is opt-in
    model = "hey_jarvis"                    # "alexa", "hey_jarvis" or "hey_mycroft"
    threshold = 0.5                         # score at which the wake word is heard
    speaking_threshold = 0.9                # the stricter score while a reply is being spoken
    speaking_holdoff_ms = 500               # ... and for this long after it ends

It holds no secret. The URL is validated here (`SttEndpoint`: http or https, no credentials, query
or fragment), and so is the shape of the two paths of `[voice.tts]` (absolute, no control
characters; the files are checked when the voice is built, `voice.piper`), so that a bad one is
refused when the file is read, with a message that does not repeat it.
"""

from typing import Final, Literal, Self, final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import ByteCount
from tumbleweed.voice.endpoint import SttEndpoint
from tumbleweed.voice.limits import BYTES_PER_SAMPLE, VoiceLimits
from tumbleweed.voice.stt import is_language_code

DEFAULT_STT_URL: Final = "http://127.0.0.1:8080"
MAX_PATH_CHARS: Final = 4096


def _is_absolute_path_text(value: str) -> bool:
    """Absolute, bounded, and free of NUL and other control characters (the file itself is
    checked where it is used)."""
    return (
        value.startswith("/")
        and len(value) <= MAX_PATH_CHARS
        and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    )


@final
class TtsConfig(BaseModel):
    """The `[voice.tts]` table: Piper, and what one reply and one sentence may cost (I9)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    enabled: bool = True
    piper_path: str | None = None
    voice_model: str | None = None
    sample_rate_hz: int = Field(default=22_050, ge=8_000, le=48_000)
    max_sentence_chars: int = Field(default=400, ge=40, le=1000)
    max_reply_chars: int = Field(default=4000, ge=100, le=20_000)
    synthesis_timeout_seconds: int = Field(default=20, ge=1, le=120)
    max_audio_seconds: int = Field(default=60, ge=1, le=300)

    @field_validator("piper_path", "voice_model")
    @classmethod
    def _path_is_absolute(cls, value: str | None) -> str | None:
        if value is not None and not _is_absolute_path_text(value):
            raise ValueError("must be an absolute path with no control characters")
        return value

    @property
    def configured(self) -> bool:
        """Whether there is a voice to speak with: speech is on and both paths are given."""
        return self.enabled and self.piper_path is not None and self.voice_model is not None

    @property
    def max_audio_bytes(self) -> ByteCount:
        return ByteCount(self.sample_rate_hz * BYTES_PER_SAMPLE * self.max_audio_seconds)


WakeModelName = Literal["alexa", "hey_jarvis", "hey_mycroft"]
"""The wake words there is a pinned, hash-checked model for (`voice.models`): a closed set, so a
configured name can never name a file."""


@final
class VadConfig(BaseModel):
    """The `[voice.vad]` table: Silero speech detection, which ends a hands-free turn by itself and
    trims push-to-talk recordings (S08, D23)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    models_dir: str | None = None
    threshold: float = Field(default=0.5, ge=0.1, le=0.95)
    end_silence_ms: int = Field(default=800, ge=200, le=3000)
    min_speech_ms: int = Field(default=128, ge=32, le=1000)
    wait_for_speech_seconds: int = Field(default=6, ge=1, le=30)

    @field_validator("models_dir")
    @classmethod
    def _dir_is_absolute(cls, value: str | None) -> str | None:
        if value is not None and not _is_absolute_path_text(value):
            raise ValueError("must be an absolute path with no control characters")
        return value


@final
class WakeConfig(BaseModel):
    """The `[voice.wake]` table: which wake word, how sure the model must be, and how much stricter
    that is while the assistant speaks (its own voice, heard back, must not wake it)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    enabled: bool = True
    models_dir: str | None = None
    model: WakeModelName = "hey_jarvis"
    threshold: float = Field(default=0.5, ge=0.1, le=0.99)
    speaking_threshold: float = Field(default=0.9, ge=0.1, le=1.0)
    speaking_holdoff_ms: int = Field(default=500, ge=0, le=3000)

    @field_validator("models_dir")
    @classmethod
    def _dir_is_absolute(cls, value: str | None) -> str | None:
        if value is not None and not _is_absolute_path_text(value):
            raise ValueError("must be an absolute path with no control characters")
        return value

    @model_validator(mode="after")
    def _speaking_is_not_looser(self) -> Self:
        if self.speaking_threshold < self.threshold:
            raise ValueError("speaking_threshold must not be below threshold")
        return self


@final
class VoiceConfig(BaseModel):
    """The `[voice]` table."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    enabled: bool = True
    stt_url: str = DEFAULT_STT_URL
    language: str = "auto"
    edit_before_send: bool = False
    limits: VoiceLimits = VoiceLimits()
    tts: TtsConfig = TtsConfig()
    tts_problem: Literal["invalid"] | None = None
    """Set by the loader (`agent.config`) when `[voice.tts]` was invalid: speech is then off and the
    factory tells the user in fixed text. Not a file setting."""
    vad: VadConfig = VadConfig()
    wake: WakeConfig = WakeConfig()
    hands_free_problem: Literal["invalid"] | None = None
    """Set by the loader when `[voice.vad]` or `[voice.wake]` was invalid: hands-free mode and the
    trimming of push-to-talk recordings are then off, and the factory tells the user in fixed text.
    Push-to-talk and speech stay. Not a file setting."""

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
