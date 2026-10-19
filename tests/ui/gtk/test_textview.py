"""The `text_view` widget: multi-line plain text, read-only or bound both ways, bounded (I4, I9).

The properties that matter: what is shown is exactly the source text (nothing is inserted, so a
selection copies the source: #63), what the user types is held to the cap, the text counts in the
tool's budget, an edit never writes a cut text over the state, and a patch never looks like the
user.
"""

import time
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from tests.shell.gtk_support import Shell, pump
from tests.ui.gtk.support import of_type, show_in_window, spec_of
from tests.ui.runtime.support import number, set_handler, text
from tests.ui.spec.support import Json, binary, lit_int, ref
from tumbleweed.ui.gtk.budget import CUT_MARK, MAX_SHOWN_PER_TEXT, NOT_SHOWN
from tumbleweed.ui.runtime import Dispatch, Origin, UiEvent, Value
from tumbleweed.ui.spec.names import StateKey

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool
    from tumbleweed.ui.spec import UiSpec

pytestmark = pytest.mark.gtk

ZWSP = chr(0x200B)


def spec_for(
    default: str = "",
    *,
    editable: bool = False,
    monospace: bool = False,
    max_length: int = 65536,
    before: list[Json] | None = None,
    pad_chars: int = MAX_SHOWN_PER_TEXT,
    on_change: str | None = None,
) -> "UiSpec":
    view: Json = {"kind": "text_view", "bind": "s", "editable": editable, "monospace": monospace}
    handlers: list[Json] = []
    state: list[Json] = [text("s", default, max_length=max_length), number("n", 0)]
    if on_change is not None:  # the handler counts how often it was fired
        view["on_change"] = on_change
        handlers.append(set_handler(on_change, n=binary("add", ref("n"), lit_int(1))))
    if before:
        state.append(text("pad", "p" * pad_chars, max_length=65536))
    return spec_of({"kind": "box", "children": [*(before or []), view]}, state, handlers)


def tool_with(*args: object, **kwargs: object) -> "RenderedTool":
    from tumbleweed.ui.gtk import RenderedTool

    return RenderedTool(spec_for(*args, **kwargs))  # type: ignore[arg-type]  # forwards spec_for's


def view_of(tool: "RenderedTool") -> "Gtk.TextView":
    from tumbleweed.shell._gi import Gtk

    (view,) = of_type(tool, Gtk.TextView)
    return view


def shown(view: "Gtk.TextView") -> str:
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


def held(tool: "RenderedTool") -> object:
    return tool.runtime.store.value(StateKey("s"))


class TestReadOnly:
    def test_it_shows_the_text_and_cannot_be_edited(self) -> None:
        view = view_of(tool_with("line one\nline two"))

        assert shown(view) == "line one\nline two"
        assert view.get_editable() is False
        assert view.get_accepts_tab() is False  # Tab moves the focus on

    def test_monospace_is_a_property_of_the_component(self) -> None:
        assert view_of(tool_with("x", monospace=True)).get_monospace() is True
        assert view_of(tool_with("x")).get_monospace() is False

    def test_a_patch_updates_it_and_is_not_the_user(self) -> None:
        tool = tool_with("before")
        view = view_of(tool)
        origins: list[Origin] = []
        tool.runtime.store.subscribe(lambda change: origins.append(change.origin))

        tool.runtime.patch_state({"s": "after"})
        tool.flush()

        assert shown(view) == "after"
        assert Origin.USER not in origins

    def test_markup_and_mnemonics_are_shown_as_they_are(self) -> None:
        raw = "<b>bold</b> _under <span foreground='red'>x</span> &amp; <a href='file:///x'>l</a>"

        assert shown(view_of(tool_with(raw))) == raw

    def test_an_update_that_changes_nothing_leaves_the_view_alone(self) -> None:
        tool = tool_with("same")
        view = view_of(tool)
        buffer = view.get_buffer()
        buffer.select_range(buffer.get_start_iter(), buffer.get_end_iter())

        tool.runtime.patch_state({"s": "same"})
        tool.flush()

        assert buffer.get_has_selection()  # not rewritten, so the selection is still there

    def test_what_is_put_in_is_no_edit_to_undo(self) -> None:
        tool = tool_with("first")
        view = view_of(tool)

        tool.runtime.patch_state({"s": "second"})
        tool.flush()

        assert view.get_buffer().get_can_undo() is False


