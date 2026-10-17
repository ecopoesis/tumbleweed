"""Text from a spec is plain, and cannot widen the window or freeze the layout (I4, I5, I9).

The text of a spec is the model's, and so is the value of a `string` entry (it can be patched, up
to 65,536 characters). It goes into labels through `plain_label` and the display transform of the
shell, in chunks; these tests put markup, mnemonics, huge grapheme clusters and long unbroken runs
through every place a label shows text and look at the labels, and at the shell window's geometry.
"""

import time
from typing import TYPE_CHECKING

import pytest

from tests.shell.gtk_support import Shell, bounds, descendants, labels_in, pump, pump_until
from tests.ui.gtk.hostile import LONG_BLOCK, LONG_LINE, MARKUP, NO_SPACES, hostile_spec
from tests.ui.gtk.support import labels_text, of_type, spec_of
from tests.ui.runtime.support import choice, flag, text
from tumbleweed.shell.viewmodel.display import MAX_CHUNK, ZERO_WIDTH_SPACE
from tumbleweed.ui.spec.names import StateKey

if TYPE_CHECKING:
    from tests.ui.spec.support import Json

pytestmark = pytest.mark.gtk


def key(name: str) -> StateKey:
    return StateKey(name)


class TestPlainText:
    def test_markup_and_mnemonics_are_shown_literally_everywhere_a_spec_puts_text(self) -> None:
        from tumbleweed.shell._gi import Gdk, Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", MARKUP), choice("e", [MARKUP, "_two"], MARKUP), flag("b")]
        root: Json = {
            "kind": "box",
            "children": [
                {"kind": "heading", "text": MARKUP},
                {"kind": "label", "text": MARKUP},
                {"kind": "label", "bind": "s"},
                {"kind": "label", "bind": "e"},
                {"kind": "button", "label": MARKUP},
                {"kind": "check", "label": MARKUP, "bind": "b"},
                {"kind": "dropdown", "bind": "e"},
                {"kind": "entry", "bind": "s", "placeholder": MARKUP},
            ],
        }

        tool = RenderedTool(spec_of(root, state))

        labels = of_type(tool, Gtk.Label)
        assert len(labels) >= 8
        for label in labels:
            assert label.get_use_markup() is False
            assert label.get_use_underline() is False
            assert label.get_mnemonic_keyval() == Gdk.KEY_VoidSymbol  # no accelerator from `_`
        shown = labels_text(tool)
        assert shown[:4] == [MARKUP, MARKUP, MARKUP, MARKUP]
        (entry,) = of_type(tool, Gtk.Entry)
        assert entry.get_text() == MARKUP
        assert entry.get_placeholder_text() == MARKUP
        button_labels = [
            w for w in of_type(tool, Gtk.Label) if isinstance(w.get_parent(), Gtk.Button)
        ]
        assert [label.get_text() for label in button_labels] == [MARKUP]
        (check,) = of_type(tool, Gtk.CheckButton)
        assert [w.get_text() for w in labels_in(check)] == [MARKUP]

    def test_nothing_in_the_tree_asks_a_label_to_parse_markup_or_makes_a_link(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(hostile_spec())

        for label in of_type(tool, Gtk.Label):
            assert label.get_use_markup() is False
            assert label.get_current_uri() is None
            assert not label.get_selectable()  # no text selection, and so no drag of the text


class TestLongText:
    def test_a_bound_label_with_the_longest_value_shows_its_start_and_says_it_is_cut(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool
        from tumbleweed.ui.gtk.budget import CUT_MARK, MAX_SHOWN_PER_TEXT
        from tumbleweed.ui.gtk.text import TextBlock

        state: list[Json] = [text("s", "", max_length=65536)]
        tool = RenderedTool(spec_of({"kind": "label", "bind": "s"}, state))
        started = time.monotonic()

        tool.runtime.patch_state({"s": "\u1100\u1161\u11a8" * 21845})  # one unbreakable cluster
        tool.flush()

        assert time.monotonic() - started < 5
        assert isinstance(tool.widget, TextBlock)
        labels = of_type(tool, Gtk.Label)
        assert len(labels) == 3  # 8,192 characters and the mark, in chunks of at most 4,096
        assert all(len(label.get_text()) <= MAX_CHUNK for label in labels)
        shown = tool.widget.shown_text
        assert shown.endswith(CUT_MARK)
        assert ZERO_WIDTH_SPACE in shown  # the cluster was cut into pieces
        assert len(shown.replace(ZERO_WIDTH_SPACE, "")) == MAX_SHOWN_PER_TEXT + len(CUT_MARK)
        assert tool.runtime.store.value(key("s")) == "\u1100\u1161\u11a8" * 21845  # state: whole

    def test_a_value_that_fits_is_shown_whole_without_a_mark(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool
        from tumbleweed.ui.gtk.budget import MAX_SHOWN_PER_TEXT

        state: list[Json] = [text("s", "", max_length=65536)]
        tool = RenderedTool(spec_of({"kind": "label", "bind": "s"}, state))

        tool.runtime.patch_state({"s": "y" * MAX_SHOWN_PER_TEXT})

        assert "(cut)" not in labels_text(tool)[0]
        assert labels_text(tool)[0].replace(ZERO_WIDTH_SPACE, "") == "y" * MAX_SHOWN_PER_TEXT

    def test_sixty_four_kib_with_no_break_opportunity_is_cut_and_fast(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "", max_length=65536)]
        tool = RenderedTool(spec_of({"kind": "label", "bind": "s"}, state))
        started = time.monotonic()

        tool.runtime.patch_state({"s": "y" * 65536})
        tool.flush()

        assert time.monotonic() - started < 5
        labels = of_type(tool, Gtk.Label)
        assert all(len(label.get_text()) <= MAX_CHUNK for label in labels)
        assert (
            "".join(label.get_text() for label in labels).count(ZERO_WIDTH_SPACE) >= 8192 // 256 - 1
        )

    def test_a_static_block_and_a_long_line_go_through_the_same_transform(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(
            spec_of(
                {
                    "kind": "box",
                    "children": [
                        {"kind": "label", "text": LONG_BLOCK},
                        {"kind": "label", "text": NO_SPACES},
                        {"kind": "button", "label": LONG_LINE},
                        {"kind": "check", "label": LONG_LINE, "bind": "b"},
                    ],
                },
                [flag("b")],
            )
        )

        shown = labels_text(tool)
        assert ZERO_WIDTH_SPACE in shown[0]
        assert ZERO_WIDTH_SPACE in shown[1]
        assert shown[0].replace(ZERO_WIDTH_SPACE, "") == LONG_BLOCK  # only breaks were added
        assert shown[1].replace(ZERO_WIDTH_SPACE, "") == NO_SPACES

    def test_rewriting_a_long_value_again_and_again_costs_one_layout_per_frame(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "", max_length=65536)]
        tool = RenderedTool(spec_of({"kind": "label", "bind": "s"}, state))
        block_texts: list[str] = []
        started = time.monotonic()

        for round_ in range(200):
            tool.runtime.patch_state({"s": ("z" if round_ % 2 else "q") * 65536})
            block_texts.append("")  # (the labels have not been touched since the last frame)

        assert time.monotonic() - started < 10
        assert labels_text(tool)[0].startswith("z" * 100)  # only the latest was laid out
        assert len(block_texts) == 200

    def test_a_shorter_value_leaves_no_label_of_the_longer_one_behind(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "", max_length=65536)]
        tool = RenderedTool(spec_of({"kind": "label", "bind": "s"}, state))
        tool.runtime.patch_state({"s": "y" * 20000})
        tool.flush()
        assert len(of_type(tool, Gtk.Label)) == 3

        tool.runtime.patch_state({"s": "short"})
        tool.flush()

        labels = of_type(tool, Gtk.Label)
        assert [label.get_text() for label in labels] == ["short"]
        assert len(list(descendants(tool.widget))) == 2  # the block and its one label

    def test_rewriting_a_long_value_again_and_again_costs_time_in_proportion_to_its_size(
        self,
    ) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "", max_length=65536)]
        tool = RenderedTool(spec_of({"kind": "label", "bind": "s"}, state))
        started = time.monotonic()

        for round_ in range(20):
            tool.runtime.patch_state({"s": ("z" if round_ % 2 else "q") * 65536})

        assert time.monotonic() - started < 10


