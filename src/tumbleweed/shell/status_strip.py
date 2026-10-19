"""The status strip along the bottom of the window: the voice indicator, the voice buttons (Mic and
Mute, added by the window) and the terminal button."""

import enum
from typing import assert_never, final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.shell._gi import Gtk
from tumbleweed.shell._widgets import plain_label


class VoiceState(enum.StrEnum):
    """What the voice side is doing. Push-to-talk sets IDLE, LISTENING and TRANSCRIBING (S06),
    speech output sets SPEAKING (S07), and hands-free mode (S08) sets WAKE (the microphone is open,
    waiting for the wake word) and, with the chat, THINKING (a voice turn was sent and the agent has
    not yet spoken or finished)."""

    IDLE = "idle"
    WAKE = "wake"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"


def _describe(state: VoiceState) -> str:
    match state:
        case VoiceState.IDLE:
            return "Voice: idle"
        case VoiceState.WAKE:
            return "Voice: waiting for the wake word"
        case VoiceState.LISTENING:
            return "Voice: listening"
        case VoiceState.TRANSCRIBING:
            return "Voice: transcribing"
        case VoiceState.THINKING:
            return "Voice: thinking"
        case VoiceState.SPEAKING:
            return "Voice: speaking"
        case _ as unreachable:
            assert_never(unreachable)


MICROPHONE_ON_TEXT = "Microphone on"


@final
class VoiceIndicator(Gtk.Box):
    """Shows a `VoiceState` as text, and, next to it, whether the microphone may be capturing.

    The microphone label is separate from the state because the two are different facts: the
    state can say "speaking" or "thinking" while hands-free mode still has the microphone open,
    and the user must always be able to see that it is (S08).
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._label = plain_label("", css_classes=["caption", "dim-label"])
        self.append(self._label)
        self._microphone = plain_label(MICROPHONE_ON_TEXT, css_classes=["caption", "error"])
        self._microphone.set_visible(False)
        self.append(self._microphone)
        self._state = VoiceState.IDLE
        self.set_state(VoiceState.IDLE)

    @property
    def microphone_on(self) -> bool:
        """Whether the indicator says that the microphone may be capturing."""
        return self._microphone.get_visible()

    @property
    def microphone_text(self) -> str:
        return self._microphone.get_text()

    def set_microphone(self, microphone_on: bool) -> None:
        self._microphone.set_visible(microphone_on)

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
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.append(spacer)
        # A widget, not a GAction: actions are exported on the session bus (test_session_bus.py),
        # and starting a shell is not something another process should be able to ask for.
        self.terminal_button = Gtk.Button(label="Terminal (F12)")
        self.terminal_button.set_has_frame(False)
        # Not in the tab order and no focus from a click: F12 is the keyboard way, and a button
        # that took the focus would get it when the side panel hides, and start a terminal on a
        # stray Space or Enter.
        self.terminal_button.set_focusable(False)
        self.terminal_button.set_focus_on_click(False)
        self.terminal_button.add_css_class("caption")
        # No tooltip: a tooltip is a surface of its own, and none is allowed in the window (#53).
        self.append(self.terminal_button)
