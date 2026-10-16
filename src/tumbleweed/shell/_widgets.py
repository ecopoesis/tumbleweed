"""Small widget helpers shared by the shell's panels."""

from collections.abc import Iterable

from tumbleweed.shell._gi import Gtk, Pango


def plain_label(
    text: str,
    *,
    wrap: bool = False,
    selectable: bool = False,
    css_classes: Iterable[str] = (),
) -> Gtk.Label:
    """A label that shows `text` literally: set with `set_text`, markup and mnemonics off (I4).

    Every string that is not a constant of ours (model output, transcripts) goes into the UI
    through a label like this one.
    """
    label = Gtk.Label(use_markup=False, use_underline=False, xalign=0.0, selectable=selectable)
    label.set_text(text)
    if wrap:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for css_class in css_classes:
        label.add_css_class(css_class)
    return label
