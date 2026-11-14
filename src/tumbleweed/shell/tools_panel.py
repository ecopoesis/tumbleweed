"""The saved tools, their history and the tool grants, as a panel of the side column (S24, D30 (9)).

A fallback for when the model is not at hand, not app switching. An in-window panel, not a popover:
no surface of its own (#53). It draws what the viewmodel built: every name, description and tag is
text the tool's author wrote (I5), so each goes in through `plain_label` (never markup), capped, and
under a caption of ours that says whose words they are; the badge, the versions and every sentence
are ours. A click is the user's decision; the broker does the rest.
"""

from collections.abc import Callable
from functools import partial
from typing import final

from tumbleweed.shell._gi import Gtk
from tumbleweed.shell._widgets import plain_label
from tumbleweed.shell.viewmodel.display import display_text
from tumbleweed.shell.viewmodel.tools import (
    GrantRow,
    ToolRow,
    ToolsModel,
    ToolsPort,
    VersionRow,
)

OWN_WORDS = "The name and description below are the tool's own words, not checked."


@final
class ToolsPanel(Gtk.Box):
    """Hidden until opened; `open` fetches the lists."""

    def __init__(self, port: ToolsPort) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_name("tumbleweed-tools")
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_visible(False)
        self.model = ToolsModel(port, self)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.append(plain_label("Saved tools", css_classes=["heading"]))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda _button: self.set_visible(False))
        header.append(close)
        self.append(header)
        self.append(plain_label(OWN_WORDS, wrap=True, css_classes=["caption", "dim-label"]))
        self._note = plain_label("", wrap=True, css_classes=["caption", "dim-label"])
        self.append(self._note)
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_min_content_height(160)
        self._scroll.set_max_content_height(360)
        self._scroll.set_propagate_natural_height(True)
        self._rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._scroll.set_child(self._rows)
        self.append(self._scroll)
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

    # --- ToolsView --------------------------------------------------------------------------

    def busy(self, busy: bool) -> None:
        self._working = busy
        for button in self._buttons:
            button.set_sensitive(not busy)

    def present(
        self,
        tools: tuple[ToolRow, ...],
        history: tuple[VersionRow, ...],
        grants: tuple[GrantRow, ...],
        note: str | None,
    ) -> None:
        self._note.set_text(display_text(note) if note else "")
        child = self._rows.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._rows.remove(child)
            child = following
        self._buttons = []
        for row in tools:
            self._rows.append(self._tool(row, history))
        if grants:
            self._rows.append(plain_label("Access given to tools", css_classes=["heading"]))
            for grant in grants:
                self._rows.append(self._grant(grant))

    # --- rows -------------------------------------------------------------------------------

    def _button(self, label: str, action: Callable[[], object]) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.set_sensitive(not self._working)
        button.connect("clicked", lambda _button: action())
        self._buttons.append(button)
        return button

    def _tool(self, row: ToolRow, history: tuple[VersionRow, ...]) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(plain_label(display_text(row.name), wrap=True, css_classes=["heading"]))
        facts = f"Version {row.version}, saved {row.saved}"
        if row.taint is not None:
            facts += f". Made in a task that {row.taint}"
        box.append(plain_label(facts, wrap=True, css_classes=["caption", "dim-label"]))
        if row.description:
            box.append(plain_label(display_text(row.description), wrap=True))
        if row.tags:
            box.append(
                plain_label(display_text(row.tags), wrap=True, css_classes=["caption", "dim-label"])
            )
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.append(self._button("Open", partial(self.model.open, row.tool)))
        actions.append(self._button("Versions", partial(self.model.show_history, row.tool)))
        box.append(actions)
        for version in history:
            if version.tool == row.tool:
                box.append(self._version(version))
        return box

    def _version(self, row: VersionRow) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(12)
        facts = f"Version {row.version}" + (" (newest)" if row.newest else "")
        facts += f", saved {row.saved}"
        if row.taint is not None:
            facts += f", made in a task that {row.taint}"
        label = plain_label(facts, wrap=True, css_classes=["caption"])
        label.set_hexpand(True)
        box.append(label)
        box.append(
            self._button(
                "Open this version",
                partial(self.model.open, row.tool, row.version),
            )
        )
        if not row.newest:
            box.append(
                self._button(
                    "Make this the newest",
                    partial(self.model.restore, row.tool, row.version),
                )
            )
        return box

    def _grant(self, row: GrantRow) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        text.set_hexpand(True)
        text.append(
            plain_label(f"{display_text(row.tool_name)}, {row.version}: {row.access}", wrap=True)
        )
        if row.path is not None:
            text.append(
                plain_label(display_text(row.path), wrap=True, css_classes=["monospace", "caption"])
            )
        if row.expires is not None:
            text.append(plain_label(f"Until {row.expires}", css_classes=["caption", "dim-label"]))
        box.append(text)
        box.append(self._button("Revoke", partial(self.model.revoke, row.grant)))
        return box
