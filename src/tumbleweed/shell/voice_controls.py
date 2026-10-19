"""The shell's side of voice: push-to-talk (S06) and speech output (S07), the buttons and the key.

`VoiceControls` is the status strip's voice item. It observes a `VoiceInput` (GTK-free,
`tumbleweed.voice.input`) and shows what it reports: the voice indicator goes idle, listening,
transcribing; the mic button follows; a problem (no microphone, a server that does not answer, an
empty recording) is shown in a line next to the button as plain text. What was said goes to the
chat panel's input (`ChatPanel.dictate`): sent at once, or left there to edit when the config says
`edit_before_send`.

Speech output (`SpeechOutput`, fed by `SpeechFeed`) has a **Mute** toggle beside the Mic (none if
speech is not set up) and shares the voice indicator, which says "speaking" while a reply is being
read and gives way to listening and transcribing. The microphone opening stops speech first
(`BargeIn`): push-to-talk, by the key or the Mic, is barge-in, and nothing is spoken while it is
open, so the assistant never hears itself. Esc stops speech too (not in the terminal pane).

Push-to-talk is a key and a button, both plain widgets, never `GAction`s: actions are exported on
the session bus (`tests/shell/test_session_bus.py` pins the set), and another process must not be
able to switch the microphone on (ARCHITECTURE I10, section 8).

- **Hold Ctrl+Space** to talk and let go to send (`install_push_to_talk`: a capture-phase key
  controller on the window, so the key works wherever the focus is **except in the terminal pane**,
  where Ctrl+Space is the shell's and the event is not touched). The key is latched: repeat while it
  is held does nothing. Releasing Space or Ctrl ends the recording the key started, and so does the
  window losing focus, so a key release that is never delivered cannot leave the microphone open
  (the recording also stops by itself at its length limit).
- **Click the mic** to start and click again to stop; the button shows pressed while listening.
- **Esc** while listening or transcribing abandons the utterance, wherever the focus is, the
  terminal included (the same capture controller). While a reply is **being spoken** Esc stops
  the speech, *except while the terminal has the focus*: there it goes to the terminal, because
  how long the speaking lasts is up to the model, and a user in vim must not lose the Escape that
  leaves insert mode (Mute, or Esc outside the terminal, stops it). With nothing in progress the
  key is left alone. Esc does not also cancel a reply that is still streaming: a second Esc does
  (the window's own Esc), so stopping the voice never loses the text.

Nothing here blocks: `VoiceInput` does the audio and the network on worker threads.
"""

import logging
from collections.abc import Callable
from typing import assert_never, final

from tumbleweed.shell._gi import Gdk, Gtk, Pango
from tumbleweed.shell._widgets import plain_label
from tumbleweed.shell.chat_panel import ChatPanel
from tumbleweed.shell.status_strip import VoiceIndicator, VoiceState
from tumbleweed.shell.viewmodel.display import display_text
from tumbleweed.voice.input import VoiceInput, VoicePhase
from tumbleweed.voice.output import BargeIn, SpeechOutput, SpeechPhase

logger = logging.getLogger(__name__)

_RELEASE_KEYS = frozenset({Gdk.KEY_space, Gdk.KEY_Control_L, Gdk.KEY_Control_R})
_MESSAGE_WIDTH_CHARS = 60


def voice_state(phase: VoicePhase) -> VoiceState:
    """The indicator's state for a push-to-talk phase."""
    match phase:
        case VoicePhase.IDLE:
            return VoiceState.IDLE
        case VoicePhase.LISTENING:
            return VoiceState.LISTENING
        case VoicePhase.TRANSCRIBING:
            return VoiceState.TRANSCRIBING
        case _ as unreachable:
            assert_never(unreachable)


@final
class _SpeechView:
    """What `VoiceControls` shows of the speech: the indicator, and a problem as a message."""

    def __init__(self, controls: "VoiceControls") -> None:
        self._controls = controls

    def phase_changed(self, phase: SpeechPhase) -> None:
        self._controls.refresh_indicator()

    def problem(self, message: str) -> None:
        self._controls.problem(message)