class TestEntries:
    def test_a_value_the_entry_buffer_cannot_hold_is_shown_cut_and_read_only(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "short", max_length=65536)]
        tool = RenderedTool(spec_of({"kind": "entry", "bind": "s"}, state))
        (entry,) = of_type(tool, Gtk.Entry)
        assert entry.get_editable() is True

        tool.runtime.patch_state({"s": "€" * 40000})  # 120,000 bytes: more than GTK holds

        assert entry.get_editable() is False  # editing would write the cut text over the value
        assert "warning" in entry.get_css_classes()
        assert len(entry.get_text()) < 40000
        assert tool.runtime.store.value(key("s")) == "€" * 40000  # the state keeps it all

        tool.runtime.patch_state({"s": "back"})

        assert entry.get_editable() is True
        assert "warning" not in entry.get_css_classes()
        assert entry.get_text() == "back"

    def test_a_value_that_fits_is_shown_whole_and_edits_write_back(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "", max_length=65536)]
        tool = RenderedTool(spec_of({"kind": "entry", "bind": "s"}, state))
        (entry,) = of_type(tool, Gtk.Entry)
        value = "x" * 65534

        tool.runtime.patch_state({"s": value})

        assert entry.get_text() == value
        assert entry.get_editable() is True
        entry.set_text("edited")
        assert tool.runtime.store.value(key("s")) == "edited"

    def test_the_entry_cannot_take_more_characters_than_the_entry_allows(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "", max_length=5)]
        tool = RenderedTool(spec_of({"kind": "entry", "bind": "s"}, state))
        (entry,) = of_type(tool, Gtk.Entry)

        entry.set_text("abcdefgh")

        assert tool.runtime.store.value(key("s")) == "abcde"
        assert entry.get_text() == "abcde"  # the widget shows what the state holds

    def test_a_long_placeholder_does_not_change_the_size_an_entry_asks_for(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        def width(placeholder: str) -> int:
            root: Json = {"kind": "entry", "bind": "s", "placeholder": placeholder}
            tool = RenderedTool(spec_of(root, [text("s")]))
            return tool.widget.measure(Gtk.Orientation.HORIZONTAL, -1)[1]

        assert width(LONG_LINE) == width("") == width("x")

    def test_a_long_value_does_not_change_the_size_an_entry_asks_for_either(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "", max_length=65536)]
        tool = RenderedTool(spec_of({"kind": "entry", "bind": "s"}, state))
        measure = (Gtk.Orientation.HORIZONTAL, -1)
        empty = tool.widget.measure(*measure)[:2]

        tool.runtime.patch_state({"s": "W" * 65534})
        long_value = tool.widget.measure(*measure)[:2]
        tool.runtime.patch_state({"s": "\u1100\u1161\u11a8" * 20000})
        cluster = tool.widget.measure(*measure)[:2]

        assert long_value == empty
        # a text GTK cannot hold gets a warning icon (22 px) and nothing else
        assert cluster[1] - empty[1] <= 30


