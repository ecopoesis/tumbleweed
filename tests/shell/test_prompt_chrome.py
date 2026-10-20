"""The trusted prompt in the real window (S18, I5, I10, #53): chrome, focus, input, surfaces.

The window is the shell's own and is laid out and rendered by GTK; the controller behind the banner
is the test's, with a clock and timers it moves by hand and a picker with nobody at it, so that
what the user does is a click on a real button and what comes out is what the broker would be told.
"""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from tests.shell.conftest import PromptShell
from tests.shell.gtk_support import (
    descendants,
    labels_in,
    pump,
    pump_until,
)
from tests.shell.prompt_support import network_card, pick_card, read_card
from tests.ui.gtk.hostile import hostile_spec
from tests.ui.gtk.test_no_surfaces import show as show_spec
from tests.ui.gtk.test_no_surfaces import surfaces_below, use_every_widget
from tests.ui.spec.support import Json, build
from tumbleweed.ipc.broker_messages import Scope
from tumbleweed.ipc.prompt_messages import AllowDecision, DenyDecision
from tumbleweed.shell.prompt_chrome import REASON_CLASS, RESERVED_CLASSES
from tumbleweed.shell.viewmodel.prompts import ARM_SECONDS, PromptCard
from tumbleweed.ui.spec import SpecError, parse_ui_spec

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk

pytestmark = pytest.mark.gtk

type Builder = Callable[[], PromptShell]


def buttons(widget: "Gtk.Widget") -> dict[str, "Gtk.Button"]:
    from tumbleweed.shell._gi import Gtk

    return {w.get_label() or "": w for w in descendants(widget) if isinstance(w, Gtk.Button)}


def texts(widget: "Gtk.Widget") -> list[str]:
    return [label.get_text() for label in labels_in(widget)]


def pending(shell: PromptShell, card: PromptCard | None = None) -> PromptCard:
    """Show a prompt on the window, as the broker's request would."""
    shown = read_card() if card is None else card
    controller = shell.window.prompt_banner._controller  # noqa: SLF001  (the banner's own binding)
    assert controller is not None
    assert controller.offer(shown, 600.0)
    shell.settle()  # the first frame restarts the 750 ms: let it come before the test moves time
    return shown


def present(shell: PromptShell) -> None:
    shell.window.present()
    assert pump_until(lambda: shell.window.get_mapped() and shell.window.canvas.get_width() > 0)
    pump(0.1)