@final
class VoiceControls(Gtk.Box):
    """The mic and mute buttons and the voice message line, observing a `VoiceInput` and a
    `SpeechOutput`."""

    def __init__(
        self,
        voice: VoiceInput,
        indicator: VoiceIndicator,
        chat_panel: ChatPanel,
        *,
        edit_before_send: bool,
        speech: SpeechOutput | None = None,
        notice: str | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._voice = voice
        self._speech = speech
        self._speech_view = _SpeechView(self)
        self._indicator = indicator
        self._chat_panel = chat_panel
        self._edit_before_send = edit_before_send
        self._syncing = False  # the button is being set from the phase, not clicked
        self.keys: Gtk.EventControllerKey | None = None
        self._in_terminal: Callable[[], bool] = lambda: False  # until `install_keys`

        self.mic_button = Gtk.ToggleButton(label="Mic")
        self.mic_button.add_css_class("flat")
        # Not in the tab order and no focus from a click, like the Terminal button: the button goes
        # insensitive while it transcribes, which would take the focus off it and leave the window
        # with none, and a Space on a focused Mic would start a recording by itself. The keyboard
        # way is Ctrl+Space.
        self.mic_button.set_focusable(False)
        self.mic_button.set_focus_on_click(False)
        # No tooltip: a tooltip is a surface of its own, and none is allowed in the window (#53).
        self.mic_button.connect("toggled", self._on_toggled)
        # Mute: same rules as the Mic (no tooltip, no focus, not in the tab order); the pressed look
        # is the state. It stops what is being said and keeps the rest of that reply silent.
        self.mute_button: Gtk.ToggleButton | None = None
        if speech is not None:
            self.mute_button = Gtk.ToggleButton(label="Mute")
            self.mute_button.add_css_class("flat")
            self.mute_button.set_focusable(False)
            self.mute_button.set_focus_on_click(False)
            self.mute_button.connect("toggled", self._on_mute_toggled)
        self.message = plain_label("", css_classes=["caption", "error"])
        self.message.set_ellipsize(Pango.EllipsizeMode.END)
        self.message.set_max_width_chars(_MESSAGE_WIDTH_CHARS)
        self.message.set_visible(False)
        self.append(self.mic_button)
        if self.mute_button is not None:
            self.append(self.mute_button)
        self.append(self.message)
        if speech is not None:
            # Before `self`: the microphone opening stops speech before anything else reacts.
            voice.subscribe(BargeIn(speech))
            speech.subscribe(self._speech_view)
        voice.subscribe(self)
        if notice:
            self._show_message(notice)

    def install_keys(self, window: Gtk.Window, in_terminal: Callable[[], bool]) -> None:
        """Hold Ctrl+Space to talk, anywhere in `window` but the terminal pane."""
        self._in_terminal = in_terminal
        self.keys = install_push_to_talk(window, self._voice, in_terminal, self._speech)

    # VoiceObserver

    def phase_changed(self, phase: VoicePhase) -> None:
        self.refresh_indicator()
        if phase is VoicePhase.LISTENING:
            self._show_message("")
        self._sync_button(phase)

    def refresh_indicator(self) -> None:
        """Listening and transcribing come first; else "speaking" while a reply is read."""
        if self._voice.phase is not VoicePhase.IDLE:
            self._indicator.set_state(voice_state(self._voice.phase))
        elif self._speech is not None and self._speech.phase is SpeechPhase.SPEAKING:
            self._indicator.set_state(VoiceState.SPEAKING)
        else:
            self._indicator.set_state(VoiceState.IDLE)

    def _sync_button(self, phase: VoicePhase) -> None:
        self._syncing = True
        try:
            self.mic_button.set_active(phase is VoicePhase.LISTENING)
            self.mic_button.set_sensitive(phase is not VoicePhase.TRANSCRIBING)
        finally:
            self._syncing = False

    def transcribed(self, text: str) -> None:
        # The message line is left alone: a notice that came with this utterance ("limited to 30
        # seconds") is still news. The next LISTENING phase clears it.
        try:
            self._chat_panel.dictate(text, send=not self._edit_before_send)
        except Exception:
            logger.exception("putting a transcript in the chat failed")
            self._show_message("The transcript could not be added to the chat.")

    def problem(self, message: str) -> None:
        self._show_message(message)

    # What the user does

    def cancel_listening(self) -> bool:
        """Abandon the utterance and stop the speech (unless the terminal has the focus) if there
        are any; True if the key was used for that (Esc)."""
        used = False
        if self._voice.phase is not VoicePhase.IDLE:
            self._voice.cancel()
            used = True
        # Not while the terminal has the focus: its Esc is its own (as in the capture controller),
        # whether or not the terminal widget happens to consume the key before this runs.
        if (
            self._speech is not None
            and self._speech.phase is SpeechPhase.SPEAKING
            and not self._in_terminal()
        ):
            self._speech.stop()
            used = True
        return used

    def shutdown(self) -> None:
        """Stop observing, abandon the utterance, stop the speech, and release the microphone, the
        speaker and the connection."""
        self._voice.unsubscribe(self)
        try:
            if self._speech is not None:
                self._speech.unsubscribe(self._speech_view)
                self._speech.close()
        finally:
            self._voice.close()

    def _on_mute_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._speech is not None:
            self._speech.set_muted(button.get_active())

    def _on_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._syncing:
            return
        if button.get_active():
            self._voice.press()
        else:
            self._voice.release()
        self._sync_button(self._voice.phase)  # a press that was ignored must not leave it pressed

    def _show_message(self, text: str) -> None:
        self.message.set_text(display_text(text))
        self.message.set_visible(bool(text))


def install_push_to_talk(
    window: Gtk.Window,
    voice: VoiceInput,
    in_terminal: Callable[[], bool],
    speech: SpeechOutput | None = None,
) -> Gtk.EventControllerKey:
    """Hold Ctrl+Space to talk, anywhere in `window` but the terminal; see the module docstring.

    This is the one key controller in the shell outside the terminal's own modules, and it is
    reviewed as such (`tests/shell/test_terminal_isolation.py`): it looks at the key value and the
    modifiers of a press or release, nothing else, and it does nothing at all, letting the event
    through untouched, while `in_terminal()` says the focus is in the terminal pane (Ctrl+Space is
    the shell's own key there: set-mark in Emacs and readline). The one other key it takes is a
    plain Esc while an utterance is in progress (which it abandons, the terminal included: the
    terminal would otherwise get it) or a reply is being spoken (which it stops, but not while the
    terminal has the focus: speech lasts as long as the model's text, so it never takes the
    terminal's Esc).

    The key is *latched*: only the first Ctrl+Space press starts a recording, key repeat is consumed
    and ignored, and only the release of Space or Ctrl (or the window losing focus) ends what the
    keyboard started. A recording the mic button started is not ended by a space typed in the
    input, and a held key cannot record again when its utterance ends.
    """
    keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
    held = (
        False  # the keyboard has an utterance in hand: set by the first press, cleared by release
    )

    def on_pressed(
        _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, state: Gdk.ModifierType
    ) -> bool:
        nonlocal held
        if keyval == Gdk.KEY_Escape:
            # Esc abandons an utterance in progress, wherever the focus is (the terminal takes the
            # bubble-phase Esc), and stops speech, except with the terminal focused (a model-long
            # speech must not eat vim's Esc); with neither it is not ours, and vim still gets it.
            listening = voice.phase is not VoicePhase.IDLE
            speaking = (
                speech is not None and speech.phase is SpeechPhase.SPEAKING and not in_terminal()
            )
            if state & Gtk.accelerator_get_default_mod_mask() or not (listening or speaking):
                return False
            held = False
            if listening:
                voice.cancel()
            if speech is not None and speaking:
                speech.stop()
            return True
        if keyval != Gdk.KEY_space:
            return False
        if state & Gtk.accelerator_get_default_mod_mask() != Gdk.ModifierType.CONTROL_MASK:
            return False
        if in_terminal():
            return False  # the terminal's key: untouched, and no latch
        if not held:
            held = True
            voice.press()
        return True  # key repeat is consumed: it never types spaces, it never records again

    def on_released(
        _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType
    ) -> None:
        nonlocal held
        if keyval in _RELEASE_KEYS and held:
            held = False
            voice.release()

    def on_focus_changed(_window: Gtk.Window, _pspec: object) -> None:
        # A window that is no longer active gets no key release.
        nonlocal held
        if not window.is_active() and held:
            held = False
            voice.release()

    keys.connect("key-pressed", on_pressed)
    keys.connect("key-released", on_released)
    window.add_controller(keys)
    window.connect("notify::is-active", on_focus_changed)
    return keys
