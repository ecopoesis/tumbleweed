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

Hands-free mode (`HandsFree`, S08) has a **Hands-free** toggle after Mute (none unless
`[voice.wake]` is set up and its models check out): no tooltip, no focus, not a `GAction`, pressed
while it is on. The indicator then says "waiting for the wake word", and shows "Microphone on"
whenever audio may be being captured, hands-free or push-to-talk, in whatever state the voice
indicator is in: the mic-active indicator is separate from the state so that "speaking" or
"thinking" never hides an open microphone. The wake word is barge-in and the turn it starts is
transcribed through the same path as dictation (`transcribed`: `edit_before_send` applies).
THINKING (#117) is shown from the moment a dictated message is sent until the reply is spoken
(SPEAKING) or finished. Esc during a hands-free turn abandons it, as for push-to-talk, and so does
Mute.

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
from tumbleweed.shell.viewmodel.transcript import MessageState, Role, Transcript, TranscriptMessage
from tumbleweed.voice.hands_free import HandsFree, HandsFreeObserver, HandsFreePhase
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
class _HandsFreeView(HandsFreeObserver):
    """What `VoiceControls` shows of hands-free mode: the toggle, the indicator, a problem."""

    def __init__(self, controls: "VoiceControls") -> None:
        self._controls = controls

    def phase_changed(self, phase: HandsFreePhase) -> None:
        self._controls.hands_free_phase_changed(phase)

    def microphone_changed(self, is_open: bool) -> None:
        self._controls.refresh_indicator()

    def problem(self, message: str) -> None:
        self._controls.problem(message)


@final
class _ReplyWatch:
    """A `TranscriptObserver`: a voice turn that was sent is THINKING until its reply ends (#117)
    or is spoken."""

    def __init__(self, controls: "VoiceControls") -> None:
        self._controls = controls

    def message_added(self, message: TranscriptMessage) -> None:
        if message.role is Role.USER:
            if self._controls.dictating:
                self._controls.set_thinking(True)
        elif message.state is not MessageState.STREAMING:
            self._controls.set_thinking(False)

    def message_changed(self, message: TranscriptMessage) -> None:
        if message.role is Role.ASSISTANT and message.state is not MessageState.STREAMING:
            self._controls.set_thinking(False)

    def messages_replaced(self, messages: tuple[TranscriptMessage, ...]) -> None:
        self._controls.set_thinking(False)


@final
class VoiceControls(Gtk.Box):
    """The mic, mute and hands-free buttons and the voice message line, observing a `VoiceInput`, a
    `SpeechOutput` and a `HandsFree`."""

    def __init__(
        self,
        voice: VoiceInput,
        indicator: VoiceIndicator,
        chat_panel: ChatPanel,
        *,
        edit_before_send: bool,
        speech: SpeechOutput | None = None,
        notice: str | None = None,
        hands_free: HandsFree | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._voice = voice
        self._speech = speech
        self._hands_free = hands_free
        self._hands_free_view = _HandsFreeView(self)
        self.reply_watch = _ReplyWatch(self)
        self.dictating = False  # a transcript is being put in the chat, to be sent
        self._thinking = False
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
        # Hands-free: same rules again (no tooltip, no focus, not in the tab order, not a GAction).
        self.hands_free_button: Gtk.ToggleButton | None = None
        if hands_free is not None:
            self.hands_free_button = Gtk.ToggleButton(label="Hands-free")
            self.hands_free_button.add_css_class("flat")
            self.hands_free_button.set_focusable(False)
            self.hands_free_button.set_focus_on_click(False)
            self.hands_free_button.connect("toggled", self._on_hands_free_toggled)
        self.message = plain_label("", css_classes=["caption", "error"])
        self.message.set_ellipsize(Pango.EllipsizeMode.END)
        self.message.set_max_width_chars(_MESSAGE_WIDTH_CHARS)
        self.message.set_visible(False)
        self.append(self.mic_button)
        if self.mute_button is not None:
            self.append(self.mute_button)
        if self.hands_free_button is not None:
            self.append(self.hands_free_button)
        self.append(self.message)
        if hands_free is not None:
            hands_free.subscribe(self._hands_free_view)
        if speech is not None:
            # Before `self`: the microphone opening stops speech before anything else reacts.
            voice.subscribe(BargeIn(speech))
            speech.subscribe(self._speech_view)
        voice.subscribe(self)
        voice.subscribe_microphone(self._on_microphone)
        if notice:
            self._show_message(notice)

    def install_keys(self, window: Gtk.Window, in_terminal: Callable[[], bool]) -> None:
        """Hold Ctrl+Space to talk, anywhere in `window` but the terminal pane."""
        self._in_terminal = in_terminal
        self.keys = install_push_to_talk(
            window, self._voice, in_terminal, self._speech, self._hands_free
        )

    def observe(self, transcript: Transcript) -> None:
        """Watch the transcript, so that a sent voice turn shows THINKING until its reply ends."""
        transcript.subscribe(self.reply_watch)

    # VoiceObserver

    def phase_changed(self, phase: VoicePhase) -> None:
        self.refresh_indicator()
        if phase is VoicePhase.LISTENING:
            self._show_message("")
        self._sync_button(phase)

    def _on_microphone(self, is_open: bool) -> None:
        self.refresh_indicator()

    def refresh_indicator(self) -> None:
        """What the voice side is doing, most pressing first: listening or transcribing
        (push-to-talk or hands-free), then speaking, then thinking, then waiting for the wake word.
        Separately, whether the microphone may be capturing (push-to-talk's recording, or
        hands-free's open stream, which stays "on" until it is confirmed closed)."""
        hands_free = self._hands_free
        if self._voice.phase is not VoicePhase.IDLE:
            self._indicator.set_state(voice_state(self._voice.phase))
        elif hands_free is not None and hands_free.phase is HandsFreePhase.LISTENING:
            self._indicator.set_state(VoiceState.LISTENING)
        elif self._speech is not None and self._speech.phase is SpeechPhase.SPEAKING:
            self._indicator.set_state(VoiceState.SPEAKING)
        elif self._thinking:
            self._indicator.set_state(VoiceState.THINKING)
        elif hands_free is not None and hands_free.phase is HandsFreePhase.WAITING:
            self._indicator.set_state(VoiceState.WAKE)
        else:
            self._indicator.set_state(VoiceState.IDLE)
        self._indicator.set_microphone(
            self._voice.microphone_open or (hands_free is not None and hands_free.microphone_open)
        )

    def set_thinking(self, thinking: bool) -> None:
        if thinking is not self._thinking:
            self._thinking = thinking
            self.refresh_indicator()

    def hands_free_phase_changed(self, phase: HandsFreePhase) -> None:
        if phase is HandsFreePhase.LISTENING:
            self._show_message("")
        button = self.hands_free_button
        if button is not None:
            self._syncing = True
            try:
                button.set_active(phase is not HandsFreePhase.OFF)
            finally:
                self._syncing = False
        self.refresh_indicator()

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
        self.dictating = not self._edit_before_send
        try:
            self._chat_panel.dictate(text, send=not self._edit_before_send)
        except Exception:
            logger.exception("putting a transcript in the chat failed")
            self._show_message("The transcript could not be added to the chat.")
        finally:
            self.dictating = False

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
        hands_free = self._hands_free
        if hands_free is not None and hands_free.phase is HandsFreePhase.LISTENING:
            hands_free.cancel()
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
        self._voice.unsubscribe_microphone(self._on_microphone)
        try:
            if self._hands_free is not None:
                self._hands_free.unsubscribe(self._hands_free_view)
                self._hands_free.close()
        finally:
            try:
                if self._speech is not None:
                    self._speech.unsubscribe(self._speech_view)
                    self._speech.close()
            finally:
                self._voice.close()

    def _on_mute_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._speech is not None:
            self._speech.set_muted(button.get_active())
        if self._hands_free is not None:
            self._hands_free.cancel()  # a wake word not yet acted on, or a turn begun, is dropped

    def _on_hands_free_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._syncing or self._hands_free is None:
            return
        self._hands_free.set_enabled(button.get_active())
        # A refusal (or a failure at once) must not leave the button pressed.
        self.hands_free_phase_changed(self._hands_free.phase)

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
    hands_free: HandsFree | None = None,
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
    terminal's Esc) or a hands-free turn is being recorded (which it abandons, as it does an
    utterance: hands-free adds no key of its own).

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
            hearing = hands_free is not None and hands_free.phase is HandsFreePhase.LISTENING
            speaking = (
                speech is not None and speech.phase is SpeechPhase.SPEAKING and not in_terminal()
            )
            if state & Gtk.accelerator_get_default_mod_mask() or not (
                listening or hearing or speaking
            ):
                return False
            held = False
            if listening:
                voice.cancel()
            if hands_free is not None and hearing:
                hands_free.cancel()
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
