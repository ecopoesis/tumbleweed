"""Errors of the voice package.

Every `VoiceError` carries a message that is safe to show in the UI as it is: fixed text written
here or by the code that raised it, never the words of a speech server or of a library (a server
can put anything in them). The UI shows `str(error)`.
"""

from tumbleweed.core.errors import TumbleweedError


class VoiceError(TumbleweedError):
    """Base of the voice package's expected failures; `str(error)` is safe to show."""


class AudioError(VoiceError):
    """Audio could not be captured: no GStreamer, no microphone, the device failed."""


class SttError(VoiceError):
    """Speech could not be turned into text."""


class SttUnavailableError(SttError):
    """The speech server could not be reached, or did not answer in time."""


class SttResponseError(SttError):
    """The speech server answered with something that cannot be used: an error status, a body
    that is too large or not what was asked for, text that is too long."""


class WrongThreadError(VoiceError):
    """A `VoiceInput` method that belongs to the main thread was called from another one."""
