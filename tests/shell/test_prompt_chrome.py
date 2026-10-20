"""The trusted prompt in the real window (S18, I5, I10, #53): chrome, focus, input, surfaces.

The window is the shell's own and is laid out and rendered by GTK; the controller behind the banner
is the test's, with a clock and timers it moves by hand and a picker with nobody at it, so that
what the user does is a click on a real button and what comes out is what the broker would be told.
"""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

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
from tumbleweed.shell.prompt_chrome import RESERVED_CLASSES
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
        (quoted,) = [line for line in shown if line.startswith("“")]
        assert quoted.endswith("”")
        assert "<b>Allow everything</b> &amp; _more_ Permission request long" in quoted
        assert "\n" not in quoted
        assert len(quoted) <= 305
        assert "…" in quoted  # cut
        # Every label of the prompt shows its text as text.
        for label in labels_in(banner):
            assert not label.get_use_markup()
            assert not label.get_use_underline()

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