class TestTheTextIsShownAsItIs:
    SOURCE = (
        "plain words\n"
        + "y" * 600  # a run with no break opportunity: a label would get U+200B in it
        + "\n"
        + "e"
        + chr(0x301) * 40  # one grapheme cluster of 41 code points
        + "\n"
        + "漢" * 300
        + f"\n{ZWSP}a zero-width space of the source's own"
    )

    def test_nothing_is_inserted_so_a_selection_is_the_source_text(self) -> None:
        view = view_of(tool_with(self.SOURCE))
        buffer = view.get_buffer()

        buffer.select_range(buffer.get_start_iter(), buffer.get_end_iter())
        first, last = buffer.get_selection_bounds()  # type: ignore[misc]  # stub: a pair or ()
        selected = buffer.get_text(first, last, False)

        assert shown(view) == self.SOURCE
        assert selected == self.SOURCE  # what copy and the primary selection read
        assert self.SOURCE.count(ZWSP) == 1  # only the one the source had

    def test_it_wraps_at_any_character_instead(self) -> None:
        from tumbleweed.shell._gi import Gtk

        assert view_of(tool_with("x")).get_wrap_mode() == Gtk.WrapMode.WORD_CHAR

    def test_a_hostile_full_size_text_is_laid_out_in_bounded_time(self, echo_shell: Shell) -> None:
        # The label needs the display transform because laying out an unbroken run or one huge
        # cluster takes a time that grows faster than the text. A text view lays out line by
        # line; the whole text at its cap is shown here, in a window, and must not take long.
        hostile = ("각" * 4000 + "y" * 4000)[:MAX_SHOWN_PER_TEXT]
        cluster = "e" + chr(0x301) * (MAX_SHOWN_PER_TEXT - 1)
        for source in (hostile, cluster, "z" * MAX_SHOWN_PER_TEXT):
            started = time.monotonic()
            tool = show_in_window(echo_shell, spec_for(source))
            pump(0.2)

            assert shown(view_of(tool)) == source
            assert time.monotonic() - started < 10


class TestBounds:
    def test_a_text_over_the_cap_is_cut_and_marked_and_read_only(self) -> None:
        source = "w" * (MAX_SHOWN_PER_TEXT + 1)
        tool = tool_with(source, editable=True)
        view = view_of(tool)

        assert shown(view) == "w" * MAX_SHOWN_PER_TEXT + CUT_MARK
        assert view.get_editable() is False  # an edit would write the cut text over the state
        assert "warning" in view.get_css_classes()
        assert held(tool) == source  # the state keeps all of it

    def test_a_text_of_exactly_the_cap_is_whole_and_editable(self) -> None:
        source = "w" * MAX_SHOWN_PER_TEXT
        view = view_of(tool_with(source, editable=True))

        assert shown(view) == source
        assert view.get_editable() is True
        assert "warning" not in view.get_css_classes()

    def test_it_counts_in_the_tools_budget(self) -> None:
        tool = tool_with("hello")

        assert tool.shown_text_total == len("hello")

    def test_what_does_not_fit_is_a_line_saying_so_and_the_text_comes_when_room_does(self) -> None:
        pads: list[Json] = [{"kind": "label", "bind": "pad"}] * 8  # 8 * 8,192: the whole budget
        tool = tool_with("the text", editable=True, before=pads)
        view = view_of(tool)

        assert shown(view) == NOT_SHOWN
        assert view.get_editable() is False  # not an edit that would overwrite the value
        assert held(tool) == "the text"
        tool.runtime.patch_state({"pad": ""})
        tool.flush()
        tool.flush()

        assert shown(view) == "the text"
        assert view.get_editable() is True

    def test_closing_the_tool_gives_the_budget_back(self) -> None:
        tool = tool_with("hello")

        tool.close()

        assert tool.shown_text_total == 0

    def test_a_stale_entry_says_so_instead_of_showing_the_old_text(self) -> None:
        tool = tool_with("old text", editable=True)
        view = view_of(tool)

        with mock.patch.object(tool.runtime.store, "is_stale", return_value=True):
            tool.runtime.patch_state({"s": "new text"})
            tool.flush()

        assert shown(view) == "Out of date"
        assert view.get_editable() is False
        assert "error" in view.get_css_classes()
        tool.runtime.patch_state({"s": "newer text"})
        tool.flush()
        assert shown(view) == "newer text"
        assert "error" not in view.get_css_classes()


