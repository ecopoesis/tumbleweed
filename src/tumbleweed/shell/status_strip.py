"""The status strip along the bottom of the window, with the voice indicator."""

import enum
from typing import assert_never, final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.shell._gi import Gtk
from tumbleweed.shell._widgets import plain_label


class VoiceState(enum.StrEnum):
    """What the voice side is doing. Nothing sets it yet: S06 and S07 wire it up."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


def _describe(state: VoiceState) -> str:
    match state:
        case VoiceState.IDLE:
            return "Voice: idle"
        case VoiceState.LISTENING:
            return "Voice: listening"
        case VoiceState.THINKING:
            return "Voice: thinking"
        case VoiceState.SPEAKING:
            return "Voice: speaking"
        case _ as unreachable:
            assert_never(unreachable)


@final
class VoiceIndicator(Gtk.Box):
    """Shows a `VoiceState` as text."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self._label = plain_label("", css_classes=["caption", "dim-label"])
        self.append(self._label)
        self._state = VoiceState.IDLE
        self.set_state(VoiceState.IDLE)

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def text(self) -> str:
        return self._label.get_text()

    def set_state(self, state: VoiceState) -> None:
        if type(state) is not VoiceState:
            raise InvalidValueError(f"expected a VoiceState, got {type(state).__name__}")
        self._state = state
        self._label.set_text(_describe(state))


@final
class StatusStrip(Gtk.Box):
    """The strip itself: the voice indicator, and room for later status items."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.set_name("tumbleweed-status")
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(4)
        self.set_margin_bottom(4)
        self.voice = VoiceIndicator()
        self.append(self.voice)