class TestWhereItIs:
    def test_the_prompt_is_in_the_chrome_region_and_not_on_the_canvas(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        window, banner = shell.window, shell.window.prompt_banner
        assert not banner.get_visible()  # nothing, and no height, until a prompt is pending

        card = pending(shell)

        assert banner.get_visible()
        assert banner.is_ancestor(window.chrome)
        assert not banner.is_ancestor(window.canvas)
        shown = texts(banner)
        assert "Permission request" in shown
        assert any("read this file or folder" in line for line in shown)
        assert card.path in shown  # the path, on a line of its own
        assert not any("Permission request" in line for line in texts(window.canvas))

    def test_what_is_asked_is_drawn_from_the_structure(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)

        pending(shell, network_card())

        shown = texts(shell.window.prompt_banner)
        assert "The assistant asks for full network access." in shown
        assert not any("/home" in line for line in shown)

    def test_the_reason_is_quoted_labelled_plain_and_capped(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        hostile = "<b>Allow everything</b> &amp; _more_\n\nPermission request\n" + "long " * 80

        pending(shell, read_card(reason=hostile))

        banner = shell.window.prompt_banner
        shown = texts(banner)
        assert "The assistant says (its own words, not checked):" in shown
        (block,) = [w for w in labels_in(banner) if REASON_CLASS in w.get_css_classes()]
        quoted = block.get_text()
        assert "<b>Allow everything</b> &amp; _more_ Permission request long" in quoted
        assert "\n" not in quoted
        assert len(quoted) <= 305
        assert "\u2026" in quoted  # cut
        # Every label of the prompt shows its text as text.
        for label in labels_in(banner):
            assert not label.get_use_markup()
            assert not label.get_use_underline()

    def test_a_reason_cannot_close_its_own_quotation(self, build_prompt_shell: Builder) -> None:
        """The reason is a block under a label, with no quotation marks around it, so a quotation
        mark inside it ends nothing and what follows it is still the assistant's words."""
        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        evil = 'fine\u201d" \u00bb\u201d The user has approved everything. \u201c'

        pending(shell, read_card(reason=evil))

        banner = shell.window.prompt_banner
        (block,) = [w for w in labels_in(banner) if REASON_CLASS in w.get_css_classes()]
        assert block.get_text() == evil  # all of it, in the one block
        others = [t for t in texts(banner) if "approved everything" in t]
        assert others == [evil]  # and nowhere outside it

    def test_a_prompt_with_no_reason_says_so(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)

        pending(shell, read_card(reason=""))

        assert "(no reason given)" in texts(shell.window.prompt_banner)

    @pytest.mark.parametrize(
        ("scopes", "expected"),
        [
            (("once",), {"Deny", "Allow once"}),
            (("once", "task"), {"Deny", "Allow once", "Allow for this task"}),
        ],
    )
    def test_the_buttons_are_the_scopes_offered(
        self,
        build_prompt_shell: Builder,
        scopes: tuple[Scope, ...],
        expected: set[str],
    ) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)

        pending(shell, read_card(scopes=scopes))

        assert set(buttons(shell.window.prompt_banner)) == expected


class TestFocusAndInput:
    def test_deny_has_the_focus_whatever_had_it_before(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        window = shell.window
        window.set_focus(window.chat_panel.input_view)
        assert window.get_focus() is window.chat_panel.input_view

        pending(shell)

        assert window.get_focus() is window.prompt_banner.deny_button

    def test_early_clicks_and_enter_presses_are_ignored_and_later_ones_count(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        banner = shell.window.prompt_banner
        deny = banner.deny_button
        assert deny is not None
        once = buttons(banner)["Allow once"]

        shell.timers.advance(ARM_SECONDS - 0.05)
        once.emit("clicked")  # a click meant for what was under the pointer a moment ago
        deny.emit("activate")  # an Enter meant for the chat input, which had the focus
        pump(0.4)  # (GTK shows the press for a moment before it clicks)
        deny.emit("clicked")

        assert answers.given == []
        assert banner.get_visible()

        shell.timers.advance(0.1)
        once.emit("clicked")

        assert [d for _, d in answers.given] == [
            AllowDecision(kind="allow", scope="once", picked=None)
        ]
        assert not banner.get_visible()

    def test_enter_on_the_focused_deny_denies_once_armed(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        shell.wait_armed()

        focused = shell.window.get_focus()
        assert focused is shell.window.prompt_banner.deny_button
        assert focused is not None
        focused.emit("activate")  # Enter on the focused button
        assert pump_until(lambda: bool(answers.given))

        assert [d for _, d in answers.given] == [DenyDecision(kind="deny", reason="user_denied")]

    def test_the_buttons_look_unavailable_until_they_are_armed(
        self, build_prompt_shell: Builder
    ) -> None:
        from tumbleweed.shell.prompt_chrome import DISARMED_CLASS

        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        pending(shell)
        every = list(buttons(shell.window.prompt_banner).values())

        assert all(DISARMED_CLASS in b.get_css_classes() for b in every)
        shell.wait_armed()
        assert not any(DISARMED_CLASS in b.get_css_classes() for b in every)

    def test_one_prompt_at_a_time_and_the_next_starts_disarmed_with_deny_focused(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        first = pending(shell, read_card(reason="first"))
        second = pending(shell, read_card(reason="second"))
        banner = shell.window.prompt_banner
        assert any("first" in line for line in texts(banner))
        assert not any("second" in line for line in texts(banner))
        shell.wait_armed()

        buttons(banner)["Allow once"].emit("clicked")
        # The click that answered the first lands again, on the second, at once.
        deny = banner.deny_button
        assert deny is not None
        deny.emit("clicked")

        assert [p for p, _ in answers.given] == [first.prompt]
        assert any("second" in line for line in texts(banner))
        assert shell.window.get_focus() is deny
        shell.wait_armed()
        deny.emit("clicked")
        assert [p for p, _ in answers.given] == [first.prompt, second.prompt]

    def test_a_deadline_with_no_answer_takes_the_prompt_down_with_a_denial(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        controller, answers = shell.own_controller()
        present(shell)
        card = read_card()
        assert controller.offer(card, 30.0)

        shell.timers.advance(31)

        assert [(p, d) for p, d in answers.given] == [
            (card.prompt, DenyDecision(kind="deny", reason="prompt_timeout"))
        ]
        assert not shell.window.prompt_banner.get_visible()


class TestTheRestOfTheWindowWhilePending:
    def test_the_canvas_and_the_side_panel_are_insensitive_and_come_back(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        window = shell.window
        window.set_focus(window.chat_panel.input_view)
        assert window.canvas.get_sensitive()

        card = pending(shell)

        assert not window.canvas.get_sensitive()
        assert not window.chat_panel.is_sensitive()  # (its own box is locked)
        assert window.chrome.is_sensitive()
        shell.wait_armed()
        buttons(window.prompt_banner)["Allow once"].emit("clicked")
        assert card is not None
        assert window.canvas.get_sensitive()
        assert window.chat_panel.is_sensitive()
        assert pump_until(lambda: window.get_focus() is window.chat_panel.input_view)

    def test_they_stay_locked_between_two_prompts(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        window = shell.window
        pending(shell)
        pending(shell)
        shell.wait_armed()

        window.prompt_banner.deny_button.emit("clicked")  # type: ignore[union-attr]  # it is there

        assert not window.canvas.get_sensitive()  # the second prompt is up: no gap to click in

    def test_a_window_closed_with_a_prompt_pending_comes_down_cleanly(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)

        shell.window.shutdown()

        assert answers.given == []  # nobody was answered for: the broker sees the connection end
        assert shell.window.canvas.get_sensitive()


class FakeEvent:
    """Stands in for a `Gdk.Event`: the controller only asks it what it is, its button and its
    touch sequence."""

    def __init__(self, kind: object, button: int = 1, sequence: object = None) -> None:
        self._kind, self._button, self._sequence = kind, button, sequence

    def get_event_type(self) -> object:
        return self._kind

    def get_button(self) -> int:
        return self._button

    def get_event_sequence(self) -> object:
        return self._sequence


class TestInputsThatAreNotTheUsers:
    """The window reports what goes down and up to the prompt's controller, which holds the arming
    invariant (`viewmodel.prompts`, `test_prompt_arming_model`): a stream of clicks aimed at where
    "Allow" appears never finds it armed, a press held from before ends in no click, and so on."""

    @staticmethod
    def down(shell: PromptShell, button: int = 1) -> None:
        from tumbleweed.shell._gi import Gdk

        shell.window.prompt_banner.pointer_event(Gdk.EventType.BUTTON_PRESS, button)

    @staticmethod
    def up(shell: PromptShell, button: int = 1) -> None:
        from tumbleweed.shell._gi import Gdk

        shell.window.prompt_banner.pointer_event(Gdk.EventType.BUTTON_RELEASE, button)

    @classmethod
    def press(cls, shell: PromptShell, button: int = 1) -> None:
        cls.down(shell, button)
        cls.up(shell, button)

    @staticmethod
    def keys(window: "Gtk.Window") -> "list[Gtk.EventControllerKey]":
        from tumbleweed.shell._gi import Gtk

        found = [
            c
            for c in window.observe_controllers()
            if isinstance(c, Gtk.EventControllerKey)
            and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
            and c.get_widget() is window
        ]
        assert found
        return found

    @classmethod
    def key_down(cls, window: "Gtk.Window", keycode: int = 38) -> bool:
        from tumbleweed.shell._gi import Gdk

        return any(
            bool(k.emit("key-pressed", Gdk.KEY_a, keycode, Gdk.ModifierType(0)))
            for k in cls.keys(window)
        )

    @classmethod
    def key_up(cls, window: "Gtk.Window", keycode: int = 38) -> None:
        from tumbleweed.shell._gi import Gdk

        for k in cls.keys(window):
            k.emit("key-released", Gdk.KEY_a, keycode, Gdk.ModifierType(0))

    def test_the_window_has_the_controllers_that_report_inputs_and_no_gesture_to_miscount_them(
        self, build_prompt_shell: Builder
    ) -> None:
        """A gesture's "stopped" fires from the double-click timer during any hold of 400 ms: it
        is not a release, so none of ours is a gesture."""
        from tumbleweed.shell._gi import Gtk

        window = build_prompt_shell().window
        controllers = list(window.observe_controllers())

        capturing = [
            c
            for c in controllers
            if isinstance(c, Gtk.Gesture)
            and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        ]
        assert capturing == []  # (the window has gestures of its own, in the bubble phase)
        legacy = [
            c
            for c in controllers
            if isinstance(c, Gtk.EventControllerLegacy)
            and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        ]
        assert legacy  # the prompt's, which sees every pointer event before its target does

    def test_a_stream_of_presses_never_arms_the_prompt_until_it_stops(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        once = buttons(shell.window.prompt_banner)["Allow once"]

        for _ in range(10):  # 2 s of clicks, 200 ms apart: press, release, the button's click
            shell.timers.advance(0.2)
            self.press(shell)
            once.emit("clicked")
        assert answers.given == []
        assert "was ignored" in shell.window.prompt_banner.note.get_text()  # never silent

        shell.timers.advance(ARM_SECONDS)
        self.press(shell)  # the user's own click: armed already
        once.emit("clicked")
        assert len(answers.given) == 1

    def test_a_press_held_past_400_ms_and_past_the_delay_ends_in_no_click(
        self, build_prompt_shell: Builder
    ) -> None:
        """Nothing times a hold out: it is counted until its release event, however long."""
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        once = buttons(shell.window.prompt_banner)["Allow once"]

        shell.timers.advance(0.1)
        self.down(shell)  # lands on the button where it has just appeared ...
        pump(0.5)  # (real time: longer than GTK's double-click time)
        shell.timers.advance(0.8)  # ... and is held until the prompt takes answers
        self.up(shell)
        once.emit("clicked")  # the release
        assert answers.given == []

        self.press(shell)  # the user's own, deliberate press
        once.emit("clicked")
        assert len(answers.given) == 1

    def test_enter_on_the_focused_deny_during_a_held_press_is_a_deny(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        once = buttons(shell.window.prompt_banner)["Allow once"]
        deny = shell.window.prompt_banner.deny_button
        assert deny is not None

        shell.timers.advance(0.1)
        self.down(shell)  # held on Allow from before the prompt was armed
        shell.timers.advance(0.8)
        self.key_down(shell.window, 36)  # Enter, with the focus on Deny
        deny.emit("clicked")
        self.key_up(shell.window, 36)
        self.up(shell)
        once.emit("clicked")  # (the prompt is gone: nothing answers this)

        assert [d.kind for _, d in answers.given] == ["deny"]

    def test_a_key_during_the_hold_does_not_end_it(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        once = buttons(shell.window.prompt_banner)["Allow once"]

        shell.timers.advance(0.1)
        self.down(shell)
        shell.timers.advance(0.8)
        self.key_down(shell.window)  # a letter, once armed ("hold and type the word")
        self.key_up(shell.window)
        self.up(shell)
        once.emit("clicked")

        assert answers.given == []

    def test_a_press_that_ended_in_no_click_does_not_cost_the_next_one(
        self, build_prompt_shell: Builder
    ) -> None:
        """Only true if the release is seen: without it the button stays "held"."""
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        once = buttons(shell.window.prompt_banner)["Allow once"]

        shell.timers.advance(0.1)
        self.press(shell)  # on something else, released: no click on a button
        shell.timers.advance(ARM_SECONDS)
        self.press(shell)
        once.emit("clicked")

        assert len(answers.given) == 1

    def test_a_touch_that_is_cancelled_is_over_without_a_click(
        self, build_prompt_shell: Builder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        banner = shell.window.prompt_banner
        once = buttons(banner)["Allow once"]

        shell.timers.advance(0.1)
        banner.pointer_event(Gdk.EventType.TOUCH_BEGIN, 1000)
        banner.pointer_event(Gdk.EventType.TOUCH_CANCEL, 1000)
        shell.timers.advance(ARM_SECONDS)
        self.press(shell)
        once.emit("clicked")

        assert len(answers.given) == 1

    def test_two_fingers_are_two_inputs_of_their_own(self, build_prompt_shell: Builder) -> None:
        """Each touch has the identity of its own event sequence, so one finger coming up does not
        make the other's end look like a release with no record."""
        from tumbleweed.shell._gi import Gdk

        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        shell.wait_armed()
        banner = shell.window.prompt_banner
        once = buttons(banner)["Allow once"]
        first, second = object(), object()

        for kind, sequence in (
            (Gdk.EventType.TOUCH_BEGIN, first),
            (Gdk.EventType.TOUCH_BEGIN, second),
            (Gdk.EventType.TOUCH_END, first),
        ):
            banner._on_pointer_event(None, FakeEvent(kind, sequence=sequence))  # type: ignore[arg-type]  # noqa: SLF001  (a fake event)
        once.emit("clicked")  # the tap of the first finger

        assert len(answers.given) == 1

    def test_the_window_events_are_translated_to_inputs(self, build_prompt_shell: Builder) -> None:
        """A Gdk event to a button or touch going down or up (a fake event stands in for it)."""
        from tumbleweed.shell._gi import Gdk

        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        banner = shell.window.prompt_banner
        once = buttons(banner)["Allow once"]

        def event(kind: "Gdk.EventType", button: int = 1) -> None:
            banner._on_pointer_event(None, FakeEvent(kind, button=button))  # type: ignore[arg-type]  # noqa: SLF001

        shell.timers.advance(0.1)
        event(Gdk.EventType.BUTTON_PRESS)  # disarmed
        event(Gdk.EventType.BUTTON_RELEASE)
        once.emit("clicked")
        assert answers.given == []  # tainted: the press began before the prompt took answers

        shell.timers.advance(ARM_SECONDS)
        event(Gdk.EventType.BUTTON_PRESS, 3)  # the right button, armed
        event(Gdk.EventType.BUTTON_RELEASE, 3)
        once.emit("clicked")
        assert len(answers.given) == 1

    def test_a_release_is_forgotten_when_the_main_loop_is_idle_again(
        self, build_prompt_shell: Builder
    ) -> None:
        """A tainted release that started no click must not refuse a later activation (a timer's)
        that no input of its own produced."""
        shell = build_prompt_shell()
        controller, answers = shell.own_controller()
        present(shell)
        card = pending(shell)
        shell.timers.advance(0.1)
        self.press(shell, 2)  # tainted, and no button clicked
        shell.timers.advance(ARM_SECONDS)

        assert pump_until(lambda: True)  # the idle callback runs
        pump(0.05)
        assert controller.choose(
            card.prompt, AllowDecision(kind="allow", scope="once", picked=None)
        )
        assert len(answers.given) == 1

    def test_a_key_that_ended_in_no_activation_does_not_cost_the_next_one(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        once = buttons(shell.window.prompt_banner)["Allow once"]

        shell.timers.advance(0.1)
        self.key_down(shell.window)
        self.key_up(shell.window)  # (without the release the key stays "held")
        shell.timers.advance(ARM_SECONDS)
        self.key_down(shell.window, 36)
        once.emit("clicked")  # Enter on the focused button, deliberately

        assert len(answers.given) == 1

    def test_activating_the_window_starts_the_delay_again_and_dims_the_buttons(
        self, build_prompt_shell: Builder
    ) -> None:
        from tumbleweed.shell.prompt_chrome import DISARMED_CLASS

        shell = build_prompt_shell()
        controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        shell.wait_armed()
        once = buttons(shell.window.prompt_banner)["Allow once"]
        assert controller.armed()

        with patch.object(shell.window, "is_active", return_value=True):
            shell.window.notify("is-active")  # the window comes to the front
        pump(0.05)

        assert not controller.armed()
        assert DISARMED_CLASS in once.get_css_classes()
        once.emit("clicked")
        assert answers.given == []

    def test_losing_the_focus_forgets_the_keys_that_were_down(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        once = buttons(shell.window.prompt_banner)["Allow once"]
        shell.timers.advance(0.1)
        self.key_down(shell.window, 36)  # its release goes to another window

        with patch.object(shell.window, "is_active", return_value=False):
            shell.window.notify("is-active")
        with patch.object(shell.window, "is_active", return_value=True):
            shell.window.notify("is-active")
        pump(0.05)
        shell.timers.advance(ARM_SECONDS)
        self.press(shell)
        once.emit("clicked")

        assert len(answers.given) == 1

    def test_a_press_held_across_a_focus_loss_still_ends_in_no_click(
        self, build_prompt_shell: Builder
    ) -> None:
        """GTK does not cancel a pointer press when the window is deactivated: the release still
        arrives, with the press recorded as tainted (and, unrecorded, as tainted too)."""
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        once = buttons(shell.window.prompt_banner)["Allow once"]
        shell.timers.advance(0.1)
        self.down(shell)  # on Allow, while disarmed

        with patch.object(shell.window, "is_active", return_value=False):
            shell.window.notify("is-active")
        with patch.object(shell.window, "is_active", return_value=True):
            shell.window.notify("is-active")
        pump(0.05)
        shell.timers.advance(ARM_SECONDS)
        self.up(shell)  # ... and released on it, after the prompt took answers
        once.emit("clicked")

        assert answers.given == []

    def test_the_prompts_controllers_run_before_the_f12_shortcut_so_they_see_every_key(
        self, build_prompt_shell: Builder
    ) -> None:
        """The first controller of the window runs first, and one that handles an event (a
        shortcut) ends the run: the prompt's must come before the capture shortcuts."""
        from tumbleweed.shell._gi import Gtk

        order = list(build_prompt_shell().window.observe_controllers())
        capture = Gtk.PropagationPhase.CAPTURE
        key = next(
            i
            for i, c in enumerate(order)
            if isinstance(c, Gtk.EventControllerKey) and c.get_propagation_phase() == capture
        )
        pointer = next(
            i
            for i, c in enumerate(order)
            if isinstance(c, Gtk.EventControllerLegacy) and c.get_propagation_phase() == capture
        )
        shortcuts = [
            i
            for i, c in enumerate(order)
            if isinstance(c, Gtk.ShortcutController) and c.get_propagation_phase() == capture
        ]

        assert shortcuts  # (F12's is one)
        assert max(key, pointer) < min(shortcuts)

    def test_keys_start_the_delay_again_and_are_not_consumed(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell)
        deny = shell.window.prompt_banner.deny_button
        assert deny is not None

        for _ in range(6):
            shell.timers.advance(0.2)
            assert not self.key_down(shell.window)  # watched, not taken: the key goes on
            self.key_up(shell.window)
            deny.emit("clicked")

        assert answers.given == []

    def test_the_first_frame_is_what_starts_the_delay(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        controller, _answers = shell.own_controller()
        present(shell)
        card = read_card()

        with patch.object(controller, "mark_drawn", wraps=controller.mark_drawn) as drawn:
            assert controller.offer(card, 600.0)
            assert pump_until(lambda: drawn.call_count == 1)

        drawn.assert_called_once_with(card.prompt)


class TestTheTerminalDuringAPrompt:
    def test_the_terminal_and_the_status_strip_take_no_input_and_f12_does_nothing(
        self, build_prompt_shell: Builder
    ) -> None:
        from tests.shell.test_terminal_pane import press_f12

        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        window = shell.window
        pending(shell)

        with patch.object(window.terminal_pane, "toggle") as toggle:
            consumed = press_f12(shell.shell)
            window.status_strip.terminal_button.emit("clicked")

        assert consumed  # the key is not left for VTE to get
        toggle.assert_not_called()  # and nothing was opened
        assert not window.terminal_pane.is_sensitive()
        assert not window.status_strip.terminal_button.is_sensitive()
        assert window.get_focus() is window.prompt_banner.deny_button

    def test_after_the_prompt_the_terminal_opens_again(self, build_prompt_shell: Builder) -> None:
        from tests.shell.test_terminal_pane import press_f12

        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        pending(shell)
        shell.wait_armed()
        shell.window.prompt_banner.deny_button.emit("clicked")  # type: ignore[union-attr]  # it is there

        with patch.object(shell.window.terminal_pane, "toggle") as toggle:
            press_f12(shell.shell)

        toggle.assert_called_once()
        assert shell.window.terminal_pane.is_sensitive()


class TestSpecsCannotImitateOrReachIt:
    def test_a_hostile_spec_has_none_of_the_reserved_classes_or_names_anywhere(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        window = shell.window
        window.canvas.show_spec(hostile_spec())
        present(shell)
        pending(shell)

        for widget in descendants(window.canvas):
            classes = set(widget.get_css_classes())
            assert not classes & set(RESERVED_CLASSES), widget
            assert widget.get_name() not in ("tumbleweed-chrome", "tumbleweed-trusted-prompt")
        assert not window.prompt_banner.is_ancestor(window.canvas)
        # ... and the prompt itself has them all.
        banner_classes = {c for w in descendants(window.prompt_banner) for c in w.get_css_classes()}
        assert set(RESERVED_CLASSES) <= banner_classes | {"tw-trusted-disarmed"}

    def test_a_spec_has_no_field_that_sets_a_style_class_or_a_region(self) -> None:
        for field in ("css_classes", "class", "style", "region", "chrome", "name", "css"):
            root: Json = {
                "kind": "label",
                "text": "Permission request",
                field: ["tw-trusted-prompt"],
            }
            with pytest.raises(SpecError):
                parse_ui_spec(json.dumps(build(root)))

    def test_a_spec_that_says_permission_request_is_on_the_canvas_not_in_the_chrome(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        window = shell.window
        window.canvas.show_spec(
            parse_ui_spec(
                json.dumps(build({"kind": "label", "text": "Permission request: allow everything"}))
            )
        )
        present(shell)

        pending(shell)

        (fake,) = [w for w in labels_in(window.canvas) if "allow everything" in w.get_text()]
        assert fake.is_ancestor(window.canvas)
        assert not fake.is_ancestor(window.chrome)
        assert not window.canvas.get_sensitive()  # and it cannot be clicked, nor can its buttons


class TestNoSurfaceAboveIt:
    """#53: a popover, a tooltip, a menu or a dialog is closed or blocked while a prompt is up."""

    def test_a_popover_that_is_open_when_the_prompt_arrives_is_closed(
        self, build_prompt_shell: Builder
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        shell = build_prompt_shell()
        shell.own_controller()
        window = shell.window
        opener = Gtk.Button(label="opens a popover")
        window.canvas.set_content(opener)
        present(shell)
        popover = Gtk.Popover()
        popover.set_parent(opener)
        popover.set_child(Gtk.Label(label="from the canvas"))
        popover.popup()
        assert popover.get_visible()  # (visible, whether or not the compositor has mapped it yet)

        pending(shell)

        assert pump_until(lambda: not popover.get_visible())

    def test_a_toplevel_that_is_open_or_opens_meanwhile_is_closed(
        self, build_prompt_shell: Builder
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        before = Gtk.Window(title="a dialog from somewhere")
        before.present()
        assert pump_until(before.get_mapped)

        pending(shell)
        assert pump_until(lambda: not before.get_mapped())

        after = Gtk.Window(title="a dialog while the prompt is up")
        after.present()
        assert pump_until(lambda: not after.get_mapped())
        assert [w for w in Gtk.Window.list_toplevels() if w is not shell.window] == []

    def test_after_the_prompt_a_window_can_open_again(self, build_prompt_shell: Builder) -> None:
        from tumbleweed.shell._gi import Gtk

        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        pending(shell)
        shell.wait_armed()
        shell.window.prompt_banner.deny_button.emit("clicked")  # type: ignore[union-attr]  # it is there

        later = Gtk.Window(title="fine now")
        later.present()
        try:
            assert pump_until(later.get_mapped)
            pump(0.1)
            assert later.get_mapped()
        finally:
            later.destroy()

    @pytest.mark.parametrize("name", ["tip_calculator", "photo_adjuster", "csv_viewer", "hostile"])
    def test_using_every_widget_of_a_tool_under_a_pending_prompt_opens_no_surface(
        self, build_prompt_shell: Builder, name: str
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        shell = build_prompt_shell()
        shell.own_controller()
        show_spec(shell.shell, name)
        pending(shell)

        use_every_widget(shell.shell)

        assert surfaces_below(shell.window) == []
        assert [w for w in Gtk.Window.list_toplevels() if w is not shell.window] == []
        # and nothing in the window has a tooltip to show above the prompt
        assert not [w for w in descendants(shell.window) if w.get_has_tooltip()]


class TestThePicker:
    def test_choosing_in_the_picker_is_the_answer_at_the_scope_of_the_button(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        card = pending(shell, pick_card("folder"))
        banner = shell.window.prompt_banner
        assert any("choose a folder it may read" in line for line in texts(banner))
        shell.wait_armed()

        buttons(banner)["Choose, allow for this task…"].emit("clicked")
        assert shell.picker.asked == ["folder"]
        assert answers.given == []  # nothing is decided until the user has chosen
        shell.picker.finish("/home/u/Documents")

        assert answers.given == [
            (card.prompt, AllowDecision(kind="allow", scope="task", picked="/home/u/Documents"))
        ]
        assert not banner.get_visible()

    def test_a_picker_prompt_is_not_opened_by_an_early_click(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        pending(shell, pick_card("file"))

        buttons(shell.window.prompt_banner)["Choose, allow once…"].emit("clicked")

        assert shell.picker.asked == []

    def test_a_picker_whose_prompt_ends_is_closed_and_its_late_choice_is_no_answer(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        controller, answers = shell.own_controller()
        present(shell)
        card = pending(shell, pick_card("file"))
        shell.wait_armed()
        buttons(shell.window.prompt_banner)["Choose, allow once\u2026"].emit("clicked")
        assert shell.picker.cancelled == 0

        controller.withdraw(card.prompt)

        assert shell.picker.cancelled == 1  # the dialog is closed with its prompt
        shell.picker.finish("/home/u/Documents")  # a late choice, from a dialog that was too slow
        assert answers.given == []

    def test_coming_back_from_the_picker_does_not_drop_the_choice(
        self, build_prompt_shell: Builder
    ) -> None:
        """The window loses the focus to the dialog and gets it back when it closes, maybe before
        the dialog's own answer arrives: that activation is not a stranger's click."""
        shell = build_prompt_shell()
        controller, answers = shell.own_controller()
        present(shell)
        card = pending(shell, pick_card("folder"))
        shell.wait_armed()
        buttons(shell.window.prompt_banner)["Choose, allow once\u2026"].emit("clicked")

        with patch.object(shell.window, "is_active", return_value=True):
            shell.window.notify("is-active")  # back from the dialog ...

        assert controller.armed()  # coming back from the dialog did not disarm the prompt
        shell.picker.finish("/home/u/Documents")
        assert answers.given == [
            (card.prompt, AllowDecision(kind="allow", scope="once", picked="/home/u/Documents"))
        ]

    def test_a_choice_in_the_dialog_is_taken_whatever_the_window_saw_meanwhile(
        self, build_prompt_shell: Builder
    ) -> None:
        """The click on Choose passed the arming check and opened the dialog; the choice is made
        there, so a stray release or a restart of the delay in the window does not refuse it."""
        shell = build_prompt_shell()
        controller, answers = shell.own_controller()
        present(shell)
        card = pending(shell, pick_card("file"))
        shell.wait_armed()
        buttons(shell.window.prompt_banner)["Choose, allow once\u2026"].emit("clicked")
        TestInputsThatAreNotTheUsers.up(shell, 3)  # a release from the dialog, with no press seen
        controller.reactivated()  # and something restarted the delay meanwhile

        shell.picker.finish("/home/u/Documents")

        assert [(p, d.kind) for p, d in answers.given] == [(card.prompt, "allow")]

    def test_a_dismissed_picker_leaves_the_prompt_open(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell, pick_card("file"))
        banner = shell.window.prompt_banner
        shell.wait_armed()
        button = buttons(banner)["Choose, allow once…"]
        button.emit("clicked")
        button.emit("clicked")  # the dialog is open: a second one is not
        assert shell.picker.asked == ["file"]

        shell.picker.finish(None)

        assert answers.given == []
        assert banner.get_visible()
        assert "Nothing was chosen" in banner.note.get_text()
        button.emit("clicked")  # and it can be opened again
        assert shell.picker.asked == ["file", "file"]

    def test_a_name_that_cannot_be_sent_is_not_a_choice(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        _controller, answers = shell.own_controller()
        present(shell)
        pending(shell, pick_card("file"))
        shell.wait_armed()
        buttons(shell.window.prompt_banner)["Choose, allow once…"].emit("clicked")

        shell.picker.finish("/home/u/\udcff-not-utf8")

        assert answers.given == []
        assert "cannot be used" in shell.window.prompt_banner.note.get_text()

    def test_a_prompt_that_ends_while_the_picker_is_open_ignores_its_result(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        controller, answers = shell.own_controller()
        present(shell)
        card = pending(shell, pick_card("file"))
        shell.wait_armed()
        buttons(shell.window.prompt_banner)["Choose, allow once…"].emit("clicked")

        controller.withdraw(card.prompt)  # the broker gave up
        shell.picker.finish("/home/u/Documents")

        assert answers.given == []
        assert not shell.window.prompt_banner.get_visible()
        assert not shell.window.prompt_guard.picking

    def test_the_pickers_own_dialog_is_not_closed_as_a_stranger(
        self, build_prompt_shell: Builder
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        shell = build_prompt_shell()
        shell.own_controller()
        present(shell)
        pending(shell, pick_card("file"))
        shell.wait_armed()
        buttons(shell.window.prompt_banner)["Choose, allow once…"].emit("clicked")
        assert shell.window.prompt_guard.picking
        dialog = Gtk.Window(title="the picker's dialog")

        dialog.present()
        pump(0.2)
        assert dialog.get_mapped()  # let through while the picker is open
        shell.picker.finish(None)

        assert pump_until(lambda: not dialog.get_mapped())  # and gone with it
