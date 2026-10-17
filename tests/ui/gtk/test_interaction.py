"""What a widget does when the state refuses an edit, and when a handler writes back."""

from typing import TYPE_CHECKING

import pytest

from tests.shell.gtk_support import pump, pump_until
from tests.ui.gtk.support import labels_text, of_type, spec_of
from tests.ui.runtime.support import number, set_handler, text
from tests.ui.spec.support import Json, binary, lit_int, lit_str, ref, variadic
from tumbleweed.ui.spec.names import StateKey

if TYPE_CHECKING:
    from tumbleweed.ui.gtk import RenderedTool

pytestmark = pytest.mark.gtk


def key(name: str) -> StateKey:
    return StateKey(name)


def test_an_edit_that_the_state_budget_refuses_is_undone_in_the_widget() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [text(f"b{i}", max_length=65536) for i in range(63)]
    state += [text("pad", max_length=65536), text("u", max_length=10), number("hits", 0)]
    spec = spec_of(
        {"kind": "entry", "bind": "u", "on_change": "count"},
        state,
        [set_handler("count", hits=binary("add", ref("hits"), lit_int(1)))],
    )
    tool = RenderedTool(spec)
    tool.runtime.patch_state({f"b{i}": "x" * 65536 for i in range(32)})
    tool.runtime.patch_state({f"b{i}": "x" * 65536 for i in range(32, 63)})
    tool.runtime.patch_state({"pad": "p" * 65535})  # 63 x 64 KiB and this: one byte of room left
    (entry,) = of_type(tool, Gtk.Entry)
    entry.get_buffer().insert_text(0, "a", -1)  # fits in the one byte left
    assert tool.runtime.store.value(key("u")) == "a"
    assert tool.runtime.store.value(key("hits")) == 1

    entry.get_buffer().insert_text(1, "é", -1)  # typed after the "a": two bytes, room for one

    assert tool.runtime.store.value(key("u")) == "a"
    assert pump_until(lambda: entry.get_text() == "a")  # the widget goes back to the state's text
    assert tool.runtime.store.value(key("hits")) == 1  # and the handler did not fire


def test_a_handler_that_rewrites_the_entry_being_edited_runs_once_per_edit() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    spec = spec_of(
        {
            "kind": "box",
            "children": [
                {"kind": "entry", "bind": "s", "on_change": "shout"},
                {"kind": "label", "bind": "s"},
            ],
        },
        [text("s", max_length=100), number("edits", 0)],
        [
            set_handler(
                "shout",
                s=variadic("concat", ref("s"), lit_str("!")),
                edits=binary("add", ref("edits"), lit_int(1)),
            )
        ],
    )
    tool = RenderedTool(spec)
    (entry,) = of_type(tool, Gtk.Entry)

    entry.set_text("hi")

    # The user's edit wrote `hi`, the handler wrote `hi!` once, and the widget showing it did not
    # count as another edit by the user (which would have written `hi!!`, and so on, forever).
    assert tool.runtime.store.value(key("s")) == "hi!"
    assert tool.runtime.store.value(key("edits")) == 1
    assert pump_until(lambda: entry.get_text() == "hi!")
    assert labels_text(tool)[:1] == ["hi!"]

    # typed at the end: one edit (`set_text` would be a delete and an insert, two)
    entry.get_buffer().insert_text(3, "x", -1)

    assert tool.runtime.store.value(key("s")) == "hi!x!"
    assert tool.runtime.store.value(key("edits")) == 2
    assert pump_until(lambda: entry.get_text() == "hi!x!")
    pump(0.1)
    assert tool.runtime.store.value(key("s")) == "hi!x!"  # and nothing more came of it


def echo_spec(kind: str, state: list[Json], handler: Json) -> "RenderedTool":
    from tumbleweed.ui.gtk import RenderedTool

    box: Json = {
        "kind": "box",
        "children": [{"kind": kind, "bind": "x", "on_change": "answer", **EXTRA.get(kind, {})}],
    }
    return RenderedTool(spec_of(box, [*state, number("edits", 0)], [handler]))


EXTRA: dict[str, Json] = {"check": {"label": "Check"}}


def answering(target: str, value: Json) -> Json:
    return set_handler("answer", **{target: value}, edits=binary("add", ref("edits"), lit_int(1)))


def test_an_input_whose_handler_rewrites_it_is_answered_once_per_edit_and_shows_the_answer() -> (
    None
):
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk.choice import ChoiceList

    cases: list[tuple[str, list[Json], Json]] = [
        ("slider", [number("x", 0, minimum=0, maximum=100)], answering("x", lit_int(77))),
        ("spin", [number("x", 0, minimum=0, maximum=100)], answering("x", lit_int(77))),
        (
            "switch",
            [{"kind": "bool", "key": "x", "default": False}],
            answering("x", {"kind": "bool", "value": False}),
        ),
        (
            "check",
            [{"kind": "bool", "key": "x", "default": False}],
            answering("x", {"kind": "bool", "value": False}),
        ),
        (
            "dropdown",
            [{"kind": "enum", "key": "x", "options": ["a", "b", "c"], "default": "a"}],
            answering("x", lit_str("c")),
        ),
    ]
    for kind, state, handler in cases:
        tool = echo_spec(kind, state, handler)
        widget = tool.widget_at("root.children[0]")
        store = tool.runtime.store

        match kind:
            case "slider" | "spin":
                assert isinstance(widget, Gtk.Scale | Gtk.SpinButton)
                widget.get_adjustment().set_value(20.0)
                assert widget.get_value() == 77.0
                assert store.value(key("x")) == 77
            case "switch" | "check":
                assert isinstance(widget, Gtk.Switch | Gtk.CheckButton)
                widget.set_active(True)
                assert widget.get_active() is False
                assert store.value(key("x")) is False
            case _:
                assert isinstance(widget, ChoiceList)
                widget.select("b")
                assert widget.selected == "c"
                assert store.value(key("x")) == "c"
        pump(0.1)

        assert store.value(key("edits")) == 1, kind  # one edit, one answer


