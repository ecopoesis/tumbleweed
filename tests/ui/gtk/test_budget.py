"""The text a tool puts on the screen is bounded in total (I9), and what is bounded is tested.

Before the budget, an 80 KB valid spec of 448 labels bound to one 64K-character computed string
took 10 s to build, minutes to lay out, and 35 s for every slider tick, with the whole window (the
trusted chrome and its prompts included) frozen. Each label was bounded; the total was not.
"""

import time
from typing import TYPE_CHECKING

import pytest

from tests.shell.gtk_support import Shell, pump, pump_until
from tests.ui.gtk.support import labels_text, of_type, spec_of
from tests.ui.runtime.support import choice, text
from tests.ui.spec.support import Json, computed, lit_str, ref, variadic
from tumbleweed.ui.gtk.budget import (
    CUT_MARK,
    MAX_SHOWN_PER_TEXT,
    MAX_SHOWN_PER_TOOL,
    MORE_OPTIONS,
    NOT_SHOWN,
    TextBudget,
)
from tumbleweed.ui.spec.limits import MAX_COMPONENTS

if TYPE_CHECKING:
    from tumbleweed.core.types import JsonValue
    from tumbleweed.ui.gtk import RenderedTool
    from tumbleweed.ui.spec import UiSpec

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk

pytestmark = pytest.mark.gtk


def many(child: Json, count: int) -> Json:
    """`count` copies of `child`, in boxes of at most 63 (a container holds 64)."""
    rows: list[JsonValue] = []
    for start in range(0, count, 63):
        copies: list[JsonValue] = [child] * min(63, count - start)
        rows.append({"kind": "box", "children": copies})
    return {"kind": "box", "children": rows}


class TestTheBudget:
    def test_claims_add_up_and_a_claim_that_does_not_fit_is_dropped(self) -> None:
        budget = TextBudget()
        owner_a, owner_b = object(), object()

        assert budget.claim(owner_a, MAX_SHOWN_PER_TOOL) is True  # exactly the budget fits
        assert budget.claim(owner_b, 1) is False
        assert budget.total == MAX_SHOWN_PER_TOOL

    def test_an_owner_that_shows_less_gives_back_room(self) -> None:
        budget = TextBudget()
        owner_a, owner_b = object(), object()
        budget.claim(owner_a, MAX_SHOWN_PER_TOOL)

        budget.claim(owner_a, 10)

        assert budget.claim(owner_b, MAX_SHOWN_PER_TOOL - 10) is True
        assert budget.total == MAX_SHOWN_PER_TOOL

    def test_a_claim_that_no_longer_fits_frees_what_the_owner_held(self) -> None:
        budget = TextBudget()
        owner = object()
        budget.claim(owner, 100)

        assert budget.claim(owner, MAX_SHOWN_PER_TOOL + 1) is False

        assert budget.total == 0

    def test_clear_empties_it(self) -> None:
        budget = TextBudget()
        budget.claim(object(), 5)

        budget.clear()

        assert budget.total == 0


LONG = "y" * 65536


def worst_case_spec() -> "UiSpec":
    """441 labels bound to one 64K computed string that a slider changes, among other things."""
    state: list[Json] = [
        text("s", LONG, max_length=65536),
        {"kind": "int", "key": "n", "default": 0, "minimum": 0, "maximum": 100},
        {
            **computed("big", "string", variadic("concat", ref("s"), lit_str("")), ""),
            "max_length": 65536,
        },
    ]
    rows: list[Json] = [
        {"kind": "box", "children": [{"kind": "label", "bind": "big"}] * 63} for _ in range(7)
    ]
    root: Json = {"kind": "box", "children": [{"kind": "slider", "bind": "n"}, *rows]}
    assert MAX_COMPONENTS >= 1 + 1 + 1 + 7 * 64
    return spec_of(root, state)


