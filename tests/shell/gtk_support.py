"""Helpers for the GTK tests of the shell.

`gi` is imported inside the functions, never at module level: on a host where PyGObject cannot
load, the gate in `tests/plugins/host.py` skips the `gtk` tests with the reason, instead of this
module breaking the collection of every other test.

Every wait here has a deadline: the main loop is iterated, never blocked on.
"""

import functools
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gi.repository import Graphene

    from tumbleweed.shell._gi import Gio, Gtk
    from tumbleweed.shell.app import ShellApplication
    from tumbleweed.shell.chat_panel import MessageRow
    from tumbleweed.shell.window import ShellWindow

DEADLINE_SECONDS = 5.0
_ITERATIONS_PER_TURN = 1000  # a source that always fires must not hold the loop here forever

type Rgb = tuple[float, float, float]


def pump_until(condition: Callable[[], bool], *, timeout: float = DEADLINE_SECONDS) -> bool:
    """Run the default main context until `condition()` holds. False if the deadline passed."""
    from tumbleweed.shell._gi import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while True:
        for _ in range(_ITERATIONS_PER_TURN):
            if not context.iteration(False):
                break
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def pump(seconds: float) -> None:
    """Let the main loop run for `seconds` (layout, frame clock, idle callbacks)."""
    pump_until(lambda: False, timeout=seconds)


def descendants(widget: "Gtk.Widget") -> Iterator["Gtk.Widget"]:
    """`widget` and everything below it, depth first."""
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from descendants(child)
        child = child.get_next_sibling()


def ancestor_of[W: "Gtk.Widget"](widget: "Gtk.Widget", kind: type[W]) -> W | None:
    """The nearest ancestor of `widget` that is a `kind`."""
    parent = widget.get_parent()
    while parent is not None:
        if isinstance(parent, kind):
            return parent
        parent = parent.get_parent()
    return None


def labels_in(widget: "Gtk.Widget") -> list["Gtk.Label"]:
    from tumbleweed.shell._gi import Gtk

    return [w for w in descendants(widget) if isinstance(w, Gtk.Label)]


@dataclass(frozen=True)
class Shell:
    """An application and its one window, built for a test."""

    app: "ShellApplication"
    window: "ShellWindow"


def key_controller(widget: "Gtk.Widget") -> "Gtk.EventControllerKey":
    """The capture-phase key controller on `widget`: the input's Enter handling.

    A text view has a key controller of its own (bubble phase); ours runs before it.
    """
    from tumbleweed.shell._gi import Gtk

    found = [
        controller
        for controller in widget.observe_controllers()
        if isinstance(controller, Gtk.EventControllerKey)
        and controller.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
    ]
    assert len(found) == 1, f"expected one capture-phase key controller, found {len(found)}"
    return found[0]


def press(widget: "Gtk.Widget", keyval: int, state: int = 0) -> bool:
    """Deliver a key press to the widget's key controller; True if the controller handled it."""
    from tumbleweed.shell._gi import Gdk

    controller = key_controller(widget)
    return bool(controller.emit("key-pressed", keyval, 0, Gdk.ModifierType(state)))


def rect(x: float, y: float, width: float, height: float) -> "Graphene.Rect":
    import gi

    gi.require_version("Graphene", "1.0")
    from gi.repository import Graphene

    # pygobject-stubs types Graphene.Rect() as an untyped constructor.
    return Graphene.Rect().init(x, y, width, height)  # type: ignore[no-untyped-call]


@functools.cache
def _solid_factory() -> Callable[[Rgb, bool, tuple[int, int]], "Gtk.Widget"]:
    """Widgets that paint one color; defined once, because a GType name can be registered once."""
    from tumbleweed.shell._gi import Gdk, Gtk

    class Solid(Gtk.Widget):
        __gtype_name__ = "TumbleweedTestSolid"

        def __init__(self, rgb: Rgb, leak: bool, size: tuple[int, int]) -> None:
            super().__init__()
            self._color = Gdk.RGBA()
            self._color.red, self._color.green, self._color.blue, self._color.alpha = (*rgb, 1.0)
            self._leak = leak
            self.set_size_request(*size)

        def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
            # A leaky widget paints far outside its own allocation: what a hostile child of the
            # canvas would do to cover the chrome around it.
            if self._leak:
                area = rect(-4000, -4000, 8000, 8000)
            else:
                area = rect(0, 0, self.get_width(), self.get_height())
            snapshot.append_color(self._color, area)

        def do_contains(self, x: float, y: float) -> bool:
            # A leaky widget also claims every point as its own, so that a click anywhere would
            # land on it if nothing clipped it.
            return self._leak or (0 <= x < self.get_width() and 0 <= y < self.get_height())

    return Solid