def test_an_edit_that_leaves_the_value_as_it_was_fires_no_handler_and_is_put_right() -> None:
    from tumbleweed.shell._gi import Gtk

    tool = echo_spec(
        "slider",
        [number("x", 18, minimum=0, maximum=100)],
        answering("x", lit_int(18)),
    )
    slider = tool.widget_at("root.children[0]")
    assert isinstance(slider, Gtk.Scale)

    slider.set_value(17.6)  # between whole numbers: rounds to the 18 the state holds

    assert tool.runtime.store.value(key("edits")) == 0
    assert slider.get_value() == 18.0  # and the slider snaps to it


def test_typing_into_an_entry_whose_handler_rewrites_it_keeps_the_order_and_the_cursor() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    spec = spec_of(
        {"kind": "entry", "bind": "s", "on_change": "shout"},
        [text("s", max_length=100)],
        [set_handler("shout", s=variadic("concat", ref("s"), lit_str("!")))],
    )
    tool = RenderedTool(spec)
    (entry,) = of_type(tool, Gtk.Entry)

    for letter in "abc":  # typed one after the other, each at the cursor
        entry.get_buffer().insert_text(entry.get_position(), letter, -1)
        entry.set_position(entry.get_position() + 1)  # (GTK does this when a key is typed)
        assert pump_until(lambda: entry.get_text().endswith("!"))
        pump(0.05)

    assert entry.get_text() == "abc!!!"  # not reversed ("cba!!!"): the cursor was kept
    assert tool.runtime.store.value(key("s")) == "abc!!!"
    assert entry.get_position() == 3


def test_the_cursor_is_kept_in_range_when_the_answer_is_shorter() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    tool = RenderedTool(
        spec_of({"kind": "entry", "bind": "s"}, [text("s", "hello", max_length=10)])
    )
    (entry,) = of_type(tool, Gtk.Entry)
    entry.set_position(5)

    tool.runtime.patch_state({"s": "ab"})
    tool.flush()

    assert entry.get_text() == "ab"
    assert entry.get_position() <= 2


def test_a_dropdown_cannot_be_deselected_while_the_state_keeps_the_option() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool
    from tumbleweed.ui.gtk.choice import ChoiceList

    state: list[Json] = [{"kind": "enum", "key": "e", "options": ["a", "b"], "default": "b"}]
    tool = RenderedTool(spec_of({"kind": "dropdown", "bind": "e"}, state))
    (choices,) = of_type(tool, ChoiceList)
    (listbox,) = of_type(tool, Gtk.ListBox)
    assert listbox.get_selection_mode() == Gtk.SelectionMode.BROWSE

    listbox.unselect_all()
    listbox.unselect_row(choices.rows[1])
    listbox.activate_action("selection.unselect-all", None)

    assert choices.selected == "b"
    assert tool.runtime.store.value(key("e")) == "b"


def test_selecting_what_is_not_an_option_changes_nothing() -> None:
    from tumbleweed.ui.gtk import RenderedTool
    from tumbleweed.ui.gtk.choice import ChoiceList

    state: list[Json] = [{"kind": "enum", "key": "e", "options": ["a", "b"], "default": "a"}]
    tool = RenderedTool(spec_of({"kind": "dropdown", "bind": "e"}, state))
    (choices,) = of_type(tool, ChoiceList)
    chosen: list[str] = []
    tool.runtime.store.subscribe(lambda change: chosen.append(str(change.value)))

    for not_an_option in ("zzz", "", "A", "a "):
        choices.select(not_an_option)

    assert choices.selected == "a"
    assert chosen == []


def test_a_text_over_the_cap_has_a_warning_icon_while_it_is_cut() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    tool = RenderedTool(
        spec_of({"kind": "entry", "bind": "s"}, [text("s", "ok", max_length=65536)])
    )
    (entry,) = of_type(tool, Gtk.Entry)
    position = Gtk.EntryIconPosition.SECONDARY
    assert entry.get_icon_name(position) is None

    tool.runtime.patch_state({"s": "€" * 40000})
    tool.flush()
    assert entry.get_icon_name(position) == "dialog-warning-symbolic"
    assert entry.get_icon_activatable(position) is False

    tool.runtime.patch_state({"s": "ok"})
    tool.flush()
    assert entry.get_icon_name(position) is None
