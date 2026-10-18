"""Helpers of the renderer's GTK tests.

`gi` is imported inside the functions, never at module level, as in `tests/shell/gtk_support.py`:
on a host where PyGObject cannot load, the gate in `tests/plugins/host.py` skips the `gtk` tests
with the reason instead of breaking the collection of every other test.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from tests.shell.gtk_support import descendants, pump
from tests.ui.spec.support import Json, build, golden_text
from tumbleweed.ui.spec import UiSpec, parse_ui_spec, validate_ui_spec

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

__all__ = [
    "descendants",
    "golden_spec",
    "labels_text",
    "of_type",
    "pump",
    "render",
    "spec_of",
]


def golden_spec(name: str) -> UiSpec:
    return parse_ui_spec(golden_text(name))


def spec_of(root: Json, state: Sequence[Json] = (), handlers: Sequence[Json] = ()) -> UiSpec:
    return validate_ui_spec(build(root, state, handlers))


def render(spec: UiSpec) -> "RenderedTool":
    from tumbleweed.ui.gtk import RenderedTool

    return RenderedTool(spec)


def of_type[W: "Gtk.Widget"](tool: "RenderedTool", kind: type[W]) -> list[W]:
    """Every widget of the tool that is a `kind` (not a subclass of something else's internals)."""
    return [w for w in descendants(tool.widget) if isinstance(w, kind)]


def labels_text(tool: "RenderedTool") -> list[str]:
    """The text the tool shows, in tree order: its text blocks and its placeholder lines.

    (Not the labels inside other widgets: a button's, a slider's value, an option's.)
    """
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk.text import TextBlock

    tool.flush()  # texts are shown once per frame: let the ones waiting be shown
    found: list[str] = []
    for widget in descendants(tool.widget):
        parent = widget.get_parent()
        if isinstance(widget, TextBlock):
            found.append(widget.shown_text)
        elif isinstance(widget, Gtk.Label) and (
            parent is None
            or (isinstance(parent, Gtk.Box | Gtk.Grid) and not isinstance(parent, TextBlock))
        ):
            found.append(widget.get_text())
    return found