class TestEditing:
    def test_typing_writes_the_state_as_the_user(self) -> None:
        tool = tool_with("", editable=True)
        view = view_of(tool)
        origins: list[Origin] = []
        tool.runtime.store.subscribe(lambda change: origins.append(change.origin))

        view.get_buffer().insert_at_cursor("typed text\nsecond line")

        assert held(tool) == "typed text\nsecond line"
        assert origins == [Origin.USER]

    def test_an_edit_fires_the_handler_of_on_change_and_a_patch_does_not(self) -> None:
        tool = tool_with("", editable=True, on_change="edited")
        n = StateKey("n")

        view_of(tool).get_buffer().insert_at_cursor("a")
        assert tool.runtime.store.value(n) == 1

        tool.runtime.patch_state({"s": "patched"})
        tool.flush()
        assert tool.runtime.store.value(n) == 1  # only the user's edit fired it

    def test_a_patch_does_not_fire_a_handler_or_look_like_typing(self) -> None:
        tool = tool_with("", editable=True)
        origins: list[Origin] = []
        tool.runtime.store.subscribe(lambda change: origins.append(change.origin))

        tool.runtime.patch_state({"s": "patched"})
        tool.flush()

        assert shown(view_of(tool)) == "patched"
        assert Origin.USER not in origins

    def test_typing_past_the_entrys_own_max_length_is_refused(self) -> None:
        tool = tool_with("abc", editable=True, max_length=5)
        view = view_of(tool)
        buffer = view.get_buffer()

        buffer.insert(buffer.get_end_iter(), "de")
        assert held(tool) == "abcde"
        buffer.insert(buffer.get_end_iter(), "f")  # the sixth character

        assert shown(view) == "abcde"
        assert held(tool) == "abcde"

    def test_pasting_a_huge_text_is_refused_whole_and_changes_nothing(self) -> None:
        tool = tool_with("kept", editable=True)
        view = view_of(tool)
        buffer = view.get_buffer()

        buffer.insert(buffer.get_end_iter(), "p" * 1_000_000)

        assert shown(view) == "kept"
        assert held(tool) == "kept"
        assert buffer.get_char_count() == 4

    def test_the_cap_is_the_displayed_text_cap_even_when_the_entry_allows_more(self) -> None:
        tool = tool_with("a" * (MAX_SHOWN_PER_TEXT - 1), editable=True)
        view = view_of(tool)
        buffer = view.get_buffer()

        buffer.insert(buffer.get_end_iter(), "b")
        assert buffer.get_char_count() == MAX_SHOWN_PER_TEXT
        buffer.insert(buffer.get_end_iter(), "c")

        assert buffer.get_char_count() == MAX_SHOWN_PER_TEXT
        assert held(tool) == "a" * (MAX_SHOWN_PER_TEXT - 1) + "b"

    def test_replacing_the_selection_with_text_of_the_same_size_is_allowed_at_the_cap(self) -> None:
        tool = tool_with("x" * 10, editable=True, max_length=10)
        buffer = view_of(tool).get_buffer()
        buffer.select_range(buffer.get_start_iter(), buffer.get_end_iter())

        buffer.delete_selection(True, True)
        buffer.insert_at_cursor("y" * 10)

        assert held(tool) == "y" * 10

    def test_the_view_the_user_types_in_keeps_its_text_when_the_budget_is_spent(
        self, echo_shell: Shell
    ) -> None:
        pads: list[Json] = [{"kind": "label", "bind": "pad"}] * 8  # 8 * 8,000 of 65,536
        tool = show_in_window(
            echo_shell, spec_for("start", editable=True, before=pads, pad_chars=8000)
        )
        view = view_of(tool)
        view.grab_focus()
        assert shown(view) == "start"
        typed = "start" + "!" * 3000

        view.get_buffer().insert(view.get_buffer().get_end_iter(), "!" * 3000)  # over the budget
        assert shown(view) == typed  # at once: not a mark under the user's fingers
        tool.flush()
        pump(0.1)

        assert shown(view) == typed  # and after the idle turn, while it has the focus
        assert held(tool) == typed

    def test_a_view_without_the_focus_claims_like_any_other_once_the_edit_is_over(self) -> None:
        pads: list[Json] = [{"kind": "label", "bind": "pad"}] * 8
        tool = tool_with("start", editable=True, before=pads, pad_chars=8000)
        view = view_of(tool)

        view.get_buffer().insert(view.get_buffer().get_end_iter(), "!" * 3000)
        assert shown(view) == "start" + "!" * 3000  # at once, it is what was typed
        tool.flush()

        assert shown(view) == NOT_SHOWN  # the next idle turn: no room, and no focus to keep it
        assert view.get_editable() is False
        assert held(tool) == "start" + "!" * 3000  # the state has what was typed

    def test_an_edit_that_the_state_changed_on_the_way_is_put_right_after_not_inside(
        self,
    ) -> None:
        # What the buffer holds is what the user typed; if the state then holds something else
        # (here a handler of ours rewrites it), the view is brought to the state's value on the
        # next idle turn, never by writing the buffer inside its own `changed`.
        tool = tool_with("", editable=True)
        view = view_of(tool)
        real = tool.runtime.user_input

        def changing(key: StateKey, value: Value, *, event: UiEvent | None = None) -> Dispatch:
            return real(key, "SET BY THE STATE", event=event)

        with mock.patch.object(tool.runtime, "user_input", changing):
            view.get_buffer().insert_at_cursor("typed")
            assert shown(view) == "typed"  # not rewritten inside `changed`
            tool.flush()

        assert shown(view) == "SET BY THE STATE"
        assert held(tool) == "SET BY THE STATE"

    def test_a_failing_handler_is_behind_the_boundary_and_the_view_is_put_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tool = tool_with("state", editable=True)
        view = view_of(tool)

        with (
            caplog.at_level("ERROR"),
            mock.patch.object(tool.runtime, "user_input", side_effect=RuntimeError("boom")),
        ):
            view.get_buffer().insert_at_cursor("typed")

        assert "a user event failed" in caplog.text
        tool.flush()
        assert shown(view) == "state"  # never a text the state does not have
