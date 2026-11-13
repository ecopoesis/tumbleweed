"""The undo list as a panel in the trusted chrome region (S21, D28).

An in-window panel, not a popover: no surface of its own (#53). It lists what the viewmodel built
(`UndoRow`: plain labels only, `set_text`) with an Undo button on each row that can be undone. The
click is the user's decision; the broker does the rest and snapshots first.
"""

from typing import final

from tumbleweed.core.ids import SnapshotId
from tumbleweed.shell._gi import Gtk
from tumbleweed.shell._widgets import plain_label
from tumbleweed.shell.viewmodel.display import display_text
from tumbleweed.shell.viewmodel.undo import UndoModel, UndoPort, UndoRow


@final
class UndoPanel(Gtk.Box):
    """Hidden until opened; `open` fetches the list."""

    def __init__(self, port: UndoPort) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_name("tumbleweed-undo")
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_visible(False)
        self.model = UndoModel(port, self)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.append(
            plain_label("Recent changes made by code the assistant ran", css_classes=["heading"])
        )
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)
        self._close = Gtk.Button(label="Close")
        self._close.connect("clicked", lambda _button: self.set_visible(False))
        header.append(self._close)
        self.append(header)
        self._note = plain_label("", wrap=True, css_classes=["caption", "dim-label"])
        self.append(self._note)
        self._rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.append(self._rows)
        self._buttons: list[Gtk.Button] = []
        self._working = False

    def open(self) -> None:
        self.set_visible(True)
        self.model.refresh()

    def toggle(self) -> None:
        if self.get_visible():
            self.set_visible(False)
        else:
            self.open()

    # --- UndoView ---------------------------------------------------------------------------

    def busy(self, busy: bool) -> None:
        self._working = busy
        for button in self._buttons:
            button.set_sensitive(not busy)

    def present(self, rows: tuple[UndoRow, ...], note: str | None) -> None:
        self._note.set_text(display_text(note) if note else "")
        child = self._rows.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._rows.remove(child)
            child = following
        self._buttons = []
        for row in rows:
            self._rows.append(self._row(row))

    def _row(self, row: UndoRow) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        text.set_hexpand(True)
        suffix = "" if row.status is None else f" ({row.status})"
        text.append(plain_label(f"{row.when}  {row.what}{suffix}", wrap=True))
        for path in row.paths:
            text.append(
                plain_label(display_text(path), wrap=True, css_classes=["monospace", "caption"])
            )
        if row.more:
            text.append(plain_label(f"and {row.more} more", css_classes=["caption", "dim-label"]))
        box.append(text)
        button = Gtk.Button(label="Undo")
        button.set_sensitive(row.undoable and not self._working)
        button.connect("clicked", lambda _button, snapshot=row.snapshot: self._clicked(snapshot))
        self._buttons.append(button)
        box.append(button)
        return box

    def _clicked(self, snapshot: SnapshotId) -> None:
        self.model.undo(snapshot)