def solid(rgb: Rgb, *, leak: bool = False, size: tuple[int, int] = (10, 10)) -> "Gtk.Widget":
    """A widget that paints `rgb` over its allocation; with `leak`, it paints and claims clicks
    over a huge area instead."""
    return _solid_factory()(rgb, leak, size)


@dataclass(frozen=True)
class Pixels:
    """A rendering of a window, as RGBA bytes."""

    width: int
    height: int
    stride: int
    data: bytes
    scale: float  # texture pixels per window pixel

    def at(self, x: float, y: float) -> tuple[int, int, int]:
        """The color at window coordinates (x, y)."""
        column, row = int(x * self.scale), int(y * self.scale)
        assert 0 <= column < self.width
        assert 0 <= row < self.height
        offset = row * self.stride + column * 4
        return (self.data[offset], self.data[offset + 1], self.data[offset + 2])


def render(window: "Gtk.Window") -> Pixels:
    """Render what the window shows right now, with the window's own renderer."""
    from tumbleweed.shell._gi import Gdk, Gtk

    native = window.get_native()
    assert native is not None
    renderer = native.get_renderer()
    assert renderer is not None
    width, height = window.get_width(), window.get_height()
    snapshot = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(window).snapshot(snapshot, width, height)
    node = snapshot.to_node()
    assert node is not None
    texture = renderer.render_texture(node, rect(0, 0, width, height))
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    data, stride = downloader.download_bytes()
    raw = data.get_data()
    assert raw is not None
    return Pixels(
        width=texture.get_width(),
        height=texture.get_height(),
        stride=stride,
        data=bytes(raw),
        scale=texture.get_width() / width,
    )


def rows(shell: Shell) -> list[tuple[str, str, str]]:
    """(who, text, status) of each message row, in the order shown."""
    from tumbleweed.shell.chat_panel import MessageRow

    return [
        (
            w.role_label.get_text(),
            w.shown_text,
            w.status.get_text() if w.status.get_visible() else "",
        )
        for w in descendants(shell.window.chat_panel.transcript_list)
        if isinstance(w, MessageRow)
    ]


def body_laid_out(row: "MessageRow") -> bool:
    """Whether every paragraph label of the row has been given a size."""
    return bool(row.paragraphs) and all(label.get_height() > 0 for label in row.paragraphs)


def type_text(shell: Shell, text: str) -> None:
    shell.window.chat_panel.input_view.get_buffer().set_text(text)


def input_text(shell: Shell) -> str:
    buffer = shell.window.chat_panel.input_view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


def click_send(shell: Shell) -> None:
    shell.window.chat_panel.send_button.emit("clicked")


def show(shell: Shell) -> None:
    """Present the window and wait until it has been laid out."""
    shell.window.present()
    assert pump_until(lambda: shell.window.get_mapped() and shell.window.chat_panel.get_width() > 0)


type Bounds = tuple[float, float, float, float]


def bounds(widget: "Gtk.Widget", relative_to: "Gtk.Widget") -> Bounds:
    """(left, top, right, bottom) of `widget` in the coordinates of `relative_to`."""
    found, area = widget.compute_bounds(relative_to)
    assert found
    return (
        area.get_x(),
        area.get_y(),
        area.get_x() + area.get_width(),
        area.get_y() + area.get_height(),
    )


def clipboard_text(widget: "Gtk.Widget") -> str | None:
    """What the widget's clipboard holds as text, read through the main loop with a deadline."""
    from tumbleweed.shell._gi import GLib

    clipboard = widget.get_clipboard()
    result: list[str | None] = []

    def done(_source: object, outcome: "Gio.AsyncResult") -> None:
        try:
            result.append(clipboard.read_text_finish(outcome))
        except GLib.Error:
            result.append(None)

    clipboard.read_text_async(None, done)
    pump_until(lambda: bool(result), timeout=DEADLINE_SECONDS)
    return result[0] if result else None


def set_clipboard_text(widget: "Gtk.Widget", text: str) -> None:
    widget.get_clipboard().set(text)
    pump(0.05)


def primary_text(widget: "Gtk.Widget") -> str | None:
    """What the widget's primary selection (middle-click paste) holds as text."""
    from tumbleweed.shell._gi import GLib

    clipboard = widget.get_primary_clipboard()
    result: list[str | None] = []

    def done(_source: object, outcome: "Gio.AsyncResult") -> None:
        try:
            result.append(clipboard.read_text_finish(outcome))
        except GLib.Error:
            result.append(None)

    clipboard.read_text_async(None, done)
    pump_until(lambda: bool(result), timeout=DEADLINE_SECONDS)
    return result[0] if result else None
