"""The `SttBackend` protocol, and the one place where a transcript is made safe to use.

A speech server's answer is untrusted text: a hostile or broken server (or a proxy in front of
it) can send anything, and so can the audio itself (a video playing near the microphone, a
person nearby: ARCHITECTURE section 8, "Ambient audio"). `clean_transcript` is where whatever any
backend returns becomes the text that may enter the chat input: exactly a `str`, bounded, without
control or format characters (which can hide or reorder what the user reads), on one line, and
nothing at all when it is only a non-speech annotation like `[BLANK_AUDIO]`.

What the transcript is, afterwards: **the user's own words**. The user held the key and spoke, as if
they had typed it, so it enters the conversation as an ordinary user message (the model is told
nothing different about it), and it carries no more authority than typing would. Nothing a
transcript says can approve anything: prompts and pickers take keyboard and pointer input only
(I10), and the microphone is open only while the user holds the key or has toggled the button.
"""

import re
import threading
import unicodedata
from collections.abc import Callable
from typing import Final, Protocol, final

from tumbleweed.core.types import ByteCount
from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.errors import SttResponseError

_SEPARATORS: Final = frozenset({"Cc", "Zl", "Zp"})  # controls and line and paragraph separators
# The zero-width joiners are part of how some scripts and emoji sequences are written.
_KEPT_FORMAT: Final = frozenset({"\N{ZERO WIDTH NON-JOINER}", "\N{ZERO WIDTH JOINER}"})
# What whisper writes for sound that is not speech: "[BLANK_AUDIO]", "(silence)", "[ Music ]",
# "(upbeat music)", "[MUSIC PLAYING]", "[background noise]". A text that is only bracketed or
# parenthesised groups, each made only of words from this vocabulary, is no speech. The trade-off:
# a spoken "(music)" alone is lost; short bracketed speech ("(1)", "[sic]", "(yes)") is kept.
_ANNOTATION_WORDS: Final = frozenset(
    {
        "blank", "audio", "silence", "silent", "music", "playing", "plays", "applause",
        "clapping", "laughter", "laughing", "noise", "noises", "inaudible", "crosstalk", "no",
        "speech", "pause", "sigh", "sighs", "cough", "coughing", "upbeat", "soft", "gentle",
        "dramatic", "background", "light", "loud", "sad", "calm", "static", "sound", "sounds",
        "typing", "breathing",
    }
)  # fmt: skip
_GROUP: Final = r"[\[(]([^\[\]()]{0,60})[\])]"
_ONLY_GROUPS: Final = re.compile(rf"(?:{_GROUP}\s*)+")
_GROUPS: Final = re.compile(_GROUP)


def _is_annotation_only(text: str) -> bool:
    if _ONLY_GROUPS.fullmatch(text) is None:
        return False
    for inner in _GROUPS.findall(text):
        words = inner.lower().replace("_", " ").split()
        if not words or not set(words) <= _ANNOTATION_WORDS:
            return False
    return True


_LANGUAGE: Final = re.compile(r"auto|[a-z]{2,3}")


def is_language_code(text: str) -> bool:
    """Whether `text` is what whisper-server takes as a language: "auto" or a short code ("en")."""
    raw: object = text  # the annotation is for the checker; a caller can pass anything
    return type(raw) is str and _LANGUAGE.fullmatch(text) is not None


@final
class AbortSignal:
    """Set (from any thread) to abandon one request: whatever is waiting on the network for it
    stops at once. Made per utterance by `VoiceInput`, set by its `cancel`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._set = False
        self._callbacks: list[Callable[[], None]] = []

    @property
    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        with self._lock:
            if self._set:
                return
            self._set = True
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback()

    def on_set(self, callback: Callable[[], None]) -> None:
        """Run `callback` when the signal is set: now, if it already is. Once."""
        with self._lock:
            if not self._set:
                self._callbacks.append(callback)
                return
        callback()

    def remove(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)


class SttBackend(Protocol):
    """Turns an utterance into text. `transcribe` blocks, for a bounded time (the request
    timeout), and is called from a worker thread, never the GTK main thread."""

    def transcribe(self, utterance: Utterance, abort: AbortSignal | None = None) -> str:
        """The raw text of what was said. Raises `SttError`, `SttCancelledError` if `abort` was
        set. The text is untrusted: the caller passes it through `clean_transcript`."""
        ...

    def close(self) -> None:
        """Release what the backend holds (connections). Never raises."""
        ...


def clean_transcript(raw: str, *, max_bytes: ByteCount) -> str | None:
    """The usable text of a backend's answer, or None if nothing was said.

    Raises `SttResponseError` if `raw` is not a `str` or is longer than `max_bytes` (in UTF-8),
    measured before anything is done to it.
    """
    value: object = raw  # the annotation is for the checker; a backend can return anything
    if type(value) is not str:
        raise SttResponseError("The speech server's answer was not text.")
    try:
        size = len(raw.encode("utf-8"))  # strict: a lone surrogate cannot be encoded
    except UnicodeEncodeError:
        raise SttResponseError("The speech server's answer was not valid text.") from None
    if size > max_bytes:
        raise SttResponseError("The speech server's answer was too long to use.")
    kept: list[str] = []
    for char in raw:
        category = unicodedata.category(char)
        if category in _SEPARATORS:
            kept.append(" ")  # tabs and newlines separate words; the other controls are noise
        elif category == "Cf" and char not in _KEPT_FORMAT:
            continue  # right-to-left overrides, zero-width spaces: they change what is read
        else:
            kept.append(char)
    text = " ".join("".join(kept).split())
    if not text or _is_annotation_only(text):
        return None
    return text
