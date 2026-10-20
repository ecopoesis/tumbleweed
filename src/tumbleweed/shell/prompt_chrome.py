"""The trusted prompt, drawn in the reserved chrome region (S18, I5, I10).

`PromptBanner` is a `PromptView`: it draws the `PromptCard` that `PromptController` hands it and
reports a click on a button back to the controller, which decides whether the click counts. What
makes it the one thing on screen a model cannot imitate:

- it lives in the chrome region, outside the canvas (`tumbleweed.shell.window`), and has styling
  (`RESERVED_CLASSES`) that nothing from a spec can name: a spec chooses from a closed set of
  components and has no way to set a CSS class (`tests/shell/test_prompt_chrome.py` renders a
  hostile spec and looks);
- what is granted is drawn from the card's structure, by our code, in our words; the path is
  data, shown in its own labelled line, and the model's reason is set apart as a quotation (with no
  quotation marks: none of its characters can close one), labelled as the assistant's, one plain
  line and capped (`viewmodel.prompts`);
- every string that is not ours goes in with `set_text` (`plain_label`), never as markup (I4);
- Deny has the focus when the prompt appears, and the buttons ignore input for the first 750 ms
  (the controller decides; `set_armed` only dims them), so a key or click meant for something else a
  moment ago answers nothing;
- the buttons are plain widgets, never `GAction`s (an action is exported on the session bus,
  I5, I10), and a transcript, the voice input or the agent has no handle on them.

The trusted picker is a `Gtk.FileDialog` opened from here, by the user's click on "Choose": its
result is the grant, at the scope of the button that opened it. `FilePicker` is the seam the tests
use for the user: a dialog is the one thing a headless test cannot operate.
"""

import functools
import logging
from collections.abc import Callable
from typing import Final, Literal, Protocol, assert_never, final

from pydantic import ValidationError

from tumbleweed.core.ids import PromptId
from tumbleweed.ipc.broker_messages import Scope
from tumbleweed.ipc.prompt_messages import AllowDecision, DenyDecision
from tumbleweed.shell._gi import Gdk, Gio, GLib, Gtk
from tumbleweed.shell._widgets import plain_label
from tumbleweed.shell.prompt_guard import PromptGuard
from tumbleweed.shell.viewmodel.display import display_text
from tumbleweed.shell.viewmodel.prompts import PromptCard, PromptController

_log = logging.getLogger(__name__)

PROMPT_CLASS: Final = "tw-trusted-prompt"
TITLE_CLASS: Final = "tw-trusted-title"
REASON_CLASS: Final = "tw-trusted-reason"
DISARMED_CLASS: Final = "tw-trusted-disarmed"
RESERVED_CLASSES: Final = (PROMPT_CLASS, TITLE_CLASS, REASON_CLASS, DISARMED_CLASS)
"""The style classes only the prompt uses. Nothing a spec says can set a class at all."""

_CSS: Final = f"""
.{PROMPT_CLASS} {{
  background-color: alpha(#e5a50a, 0.22);
  border: 3px solid #e5a50a;
  padding: 10px 14px;
}}
.{TITLE_CLASS} {{ font-weight: 800; font-size: 120%; }}
.{REASON_CLASS} {{
  font-style: italic;
  padding: 4px 10px;
  border-left: 3px solid alpha(#000, 0.4);
}}
.{DISARMED_CLASS} {{ opacity: 0.45; }}
"""

_PICK_TITLE: Final = {"file": "Choose a file to share", "folder": "Choose a folder to share"}
_SCOPE_LABEL: Final[dict[Scope, str]] = {"once": "Allow once", "task": "Allow for this task"}
_PICK_LABEL: Final[dict[Scope, str]] = {
    "once": "Choose, allow once…",
    "task": "Choose, allow for this task…",
}


class FilePicker(Protocol):
    """Lets the user choose a file or folder: `done` gets its path, or None if they did not.
    Returns what closes the dialog, for a prompt that ends while it is open."""

    def pick(
        self,
        parent: Gtk.Window,
        choose: Literal["file", "folder"],
        done: Callable[[str | None], None],
    ) -> Callable[[], None]: ...