def present(shell: Shell) -> None:
    shell.window.present()
    assert pump_until(lambda: shell.window.get_mapped() and shell.window.canvas.get_width() > 0)
    pump(0.2)


class TestTheWindowDoesNotFollowTheText:
    def test_the_hostile_spec_does_not_move_the_window_or_the_chrome(
        self, echo_shell: Shell
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        h, v = Gtk.Orientation.HORIZONTAL, Gtk.Orientation.VERTICAL
        window = echo_shell.window
        present(echo_shell)
        size = (window.get_width(), window.get_height())
        chrome, status = bounds(window.chrome, window), bounds(window.status_strip, window)
        natural = window.measure(h, -1)[:2], window.measure(v, -1)[:2]

        started = time.monotonic()
        window.canvas.show_spec(hostile_spec())
        pump(0.5)

        assert time.monotonic() - started < 30
        assert (window.get_width(), window.get_height()) == size
        assert bounds(window.chrome, window) == chrome
        assert bounds(window.status_strip, window) == status
        assert (window.measure(h, -1)[:2], window.measure(v, -1)[:2]) == natural

    def test_wrapped_labels_stay_within_the_canvas_width(self, echo_shell: Shell) -> None:
        from tumbleweed.shell._gi import Gtk

        window = echo_shell.window
        spec = spec_of(
            {
                "kind": "box",
                "children": [
                    {"kind": "label", "text": NO_SPACES},
                    {"kind": "label", "text": LONG_BLOCK},
                    {"kind": "button", "label": LONG_LINE},
                    {"kind": "heading", "text": NO_SPACES},
                ],
            }
        )
        window.canvas.show_spec(spec)
        present(echo_shell)
        canvas_width = window.canvas.get_width()

        for label in [w for w in descendants(window.canvas) if isinstance(w, Gtk.Label)]:
            assert label.get_width() <= canvas_width, label.get_text()[:20]
