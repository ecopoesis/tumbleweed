"""The widget for a `dropdown`: a list of the options in place, not a popup (I5, #53).

A `Gtk.DropDown` opens a popover, which is a window of its own that the canvas cannot clip, so the
renderer never creates one from a spec. `ChoiceList` shows the options of an `enum` entry as the
rows of a `Gtk.ListBox`, in a scrolled window of bounded height (an enum has up to 64 options of up
to 128 characters), and selecting a row is choosing the option. It is two-way bound like any input:
the row selected follows the entry, and `on_choose` is told what the user chose.
"""

from collections.abc import Callable, Sequence
from typing import Final, final

from tumbleweed.shell._gi import Gtk
from tumbleweed.ui.gtk.budget import MORE_OPTIONS, TextBudget
from tumbleweed.ui.gtk.text import line_label

MAX_HEIGHT: Final = 180  # pixels the list shows before it scrolls


def _ignore(option: str) -> None:
    """What a released list tells: nothing."""


@final
class ChoiceList(Gtk.ScrolledWindow):
    """Single selection from `options`, shown as a list."""

    def __init__(
        self,
        options: Sequence[str],
        on_choose: Callable[[str], None],
        budget: TextBudget | None = None,
    ) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_height=True,
            max_content_height=MAX_HEIGHT,
        )
        self.add_css_class("frame")
        self._on_choose = on_choose
        self._current: int | None = None  # the row last chosen, which stays chosen
        self._all = tuple(options)
        self._budget = budget
        self._wanted: str | None = None  # what the entry holds, shown or not (yet)
        # BROWSE: a row, once selected, cannot be deselected (Ctrl+click and Ctrl+Space would
        # leave the list showing nothing while the entry still holds an option).
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.rows: list[Gtk.ListBoxRow] = []
        self.options: tuple[str, ...] = ()
        self._more: Gtk.ListBoxRow | None = None
        self._handler = self._list.connect("row-selected", self._on_row_selected)
        self.set_child(self._list)
        self._grow()

    def _grow(self) -> None:
        """Build the rows that fit in the tool's budget; the rest wait for room."""
        for option in self._all[len(self.options) :]:
            row = Gtk.ListBoxRow()
            if self._budget is not None and not self._budget.fits(row, option):
                break  # the rest would not be shown: a mark says so, and they are retried
            row.set_child(line_label(option))
            self._list.insert(row, len(self.rows))
            self.rows.append(row)
            self.options = (*self.options, option)
        if len(self.options) < len(self._all):
            if self._more is None:
                self._more = Gtk.ListBoxRow(selectable=False, activatable=False)
                self._more.set_child(line_label(MORE_OPTIONS))
                self._list.append(self._more)
            if self._budget is not None:
                self._budget.wait_for_room(self, self._grow)
        elif self._more is not None:
            self._list.remove(self._more)
            self._more = None
        if self._wanted is not None:
            self.select(self._wanted)

    def release(self) -> None:
        """Let go of the signal handler, which refers back to this widget, and of the callback."""
        if self._handler:
            self._list.disconnect(self._handler)
            self._handler = 0
        if self._budget is not None:
            self._budget.stop_waiting(self)
        self._on_choose = _ignore

    @property
    def selected(self) -> str | None:
        """The option whose row is selected."""
        row = self._list.get_selected_row()
        if row is None or row.get_index() >= len(self.options):
            return None
        return self.options[row.get_index()]

    def select(self, option: str) -> None:
        """Select the row of `option` (nothing happens for a text that is not an option; an
        option that is not shown yet is selected when it is)."""
        self._wanted = option
        if option in self.options and option != self.selected:
            self._list.select_row(self.rows[self.options.index(option)])

    def _on_row_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            # BROWSE keeps a row selected, but not every route to "none" is closed (a select-all
            # action, a keyboard shortcut): the entry still holds its option, so put the row back.
            if self._current is not None:
                self._list.select_row(self.rows[self._current])
            return
        if row.get_index() < len(self.options):
            self._current = row.get_index()
            self._on_choose(self.options[row.get_index()])