@final
class GtkFilePicker:
    """The picker: a `Gtk.FileDialog`, modal over the shell's window."""

    def pick(
        self,
        parent: Gtk.Window,
        choose: Literal["file", "folder"],
        done: Callable[[str | None], None],
    ) -> Callable[[], None]:
        dialog = Gtk.FileDialog()
        cancellable = Gio.Cancellable()
        dialog.set_title(_PICK_TITLE[choose])
        dialog.set_modal(True)

        def finished(_source: object, result: Gio.AsyncResult) -> None:
            _ = dialog  # the dialog lives until the choice is made
            try:
                chosen = (
                    dialog.select_folder_finish(result)
                    if choose == "folder"
                    else dialog.open_finish(result)
                )
            except GLib.Error:  # dismissed, or the portal failed: nothing was chosen
                done(None)
                return
            done(chosen.get_path() or None)  # None if it is not a local file

        if choose == "folder":
            dialog.select_folder(parent, cancellable, finished)
        else:
            dialog.open(parent, cancellable, finished)
        return cancellable.cancel


@functools.cache
def install_style() -> None:
    """Add the prompt's CSS to the default display, once. The classes are `RESERVED_CLASSES`."""
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


@final
class PromptBanner(Gtk.Box):
    """The prompt in the chrome region: empty and invisible until `present`."""

    def __init__(
        self, window: Gtk.Window, guard: PromptGuard, picker: FilePicker | None = None
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        install_style()
        self.set_name("tumbleweed-trusted-prompt")
        self.add_css_class(PROMPT_CLASS)
        self.set_visible(False)
        self._window = window
        self._guard = guard
        self._picker: FilePicker = GtkFilePicker() if picker is None else picker
        self._controller: PromptController | None = None
        self._card: PromptCard | None = None
        self._buttons: list[Gtk.Button] = []
        self._deny: Gtk.Button | None = None
        self._picking = False
        self._cancel_picker: Callable[[], None] | None = None
        self.note = plain_label("", wrap=True, css_classes=["caption"])
        # Whatever reaches the window while the prompt is not yet armed starts its delay again
        # (a stream of clicks aimed at where a button will appear is never taken), and so does the
        # window coming to the front. They only watch: the events go on to whoever they were for.
        presses = Gtk.GestureClick()
        presses.set_button(0)
        presses.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        presses.connect("pressed", lambda *_args: self._disturbed())
        window.add_controller(presses)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda *_args: self._disturbed())  # False: not consumed
        window.add_controller(keys)
        window.connect("notify::is-active", self._on_active_changed)

    def bind(self, controller: PromptController) -> None:
        """Whose prompts these are (the controller is made with this banner as its view)."""
        self._controller = controller

    @property
    def deny_button(self) -> Gtk.Button | None:
        return self._deny

    @property
    def allow_buttons(self) -> list[Gtk.Button]:
        return [button for button in self._buttons if button is not self._deny]

    @property
    def picking(self) -> bool:
        return self._picking

    # --- PromptView ------------------------------------------------------------------------

    def present(self, card: PromptCard) -> None:
        self._end_picker()
        self._card = card
        self._rebuild(card)
        self.set_visible(True)
        self._guard.engage()
        # The delay counts from the first frame, not from now: a slow first paint must not eat it.
        self.add_tick_callback(lambda _widget, _clock: self._first_frame(card))
        self.set_armed(False)
        if self._deny is not None:
            self._window.set_focus(self._deny)  # Deny, not whatever had the focus (I5)

    def set_armed(self, armed: bool) -> None:
        for button in self._buttons:
            if armed:
                button.remove_css_class(DISARMED_CLASS)
            else:
                button.add_css_class(DISARMED_CLASS)

    def clear(self) -> None:
        self._end_picker()
        self._card = None
        self._deny = None
        self._buttons = []
        self._empty()
        self.set_visible(False)
        self._guard.release()

    def _end_picker(self) -> None:
        """Close the picker's dialog, if one is open: its prompt is over."""
        cancel, self._cancel_picker = self._cancel_picker, None
        self._picking = False
        self._guard.picking = False
        if cancel is not None:
            cancel()

    def _first_frame(self, card: PromptCard) -> bool:
        if self._card is card and self._controller is not None:
            self._controller.mark_drawn(card.prompt)
        return False  # once

    def _disturbed(self) -> bool:
        if self._controller is not None:
            self._controller.disturbed()
        return False

    def _on_active_changed(self, window: Gtk.Window, _pspec: object) -> None:
        if window.is_active() and self._controller is not None:
            self._controller.reactivated()

    # --- drawing ---------------------------------------------------------------------------

    def _empty(self) -> None:
        child = self.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.remove(child)
            child = following

    def _rebuild(self, card: PromptCard) -> None:
        self._empty()
        self._buttons = []
        self.append(plain_label("Permission request", css_classes=[TITLE_CLASS]))
        source = f"From task {card.task}" + ("" if card.tool is None else f", tool {card.tool}")
        self.append(plain_label(source, wrap=True, css_classes=["caption", "dim-label"]))
        for line, detail in self._what(card):
            self.append(plain_label(line, wrap=True))
            if detail is not None:
                self.append(plain_label(display_text(detail), wrap=True, css_classes=["monospace"]))
        self.append(
            plain_label(
                "The assistant says (its own words, not checked):",
                css_classes=["caption", "dim-label"],
            )
        )
        # No quotation marks: the block, set apart by its rule and its italics under the label
        # above, is the quotation, so no character of the reason can close it.
        shown = display_text(card.reason) if card.reason else "(no reason given)"
        self.append(plain_label(shown, wrap=True, css_classes=[REASON_CLASS]))
        self.append(self._button_row(card))
        self.note = plain_label(
            "Answer with the keyboard or the mouse. No answer means Deny.",
            wrap=True,
            css_classes=["caption", "dim-label"],
        )
        self.append(self.note)

    @staticmethod
    def _what(card: PromptCard) -> list[tuple[str, str | None]]:
        """What is asked, from the card's structure: our sentence, then the data on its own line."""
        match card.kind:
            case "read":
                return [
                    ("The assistant asks to read this file or folder and all in it:", card.path)
                ]
            case "write":
                return [
                    ("The assistant asks to change this file or folder and all in it:", card.path)
                ]
            case "network":
                return [("The assistant asks for full network access.", None)]
            case "pick_file":
                return [("The assistant asks you to choose a file it may read.", None)]
            case "pick_folder":
                return [("The assistant asks you to choose a folder it may read.", None)]
            case _:
                assert_never(card.kind)

    def _button_row(self, card: PromptCard) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        deny = Gtk.Button(label="Deny")
        deny.add_css_class("destructive-action")
        deny.connect("clicked", lambda _button: self._deny_clicked(card.prompt))
        row.append(deny)
        self._deny = deny
        self._buttons.append(deny)
        for scope in card.scopes:
            label = _PICK_LABEL[scope] if card.picks else _SCOPE_LABEL[scope]
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _button, scope=scope: self._allow_clicked(card, scope))
            row.append(button)
            self._buttons.append(button)
        return row

    # --- the user ---------------------------------------------------------------------------

    def _deny_clicked(self, prompt: PromptId) -> None:
        if self._controller is not None:
            self._controller.choose(prompt, DenyDecision(kind="deny", reason="user_denied"))

    def _allow_clicked(self, card: PromptCard, scope: Scope) -> None:
        controller = self._controller
        if controller is None:
            return
        if not card.picks:
            controller.choose(card.prompt, AllowDecision(kind="allow", scope=scope, picked=None))
            return
        if self._picking or not controller.can_act(card.prompt):
            return  # not yet armed, or the picker is open already
        choose: Literal["file", "folder"] = "file" if card.kind == "pick_file" else "folder"
        self._picking = True
        self._guard.picking = True  # the dialog is a toplevel of ours, not a stranger
        self._cancel_picker = self._picker.pick(
            self._window, choose, lambda path: self._picked(card, scope, path)
        )

    def _picked(self, card: PromptCard, scope: Scope, path: str | None) -> None:
        if self._card is not card:
            return  # the prompt ended (timeout, withdrawn) while the dialog was open
        self._cancel_picker = None
        self._picking = False
        self._guard.picking = False
        self._guard.close_surfaces()  # the dialog is done: whatever else appeared meanwhile goes
        controller = self._controller
        if controller is None:
            return
        if path is None:
            self.note.set_text("Nothing was chosen. Choose again, or Deny.")
            return
        try:
            decision = AllowDecision(kind="allow", scope=scope, picked=path)
            decision.model_dump_json()  # a name that is not valid UTF-8 cannot be sent
        except (ValidationError, UnicodeEncodeError, ValueError):
            self.note.set_text("That name cannot be used. Choose another, or Deny.")
            return
        controller.choose(card.prompt, decision)
