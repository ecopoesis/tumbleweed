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
from tumbleweed.ui.gtk.text import line_label

MAX_HEIGHT: Final = 180  # pixels the list shows before it scrolls


@final
class ChoiceList(Gtk.ScrolledWindow):
    """Single selection from `options`, shown as a list."""

    def __init__(self, options: Sequence[str], on_choose: Callable[[str], None]) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_height=True,
            max_content_height=MAX_HEIGHT,
        )
        self.add_css_class("frame")
        self.options = tuple(options)
        self._on_choose = on_choose
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.rows: list[Gtk.ListBoxRow] = []
        for option in self.options:
            row = Gtk.ListBoxRow()
            row.set_child(line_label(option))
            self._list.append(row)
            self.rows.append(row)
        self._list.connect("row-selected", self._on_row_selected)
        self.set_child(self._list)

    @property
    def selected(self) -> str | None:
        """The option whose row is selected."""
        row = self._list.get_selected_row()
        return None if row is None else self.options[row.get_index()]

    def select(self, option: str) -> None:
        """Select the row of `option` (nothing happens for a text that is not an option)."""
        if option in self.options and option != self.selected:
            self._list.select_row(self.rows[self.options.index(option)])

    def _on_row_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is not None:
            self._on_choose(self.options[row.get_index()])
