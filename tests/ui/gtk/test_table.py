"""The `table` widget: typed, plain-text cells in a column view, capped in size (I4, I9)."""

from typing import TYPE_CHECKING

import pytest

from tests.shell.gtk_support import Shell, descendants
from tests.ui.gtk.support import of_type, render, show_in_window, spec_of
from tests.ui.runtime.support import text
from tests.ui.spec.support import Json
from tumbleweed.ui.gtk.budget import CUT_MARK, MAX_SHOWN_PER_TOOL, MORE_ROWS, NOT_SHOWN
from tumbleweed.ui.gtk.table import (
    MAX_SHOWN_CELL,
    MAX_SHOWN_NAME,
    MAX_SHOWN_ROWS,
    TableWidget,
    cell_text,
    column_name,
)
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.values import Table

if TYPE_CHECKING:
    from tumbleweed.core.types import JsonValue
    from tumbleweed.ui.gtk import RenderedTool

pytestmark = pytest.mark.gtk

ZWSP = "​"


def columns(*pairs: tuple[str, str]) -> "JsonValue":
    return [{"name": name, "type": kind} for name, kind in pairs]


def table_spec(value: Json | None = None, *, before: list[Json] | None = None) -> UiSpec:
    state: list[Json] = [{"kind": "table", "key": "t", "max_rows": 10_000}]
    if value is not None:
        state[0] = {**state[0], "default": value}
    return spec_of(
        {"kind": "box", "children": [*(before or []), {"kind": "table", "bind": "t"}]},
        [*([text("s", "x" * 8192, max_length=65536)] if before else []), *state],
    )


def widget_of(tool: "RenderedTool") -> TableWidget:
    (widget,) = of_type(tool, TableWidget)
    return widget


def tool_with(value: Json | None = None, **kwargs: list[Json]) -> "RenderedTool":

    return render(table_spec(value, **kwargs))


PEOPLE: Json = {
    "columns": columns(("name", "string"), ("age", "int"), ("score", "float"), ("ok", "bool")),
    "rows": [["Ada", 36, 9.5, True], ["Alan", 41, 8.25, False]],
}


