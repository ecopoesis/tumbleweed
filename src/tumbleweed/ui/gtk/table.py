"""The widget for a `table`: a column view of plain-text cells, bounded in rows and text (I4, I9).

A table value brings its own typed columns (a CSV viewer cannot know them in advance), so the
widget rebuilds its columns and rows whenever the entry changes. What it shows is bounded as the
other text is, and the value keeps all of it:

- at most `MAX_SHOWN_ROWS` rows;
- a cell shows at most `MAX_SHOWN_CELL` characters (then `CUT_MARK`) on one line (line ends become
  spaces, and a long cell is cut with an ellipsis), a column name at most `MAX_SHOWN_NAME`;
- all the cells and names together are claimed from the tool's text budget (`budget`), so a table
  cannot spend what the labels have: the rows that do not fit are left out, the table ends in
  `MORE_ROWS`, and they are added when room appears.

Every cell is a `Gtk.Label` set with `set_text` (never markup), after `display_text`; a column name
goes into the header as plain text too (the column view sets it with `gtk_label_new`, no markup, no
mnemonics: checked by `tests/ui/gtk/test_table.py`). Columns are typed: numbers are right-aligned
and a bool reads `yes` or `no`. The column view is hardened against surfaces: its columns cannot be
dragged around (no drag icon), there is no selection, no rubber band and no header menu.
"""

import logging
from typing import Final, final

from tumbleweed.shell._gi import Gio, GObject, Gtk, Pango
from tumbleweed.shell._widgets import plain_label
from tumbleweed.shell.viewmodel.display import display_text
from tumbleweed.ui.gtk.budget import CUT_MARK, MORE_ROWS, NOT_SHOWN
from tumbleweed.ui.gtk.context import BuildContext
from tumbleweed.ui.gtk.text import TextBlock
from tumbleweed.ui.spec.limits import MAX_TABLE_COLUMNS
from tumbleweed.ui.spec.values import ColumnType, Table

logger = logging.getLogger(__name__)

MAX_SHOWN_ROWS: Final = 1_000
MAX_SHOWN_CELL: Final = 256
MAX_SHOWN_NAME: Final = 64
CELL_WIDTH_CHARS: Final = 48  # a cell's label is this wide at most, and ends in an ellipsis
HEIGHT: Final = 300  # pixels of rows a table shows before it scrolls
ROW_HEIGHT: Final = 30  # a rough one, for the height of a table of a few rows
HEADER_HEIGHT: Final = 40

_LINE_ENDS: Final = dict.fromkeys((10, 11, 12, 13, 133, 8232, 8233), " ")
_NUMERIC: Final[frozenset[ColumnType]] = frozenset({"int", "float"})


def cell_text(cell: bool | int | float | str) -> str:
    """What a cell shows: one line, at most `MAX_SHOWN_CELL` characters, made safe for a label."""
    if isinstance(cell, bool):
        return "yes" if cell else "no"
    if isinstance(cell, int | float):
        return display_text(repr(cell))  # (an int or a float: at most a few dozen characters)
    text = cell[:MAX_SHOWN_CELL].translate(_LINE_ENDS)
    if len(cell) > MAX_SHOWN_CELL:
        text += CUT_MARK
    return display_text(text)


def column_name(name: str) -> str:
    text = name[:MAX_SHOWN_NAME].translate(_LINE_ENDS)
    return display_text(text + CUT_MARK if len(name) > MAX_SHOWN_NAME else text)


@final
class _Row(GObject.Object):
    """A row of the model: the texts of its cells, made when the table was shown."""

    def __init__(self, cells: tuple[str, ...]) -> None:
        super().__init__()
        self.cells = cells


