"""The reserved trusted-chrome region (I5): where prompts and pickers will be shown (S18).

The region is a sibling of the canvas in the window's layout, in a vertical box that is not an
overlay, so it has its own allocation and the canvas has no way to cover it. The canvas clips
its own content (see `tumbleweed.shell.canvas`), so nothing it holds can paint or take input
over the region either. `tests/shell/test_shell_window.py` proves both, with hostile content.

The region is empty, and so has no height, until chrome puts something in it; S18 does.
"""

from typing import final

from tumbleweed.shell._gi import Gtk


@final
class ChromeRegion(Gtk.Box):
    """Holds trusted chrome. Only trusted code appends to it."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_name("tumbleweed-chrome")
        self.set_hexpand(True)