class TestShowingATable:
    def test_the_columns_are_the_values_and_every_cell_is_text(self) -> None:
        widget = widget_of(tool_with(PEOPLE))

        assert widget.column_names == ("name", "age", "score", "ok")
        assert widget.cells == (("Ada", "36", "9.5", "yes"), ("Alan", "41", "8.25", "no"))
        assert widget.rows == 2
        assert widget.notice == ""

    def test_the_cells_are_labels_in_a_column_view_numbers_right_aligned(
        self, echo_shell: Shell
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = show_in_window(echo_shell, spec_with(PEOPLE))
        labels = [w for w in descendants(widget_of(tool)) if isinstance(w, Gtk.Label)]
        by_text = {label.get_text(): label for label in labels}

        for shown in ("Ada", "Alan", "36", "41", "9.5", "8.25", "yes", "no"):
            assert shown in by_text, shown
        assert by_text["Ada"].get_xalign() == 0.0
        assert by_text["yes"].get_xalign() == 0.0
        assert by_text["36"].get_xalign() == 1.0
        assert by_text["8.25"].get_xalign() == 1.0
        assert len(of_type(tool, Gtk.ColumnView)) == 1

    def test_it_starts_empty_and_shows_nothing_until_there_is_a_table(self) -> None:
        tool = tool_with()
        widget = widget_of(tool)

        assert widget.cells == ()
        assert widget.column_names == ()
        assert not widget.get_first_child().get_visible()  # type: ignore[union-attr]
        assert widget.notice == ""

    def test_a_table_with_columns_and_no_rows_shows_its_header(self) -> None:
        widget = widget_of(tool_with({"columns": columns(("a", "string")), "rows": []}))

        assert widget.column_names == ("a",)
        assert widget.cells == ()
        assert widget.notice == ""

    def test_a_patch_replaces_the_columns_and_the_rows(self) -> None:
        tool = tool_with(PEOPLE)
        widget = widget_of(tool)

        tool.runtime.patch_state(
            {"t": {"columns": columns(("city", "string")), "rows": [["Oslo"], ["Lima"], ["Pune"]]}}
        )
        tool.flush()

        assert widget.column_names == ("city",)
        assert widget.cells == (("Oslo",), ("Lima",), ("Pune",))
        assert widget.rows == 3

    def test_a_table_that_becomes_empty_takes_its_rows_away(self) -> None:
        tool = tool_with(PEOPLE)
        widget = widget_of(tool)

        tool.runtime.patch_state({"t": {"columns": [], "rows": []}})
        tool.flush()

        assert widget.column_names == ()
        assert widget.cells == ()

    def test_32_columns_are_all_shown(self) -> None:
        names = [(f"c{i}", "int") for i in range(32)]
        widget = widget_of(tool_with({"columns": columns(*names), "rows": [list(range(32))]}))

        assert widget.column_names == tuple(name for name, _ in names)
        assert widget.cells == (tuple(str(i) for i in range(32)),)

    def test_a_stale_entry_says_so_instead_of_showing_the_old_rows(self) -> None:
        widget = widget_of(tool_with(PEOPLE))

        widget.set_table(None, stale=True)

        assert widget.cells == ()
        assert widget.notice == "Out of date"

    def test_the_column_view_has_no_selection_no_reordering_and_no_rubber_band(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = tool_with(PEOPLE)
        (view,) = of_type(tool, Gtk.ColumnView)

        assert isinstance(view.get_model(), Gtk.NoSelection)
        assert view.get_reorderable() is False
        assert view.get_enable_rubberband() is False
        columns = view.get_columns()
        assert columns.get_n_items() == 4
        for index in range(columns.get_n_items()):
            column = columns.get_item(index)
            assert isinstance(column, Gtk.ColumnViewColumn)
            assert column.get_header_menu() is None  # a header menu would be a popover
            assert column.get_sorter() is None  # and a header that is clicked is a sort


def spec_with(value: Json) -> UiSpec:
    return table_spec(value)


class TestCellsArePlainText:
    NAME = "<b>bold</b> _under &amp; <a href='x'>"
    HOSTILE = (
        "<b>bold</b> <span foreground='red'>x</span> _under_score &amp; <a href='file:///x'>l</a>"
    )

    def test_markup_and_mnemonics_in_a_cell_and_a_column_name_are_shown_as_they_are(
        self, echo_shell: Shell
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        value: Json = {
            "columns": columns((self.NAME, "string"), ("_n", "string")),
            "rows": [[self.HOSTILE, "_x"]],
        }
        tool = show_in_window(echo_shell, spec_with(value))
        labels = [w for w in descendants(widget_of(tool)) if isinstance(w, Gtk.Label)]
        texts = [label.get_text() for label in labels]

        assert self.NAME in texts  # the header, literally
        assert self.HOSTILE in texts  # and a cell
        assert "_x" in texts
        assert "_n" in texts
        assert labels
        assert not any(label.get_use_markup() for label in labels)
        assert not any(label.get_use_underline() for label in labels)

    def test_a_long_cell_is_cut_and_marked(self) -> None:
        shown = cell_text("y" * (MAX_SHOWN_CELL + 1), "string")

        assert shown == "y" * MAX_SHOWN_CELL + CUT_MARK
        assert cell_text("y" * MAX_SHOWN_CELL, "string") == "y" * MAX_SHOWN_CELL  # exactly the cap

    def test_a_cell_is_one_line_whatever_line_ends_it_holds(self) -> None:
        raw = "a\nb\rc\r\nd\x0be\x0cf\x85g" + chr(0x2028) + "h" + chr(0x2029) + "i"

        shown = cell_text(raw, "string")

        assert shown == "a b c  d e f g h i"
        line_ends = "\n\r\x0b\x0c\x85" + chr(0x2028) + chr(0x2029)
        assert not any(c in shown for c in line_ends)

    def test_a_cell_goes_through_the_display_transform_like_every_text(self) -> None:
        cluster = "e" + "́" * 100  # one grapheme cluster of a hundred code points

        shown = cell_text(cluster, "string")

        assert ZWSP in shown  # broken up, so that a layout can cope with it
        assert shown.replace(ZWSP, "") == cluster

    def test_the_kinds_of_cell(self) -> None:
        assert cell_text(True, "bool") == "yes"
        assert cell_text(False, "bool") == "no"
        assert cell_text(36, "int") == "36"
        assert cell_text(-(2**53 - 1), "int") == str(-(2**53 - 1))
        assert cell_text(1e-300, "float") == "1e-300"
        assert cell_text(1.7976931348623157e308, "float") == "1.7976931348623157e+308"
        assert cell_text(5.0, "float") == "5.0"

    def test_a_long_column_name_is_cut_and_marked_too(self) -> None:
        assert column_name("n" * MAX_SHOWN_NAME) == "n" * MAX_SHOWN_NAME
        assert column_name("n" * (MAX_SHOWN_NAME + 1)) == "n" * MAX_SHOWN_NAME + CUT_MARK
        assert column_name("a\nb") == "a b"


class TestBounds:
    def test_at_most_a_thousand_rows_are_shown_and_a_line_says_there_are_more(self) -> None:
        rows: list[JsonValue] = [[f"r{i}"] for i in range(MAX_SHOWN_ROWS + 500)]
        tool = tool_with({"columns": columns(("a", "string")), "rows": rows})
        widget = widget_of(tool)

        assert widget.rows == MAX_SHOWN_ROWS
        assert widget.cells[0] == ("r0",)
        assert widget.cells[-1] == (f"r{MAX_SHOWN_ROWS - 1}",)
        assert widget.notice == MORE_ROWS
        held = tool.runtime.store.value(StateKey("t"))
        assert isinstance(held, Table)
        assert len(held.rows) == MAX_SHOWN_ROWS + 500  # the state keeps all of it

    def test_exactly_the_cap_has_no_line_about_more(self) -> None:
        rows: list[JsonValue] = [["r"] for _ in range(MAX_SHOWN_ROWS)]
        widget = widget_of(tool_with({"columns": columns(("a", "string")), "rows": rows}))

        assert widget.rows == MAX_SHOWN_ROWS
        assert widget.notice == ""

    def test_the_cells_count_against_the_tools_text_budget(self) -> None:
        cell = "c" * 100
        rows: list[JsonValue] = [[cell] for _ in range(1000)]  # 100,000 characters: too many
        tool = tool_with({"columns": columns(("a", "string")), "rows": rows})
        widget = widget_of(tool)

        assert 0 < widget.rows < 1000
        assert widget.notice == MORE_ROWS
        assert tool.shown_text_total <= MAX_SHOWN_PER_TOOL
        assert tool.shown_text_total >= MAX_SHOWN_PER_TOOL - len(cell) - len(MORE_ROWS) - 100

    def test_rows_that_did_not_fit_appear_when_another_text_gives_room_back(self) -> None:
        cell = "c" * 100
        rows: list[JsonValue] = [[cell] for _ in range(1000)]
        tool = tool_with(
            {"columns": columns(("a", "string")), "rows": rows},
            before=[{"kind": "label", "bind": "s"}],  # claims 8,192 first
        )
        widget = widget_of(tool)
        before = widget.rows

        tool.runtime.patch_state({"s": ""})
        tool.flush()
        tool.flush()

        assert widget.rows > before
        assert tool.shown_text_total <= MAX_SHOWN_PER_TOOL

    def test_a_table_that_cannot_show_even_its_header_says_so_and_shows_it_when_room_comes(
        self,
    ) -> None:
        labels: list[Json] = [{"kind": "label", "bind": "s"}] * 8  # 8 * 8,192: the whole budget
        tool = tool_with(PEOPLE, before=labels)
        widget = widget_of(tool)

        assert widget.rows == 0
        assert widget.notice == NOT_SHOWN
        tool.runtime.patch_state({"s": ""})
        tool.flush()
        tool.flush()

        assert widget.rows == 2
        assert widget.cells[0] == ("Ada", "36", "9.5", "yes")
        assert widget.notice == ""

    def test_a_replaced_table_gives_back_what_the_old_one_held(self) -> None:
        tool = tool_with(PEOPLE)
        held = tool.shown_text_total

        tool.runtime.patch_state({"t": {"columns": columns(("a", "string")), "rows": [["x"]]}})
        tool.flush()

        assert tool.shown_text_total == len("a") + len("x")
        assert held > tool.shown_text_total

    def test_a_table_that_goes_empty_or_stale_holds_no_budget(self) -> None:
        tool = tool_with(PEOPLE)
        widget = widget_of(tool)

        tool.runtime.patch_state({"t": {"columns": [], "rows": []}})
        tool.flush()
        assert tool.shown_text_total == 0

        widget.set_table(None, stale=True)
        assert tool.shown_text_total == len("Out of date")

    def test_the_line_about_more_rows_is_given_back_when_the_table_is_replaced(self) -> None:
        rows: list[JsonValue] = [["r"] for _ in range(MAX_SHOWN_ROWS + 1)]
        tool = tool_with({"columns": columns(("a", "string")), "rows": rows})
        widget = widget_of(tool)
        assert widget.notice == MORE_ROWS

        tool.runtime.patch_state({"t": {"columns": columns(("a", "string")), "rows": [["x"]]}})
        tool.flush()

        assert widget.notice == ""
        assert tool.shown_text_total == len("a") + len("x")

    def test_closing_the_tool_lets_the_table_go(self) -> None:
        tool = tool_with(PEOPLE)
        widget = widget_of(tool)

        tool.close()

        assert tool.shown_text_total == 0
        assert widget.cells == ()
