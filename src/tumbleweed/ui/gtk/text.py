"""Text from a spec in a label: plain, bounded, and visibly marked when it is out of date.

Everything a spec shows as text goes into a `Gtk.Label` through `plain_label` (`set_text`, markup
and mnemonics off: I4), after `display_text`, which keeps what a label has to lay out bounded (one
huge grapheme cluster, or a long run with no break opportunity, would wreck a layout: I5, I9).
Text that can be long, a bound `string` entry of up to 65,536 characters, is shown as a column of
labels of at most `MAX_CHUNK` characters (`DisplayChunks`), as the transcript does: a label's
layout cost is superlinear in its text, and a bound label is laid out again after every edit.
"""

from collections.abc import Sequence
from typing import Final, final

from tumbleweed.shell._gi import Gtk
from tumbleweed.shell._widgets import plain_label
from tumbleweed.shell.viewmodel.display import DisplayChunks, display_text

STALE_TEXT: Final = "Out of date"
ERROR_CLASS: Final = "error"


def line_label(text: str, *, css_classes: Sequence[str] = ()) -> Gtk.Label:
    """A wrapping label for a short line (a button, a check box, an option): plain, bounded."""
    return plain_label(display_text(text), wrap=True, css_classes=css_classes)


def placeholder(message: str) -> Gtk.Widget:
    """What stands where a node could not be shown: a plain, marked line of text of ours."""
    return line_label(message, css_classes=[ERROR_CLASS])


@final
class TextBlock(Gtk.Box):
    """Plain text of any allowed length, as a column of labels."""

    def __init__(self, *, css_classes: Sequence[str] = ()) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._css_classes = tuple(css_classes)
        self._labels: list[Gtk.Label] = []
        self._text: str | None = None
        self._stale = False

    @property
    def shown_text(self) -> str:
        """What the labels show, joined as the chunks are cut (display text, no source text)."""
        return "".join(label.get_text() for label in self._labels)

    @property
    def stale(self) -> bool:
        return self._stale

    def set_text(self, text: str, *, stale: bool = False) -> None:
        """Show `text`, or, if `stale`, a mark that it is out of date instead of the old text."""
        if text == self._text and stale == self._stale:
            return
        self._text, self._stale = text, stale
        chunks = DisplayChunks()
        chunks.feed(STALE_TEXT if stale else text)
        shown = chunks.chunks
        while len(self._labels) > len(shown):
            self.remove(self._labels.pop())
        for index, chunk in enumerate(shown):
            if index == len(self._labels):
                label = plain_label("", wrap=True, css_classes=self._css_classes)
                self._labels.append(label)
                self.append(label)
            label = self._labels[index]
            label.set_text(chunk)
            if stale:
                label.add_css_class(ERROR_CLASS)
            else:
                label.remove_css_class(ERROR_CLASS)