class TestTheWorstCaseSpec:
    def test_it_builds_and_shows_at_most_the_budget_of_text(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        started = time.monotonic()
        tool = RenderedTool(worst_case_spec())
        built = time.monotonic() - started

        assert built < 5
        shown = labels_text(tool)
        assert len(shown) == 7 * 63
        assert tool.runtime.store.value("big") == LONG  # type: ignore[arg-type]  # the state: whole
        # at most the budget of characters is in labels (plus the marks that replace the rest)
        real = [t for t in shown if t != NOT_SHOWN]
        assert sum(len(t.replace("​", "")) for t in real) <= MAX_SHOWN_PER_TOOL
        assert shown.count(NOT_SHOWN) == len(shown) - len(real) > 400
        assert all(t.endswith(CUT_MARK) for t in real)  # each shows its start, and says so
        assert tool.widget is not None

    def test_it_lays_out_and_answers_a_slider_in_time_on_the_real_window(
        self, echo_shell: Shell
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        window = echo_shell.window
        started = time.monotonic()
        tool = window.canvas.show_spec(worst_case_spec())
        window.present()
        assert pump_until(lambda: window.get_mapped() and window.canvas.get_width() > 0, timeout=60)
        pump(0.3)
        first_layout = time.monotonic() - started

        (slider,) = of_type(tool, Gtk.Scale)
        ticks = time.monotonic()
        for step in range(1, 40):
            slider.set_value(float(step))
        pump(0.3)
        tick_time = time.monotonic() - ticks

        assert first_layout < 20
        assert tick_time < 10
        assert tool.runtime.store.value("n") == 39  # type: ignore[arg-type]  # the key's text


class TestOneLabel:
    def test_a_text_over_the_cap_shows_its_start_and_a_mark(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(
            spec_of({"kind": "label", "bind": "s"}, [text("s", "ab" * 6000, max_length=65536)])
        )

        shown = labels_text(tool)[0].replace("​", "")

        assert shown == ("ab" * 6000)[:MAX_SHOWN_PER_TEXT] + CUT_MARK

    def test_a_text_exactly_at_the_cap_is_not_marked(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(
            spec_of(
                {"kind": "label", "bind": "s"},
                [text("s", "a" * MAX_SHOWN_PER_TEXT, max_length=65536)],
            )
        )

        assert CUT_MARK not in labels_text(tool)[0]

    def test_a_text_over_the_tool_budget_is_replaced_and_comes_back_when_room_is_made(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("a", "", max_length=65536)]
        tool = RenderedTool(spec_of(many({"kind": "label", "bind": "a"}, 9), state))
        tool.runtime.patch_state({"a": "q" * 8000})

        shown = labels_text(tool)

        assert shown[:8] == ["q" * 8000] * 8 or all(
            t.replace("\u200b", "") == "q" * 8000 for t in shown[:8]
        )
        assert shown[8] == NOT_SHOWN  # nine of 8,000 are 72,000: over the 65,536
        tool.runtime.patch_state({"a": "short"})
        assert labels_text(tool) == ["short"] * 9  # the room is back, and each takes it again


class TestOptionsAndLines:
    def test_a_list_of_options_over_the_budget_ends_in_a_mark_and_shows_a_prefix(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool
        from tumbleweed.ui.gtk.choice import ChoiceList

        options = [f"o{i:02d}" + "m" * 120 for i in range(64)]  # 123 characters, 7,872 in all
        state: list[Json] = [choice("e", options, options[0])]
        tool = RenderedTool(spec_of(many({"kind": "dropdown", "bind": "e"}, 10), state))

        lists = of_type(tool, ChoiceList)

        shown = [len(lst.options) for lst in lists]
        assert sum(n * 123 for n in shown) <= MAX_SHOWN_PER_TOOL
        assert shown[:8] == [64] * 8
        assert shown[8] < 64
        assert shown[9] == 0
        assert [len(lst.rows) for lst in lists] == shown
        marks = [w for lst in lists for w in _labels(lst) if w.get_text() == MORE_OPTIONS]
        assert len(marks) == 2

    def test_five_hundred_dropdowns_over_a_64_option_enum_build_in_time(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        options = [f"o{i:02d}" + "m" * 120 for i in range(64)]
        state: list[Json] = [choice("e", options, options[0])]
        started = time.monotonic()

        tool = RenderedTool(spec_of(many({"kind": "dropdown", "bind": "e"}, 500), state))

        assert time.monotonic() - started < 10
        assert tool.widget is not None

    def test_buttons_over_the_budget_show_a_mark_instead_of_their_text(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(
            spec_of(many({"kind": "button", "label": "W" * 200}, 400), [])
        )  # 80,000

        labels = [w.get_text() for w in of_type(tool, Gtk.Label)]
        assert labels.count(NOT_SHOWN) > 50
        assert sum(len(t.replace("​", "")) for t in labels if t != NOT_SHOWN) <= 65536


def _labels(widget: "Gtk.Widget") -> "list[Gtk.Label]":
    from tests.shell.gtk_support import labels_in

    return labels_in(widget)


class TestUpdatesAreCoalesced:
    def test_many_changes_between_two_frames_lay_the_text_out_once_with_the_latest(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [
            {"kind": "int", "key": "n", "default": 0, "minimum": 0, "maximum": 1000},
            {
                **computed(
                    "t",
                    "string",
                    variadic(
                        "concat", lit_str("n="), {"kind": "format_number", "operand": ref("n")}
                    ),
                    "",
                ),
                "max_length": 100,
            },
        ]
        root: Json = {
            "kind": "box",
            "children": [{"kind": "slider", "bind": "n"}, {"kind": "label", "bind": "t"}],
        }
        tool = RenderedTool(spec_of(root, state))
        (slider,) = of_type(tool, Gtk.Scale)
        (label,) = [w for w in of_type(tool, Gtk.Label) if w.get_text().startswith("n=")]
        sets: list[str] = []
        original = label.set_text

        def counting(text: str) -> None:
            sets.append(text)
            original(text)

        label.set_text = counting  # type: ignore[method-assign,assignment]  # to count the layouts

        for step in range(1, 101):
            slider.set_value(float(step))
        assert sets == []  # nothing was laid out while the drag went on
        assert pump_until(lambda: bool(sets))

        assert sets == ["n=100"]  # once, and the latest
        assert tool.runtime.store.value("t") == "n=100"  # type: ignore[arg-type]  # the key's text

    def test_the_first_text_is_shown_at_once_and_flush_shows_what_is_waiting(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "first")]
        tool = RenderedTool(spec_of({"kind": "label", "bind": "s"}, state))
        assert tool.widget.get_first_child().get_text() == "first"  # type: ignore[union-attr]  # a label

        tool.runtime.patch_state({"s": "second"})
        waiting = tool.widget.get_first_child().get_text()  # type: ignore[union-attr]  # a label
        tool.flush()

        assert waiting == "first"
        assert tool.widget.get_first_child().get_text() == "second"  # type: ignore[union-attr]  # a label

    def test_closing_the_tool_cancels_what_was_waiting(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "first")]
        tool = RenderedTool(spec_of({"kind": "label", "bind": "s"}, state))
        tool.runtime.patch_state({"s": "second"})

        tool.close()
        pump(0.1)

        assert tool.widget.get_first_child().get_text() == "first"  # type: ignore[union-attr]  # a label


JAMO = "ᄀ" * 16000  # one grapheme cluster of 16,000 code points: costly to shape


def entries_spec(count: int) -> "UiSpec":
    """`count` entries bound to one long string, and a slider, in boxes of at most 63."""
    state: list[Json] = [
        text("s", JAMO, max_length=65536),
        {"kind": "int", "key": "n", "default": 0, "minimum": 0, "maximum": 100},
    ]
    root = many({"kind": "entry", "bind": "s", "placeholder": "p" * 200}, count)
    assert isinstance(root["children"], list)
    root["children"].insert(0, {"kind": "slider", "bind": "n"})
    return spec_of(root, state)


class TestEntriesAreBudgetedToo:
    def test_a_few_entries_show_their_text_and_the_rest_are_marked_read_only(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(entries_spec(20))

        entries = of_type(tool, Gtk.Entry)
        shown = [e for e in entries if e.get_text() == JAMO[:MAX_SHOWN_PER_TEXT]]
        marked = [e for e in entries if e.get_text() == NOT_SHOWN]
        assert len(shown) + len(marked) == 20
        # 8 of 8,192 would fill the budget; the 200-character placeholders (counted too) leave 7
        assert len(shown) == 7
        assert len(marked) == 13
        assert all(not e.get_editable() for e in entries)  # all over the cap: none is editable
        assert all("warning" in e.get_css_classes() for e in entries)
        assert tool.runtime.store.value("s") == JAMO  # type: ignore[arg-type]  # the key's text

    def test_what_an_entry_shows_counts_against_the_same_budget_as_the_labels(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "x" * 8000, max_length=65536)]
        root: Json = many({"kind": "entry", "bind": "s"}, 9)
        tool = RenderedTool(spec_of(root, state))

        entries = [w for w in _all(tool) if type(w).__name__ == "Entry"]

        shown = [e for e in entries if e.get_text() == "x" * 8000]  # type: ignore[attr-defined]
        assert len(shown) == 8  # eight of 8,000 are 64,000; the ninth would be 72,000
        marked = [e for e in entries if e.get_text() == NOT_SHOWN]  # type: ignore[attr-defined]
        assert len(marked) == 1

    def test_an_entry_refused_for_lack_of_room_shows_its_text_when_room_appears(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [
            text("s", "x" * 8192, max_length=65536),
            text("t", "tail", max_length=65536),
        ]
        children: list[Json] = [{"kind": "entry", "bind": "s"} for _ in range(8)]
        children.append({"kind": "entry", "bind": "t"})
        tool = RenderedTool(spec_of(many_of(children), state))
        entries = of_type(tool, Gtk.Entry)
        assert entries[-1].get_text() == NOT_SHOWN

        tool.runtime.patch_state({"s": "short"})
        tool.flush()

        assert entries[-1].get_text() == "tail"
        assert entries[-1].get_editable() is True

    def test_the_worst_case_of_448_entries_over_one_costly_string_is_fast_to_type_into(
        self, echo_shell: Shell
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        window = echo_shell.window
        started = time.monotonic()
        tool = window.canvas.show_spec(entries_spec(448))
        build = time.monotonic() - started
        window.present()
        assert pump_until(lambda: window.get_mapped() and window.canvas.get_width() > 0, timeout=60)
        pump(0.3)
        first_layout = time.monotonic() - started

        (slider,) = of_type(tool, Gtk.Scale)
        ticks = time.monotonic()
        for step in range(1, 30):
            slider.set_value(float(step))  # changes nothing the entries show
        pump(0.3)
        tick_time = time.monotonic() - ticks

        assert build < 10
        assert first_layout < 30
        assert tick_time < 10


def many_of(children: list[Json]) -> Json:
    rows: list[JsonValue] = []
    for start in range(0, len(children), 63):
        chunk: list[JsonValue] = [*children[start : start + 63]]
        rows.append({"kind": "box", "children": chunk})
    return {"kind": "box", "children": rows}


def _all(tool: "RenderedTool") -> "list[Gtk.Widget]":
    from tests.shell.gtk_support import descendants

    return list(descendants(tool.widget))


def test_keystrokes_in_one_of_448_entries_bound_to_one_costly_string_stay_fast(
    echo_shell: Shell,
) -> None:
    from tumbleweed.shell._gi import Gtk

    state: list[Json] = [text("s", "ᄀ" * 8000, max_length=65536)]  # editable: under the cap
    window = echo_shell.window
    tool = window.canvas.show_spec(
        spec_of(many({"kind": "entry", "bind": "s", "placeholder": "p" * 200}, 448), state)
    )
    window.present()
    assert pump_until(lambda: window.get_mapped() and window.canvas.get_width() > 0, timeout=60)
    pump(0.3)
    first = of_type(tool, Gtk.Entry)[0]
    assert first.get_editable() is True  # the first to ask got room for its text

    for letter in "abcde":
        started = time.monotonic()
        position = len(first.get_text())
        first.get_buffer().insert_text(position, letter, -1)  # a key typed at the end
        handled = time.monotonic() - started
        pump(0.1)
        settled = time.monotonic() - started

        assert handled < 3, handled
        assert settled < 10, settled

    assert str(tool.runtime.store.value("s")).endswith("abcde")  # type: ignore[arg-type]


class TestRefusedOwnersAreServedWhenRoomAppears:
    def crowded(self) -> "RenderedTool":
        """Seven labels of 8,192 characters and one of 8,190 leave two of the budget: everything
        after them (a button, a check box, a dropdown, a short label) is refused."""
        from tumbleweed.ui.gtk import RenderedTool

        kids: list[Json] = [{"kind": "label", "bind": "big"} for _ in range(7)]
        kids += [{"kind": "label", "bind": "mid"}, {"kind": "button", "label": "Save"}]
        kids += [
            {"kind": "check", "label": "Keep", "bind": "flag"},
            {"kind": "dropdown", "bind": "size"},
        ]
        kids += [{"kind": "label", "bind": "late"}]
        state: list[Json] = [
            text("big", "x" * 8192, max_length=65536),
            text("mid", "y" * 8190, max_length=65536),
            text("late", "late text", max_length=65536),
            choice("size", ["small", "large"], "large"),
            {"kind": "bool", "key": "flag", "default": False},
        ]
        return RenderedTool(spec_of(many_of(kids), state))

    def test_a_dropdown_built_with_the_budget_full_gets_its_options_when_room_appears(self) -> None:
        from tumbleweed.ui.gtk.choice import ChoiceList

        tool = self.crowded()
        (choices,) = of_type(tool, ChoiceList)
        nothing_fitted, selected_before = choices.options, choices.selected

        tool.runtime.patch_state({"big": "", "mid": ""})
        tool.flush()

        assert (nothing_fitted, selected_before) == ((), None)
        assert choices.options == ("small", "large")
        assert choices.selected == "large"  # the entry's option is selected once its row exists
        assert MORE_OPTIONS not in [w.get_text() for w in _labels(choices)]

    def test_static_labels_and_marks_come_back_too(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = self.crowded()
        before = [w.get_text() for w in of_type(tool, Gtk.Label)]
        assert NOT_SHOWN in before  # the late label and the buttons' labels were refused
        refused = before.count(NOT_SHOWN)
        assert refused >= 3

        tool.runtime.patch_state({"big": "", "mid": ""})
        tool.flush()

        after = [w.get_text() for w in of_type(tool, Gtk.Label)]
        assert NOT_SHOWN not in after
        assert "Save" in after
        assert "Keep" in after
        assert "late text" in after

    def test_a_dropdown_is_filled_in_as_far_as_room_allows_and_then_the_rest(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool
        from tumbleweed.ui.gtk.choice import ChoiceList

        options = [f"o{i:02d}" + "m" * 120 for i in range(64)]
        state: list[Json] = [
            text("big", "x" * 8000, max_length=65536),
            choice("e", options, options[63]),
        ]
        kids: list[Json] = [{"kind": "label", "bind": "big"} for _ in range(8)]
        kids.append({"kind": "dropdown", "bind": "e"})
        tool = RenderedTool(spec_of(many_of(kids), state))
        (choices,) = of_type(tool, ChoiceList)
        assert len(choices.options) == (65536 - 8 * 8000) // 123  # what is left, by whole options
        assert choices.selected is None  # the entry's own option is not among them yet

        tool.runtime.patch_state({"big": ""})
        tool.flush()

        assert len(choices.options) == 64
        assert choices.selected == options[63]


class TestEntryUpdatesAreCoalescedToo:
    def test_an_entry_the_user_is_not_typing_in_follows_the_state_once_per_frame(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(spec_of({"kind": "entry", "bind": "s"}, [text("s", "first")]))
        (entry,) = of_type(tool, Gtk.Entry)
        sets: list[str] = []
        original = entry.set_text

        def counting(value: str) -> None:
            sets.append(value)
            original(value)

        entry.set_text = counting  # type: ignore[method-assign,assignment]  # to count the layouts

        for step in range(100):
            tool.runtime.patch_state({"s": f"value {step}"})
        assert entry.get_text() == "first"  # nothing was laid out while the patches came
        assert sets == []
        assert pump_until(lambda: bool(sets))

        assert sets == ["value 99"]  # once, the latest
        assert entry.get_text() == "value 99"

    def test_what_the_user_types_is_not_deferred(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        state: list[Json] = [text("s", "", max_length=100), text("copy", "", max_length=100)]
        tool = RenderedTool(
            spec_of(
                {
                    "kind": "box",
                    "children": [
                        {"kind": "entry", "bind": "s"},
                        {"kind": "entry", "bind": "copy"},
                    ],
                },
                state,
            )
        )
        typed, other = of_type(tool, Gtk.Entry)

        typed.get_buffer().insert_text(0, "abc", -1)

        assert typed.get_text() == "abc"
        assert other.get_text() == ""  # the other entry is for a different entry of the state