@final
class TableWidget(Gtk.Box):
    """A table value as a column view, or a line saying why there is none."""

    def __init__(self, ctx: BuildContext) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._budget = ctx.budget
        self._view = Gtk.ColumnView(
            reorderable=False, enable_rubberband=False, show_row_separators=True
        )
        self._scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=self._view,
        )
        self._scrolled.add_css_class("frame")
        self._scrolled.set_visible(False)
        self._notice = TextBlock(budget=ctx.budget)
        self._notice.set_visible(False)
        self.append(self._scrolled)
        self.append(self._notice)
        self._numeric: tuple[bool, ...] = ()
        self._shown: tuple[Table | None, bool] = (None, False)
        self.rows = 0  # rows on show
        self._texts: tuple[tuple[str, ...], ...] = ()
        # One factory per possible column, made once: a table brings its own columns, so the
        # factories are not made per value, and every handler is the tool's guarded one, which
        # is undone when the tool is closed.
        self._factories = tuple(self._factory(ctx, index) for index in range(MAX_TABLE_COLUMNS))

    def _factory(self, ctx: BuildContext, column: int) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()

        def setup(_factory: object, item: object) -> None:
            if isinstance(item, Gtk.ListItem):
                label = plain_label("")
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_max_width_chars(CELL_WIDTH_CHARS)
                item.set_child(label)

        def bind(_factory: object, item: object) -> None:
            if not isinstance(item, Gtk.ListItem):
                return
            row, label = item.get_item(), item.get_child()
            if isinstance(row, _Row) and isinstance(label, Gtk.Label):
                label.set_text(row.cells[column] if column < len(row.cells) else "")
                numeric = column < len(self._numeric) and self._numeric[column]
                label.set_xalign(1.0 if numeric else 0.0)

        ctx.connect(factory, "setup", setup)
        ctx.connect(factory, "bind", bind)
        return factory

    @property
    def notice(self) -> str:
        """The line of text under (or instead of) the table ('' if none)."""
        return self._notice.shown_text if self._notice.get_visible() else ""

    @property
    def cells(self) -> tuple[tuple[str, ...], ...]:
        """The text of every cell on show, row by row."""
        return self._texts

    @property
    def column_names(self) -> tuple[str, ...]:
        """The titles of the columns on show."""
        columns = self._view.get_columns()
        titles = (columns.get_item(i) for i in range(columns.get_n_items()))
        return tuple(
            column.get_title() or ""
            for column in titles
            if isinstance(column, Gtk.ColumnViewColumn)
        )

    def release(self) -> None:
        """Let the rows go and stop waiting for room."""
        self._budget.stop_waiting(self)
        self._budget.claim(self, 0)
        self._empty()
        self._hide_notice()

    def refresh(self) -> None:
        """Show the table again, as far as the budget allows now."""
        table, stale = self._shown
        self.set_table(table, stale=stale)

    def set_table(self, table: Table | None, *, stale: bool) -> None:
        """Show `table` (or a mark that it is out of date, or nothing for an empty one).

        The claim on the budget is made once, replacing the last: letting it go first and
        claiming again would announce room (to whoever waits) on every call, and two tables cut
        by the budget would then rebuild each other for ever.
        """
        self._shown = (table, stale)
        self._budget.stop_waiting(self)
        self._empty()
        if stale:
            self._budget.claim(self, 0)
            self._say("", stale=True)
            return
        if table is None or not table.columns:
            self._budget.claim(self, 0)
            self._hide_notice()
            return
        names = tuple(column_name(column.name) for column in table.columns)
        # What is left, and what the table already holds, its notice's line included (which is
        # reserved again below if it is still needed: counted twice, a refresh would lose rows).
        room = self._budget.room(self) + self._budget.held(self._notice)
        header = sum(len(name) for name in names)
        if header > room:
            self._budget.claim(self, 0)
            self._say(NOT_SHOWN)
            self._budget.wait_for_room(self, self.refresh)
            return
        texts: list[tuple[str, ...]] = []
        costs: list[int] = []
        for row in table.rows[:MAX_SHOWN_ROWS]:
            cells = tuple(cell_text(cell) for cell in row)
            cost = sum(len(cell) for cell in cells)
            if header + sum(costs) + cost > room:
                break
            costs.append(cost)
            texts.append(cells)
        cut = len(texts) < len(table.rows)
        # The line that says rows are left out must itself fit: make room for it.
        while cut and texts and header + sum(costs) + len(MORE_ROWS) > room:
            texts.pop()
            costs.pop()
        self._budget.claim(self, header + sum(costs))  # it fits: within `room`, own claim counted
        if cut:
            self._say(MORE_ROWS)
            if len(texts) < min(len(table.rows), MAX_SHOWN_ROWS):  # cut by the budget, not the cap
                self._budget.wait_for_room(self, self.refresh)
        else:
            self._hide_notice()
        self._fill(table, names, texts)

    def _fill(self, table: Table, names: tuple[str, ...], texts: list[tuple[str, ...]]) -> None:
        self._numeric = tuple(column.type in _NUMERIC for column in table.columns)
        store = Gio.ListStore.new(_Row)
        for cells in texts:
            store.append(_Row(cells))
        for index, name in enumerate(names):
            column = Gtk.ColumnViewColumn.new(name, self._factories[index])
            column.set_resizable(True)
            self._view.append_column(column)
        self._view.set_model(Gtk.NoSelection.new(store))
        self._scrolled.set_min_content_height(min(HEIGHT, HEADER_HEIGHT + ROW_HEIGHT * len(texts)))
        self._scrolled.set_visible(True)
        self.rows = len(texts)
        self._texts = tuple(texts)

    def _hide_notice(self) -> None:
        self._notice.set_text("")  # which gives back what the line had claimed of the budget
        self._notice.set_visible(False)

    def _say(self, text: str, *, stale: bool = False) -> None:
        self._notice.set_text(text, stale=stale)
        self._notice.set_visible(True)

    def _empty(self) -> None:
        columns = self._view.get_columns()
        for _ in range(columns.get_n_items()):
            column = columns.get_item(0)
            if isinstance(column, Gtk.ColumnViewColumn):
                self._view.remove_column(column)
        self._view.set_model(None)
        self._numeric = ()
        self.rows = 0
        self._texts = ()
        self._scrolled.set_visible(False)
        self._notice.set_visible(False)
