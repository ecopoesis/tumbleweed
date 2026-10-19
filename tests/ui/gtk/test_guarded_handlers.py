"""The boundary `BuildContext.connect` puts around every signal handler of a rendered tool.

Nothing a handler raises may reach GTK's signal machinery: it is logged, and every bound widget
is shown the state's value again, without that being taken for an edit by the user.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from unittest import mock

import pytest

from tests.ui.gtk.support import labels_text, of_type, spec_of
from tumbleweed.ui.gtk.context import BuildContext
from tumbleweed.ui.gtk.guard import SignalHandler
from tumbleweed.ui.spec.names import StateKey

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.ui.spec.support import Json
    from tumbleweed.ui.gtk import RenderedTool

pytestmark = pytest.mark.gtk

FAILED = "a signal handler of a rendered widget failed"


class FakeWidget:
    """Captures what `connect` is given, so the wrapper can be called directly."""

    def __init__(self) -> None:
        self.callbacks: list[Callable[[object, object], object]] = []
        self.disconnected: list[int] = []

    def connect(self, signal: str, handler: SignalHandler, /) -> int:
        # sound: `connect` always hands over the context's `guarded(*args)`, which takes any number
        self.callbacks.append(cast("Callable[[object, object], object]", handler))
        return len(self.callbacks)

    def disconnect(self, handler_id: int, /) -> None:
        self.disconnected.append(handler_id)


def context_of(tool: "RenderedTool") -> BuildContext:
    return tool._context  # noqa: SLF001  (the boundary under test is the context's)


def two_switches() -> "RenderedTool":
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [
        {"kind": "bool", "key": "a", "default": False},
        {"kind": "bool", "key": "b", "default": False},
    ]
    root: Json = {
        "kind": "box",
        "children": [{"kind": "switch", "bind": "a"}, {"kind": "switch", "bind": "b"}],
    }
    return RenderedTool(spec_of(root, state))


@pytest.mark.parametrize("error", [ZeroDivisionError("x"), ValueError("x"), RuntimeError("x")])
def test_the_wrapper_contains_any_exception_and_answers_not_handled(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    tool = two_switches()
    widget = FakeWidget()

    def handler(_emitter: object) -> object:
        raise error

    context_of(tool).connect(widget, "toggled", handler)
    with caplog.at_level("ERROR"):
        answer = widget.callbacks[0](
            object(), object()
        )  # as GTK would call it: no exception comes out

    assert answer is False
    assert FAILED in caplog.text


def test_the_wrapper_passes_an_answer_through_and_is_undone_on_close() -> None:
    tool = two_switches()
    widget = FakeWidget()
    context_of(tool).connect(widget, "x", lambda _emitter, _arg: True)

    assert widget.callbacks[0](object(), 1) is True
    tool.close()

    assert widget.disconnected == [1]


def test_a_failing_handler_puts_back_a_widget_that_shows_what_the_state_does_not_hold() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [{"kind": "bool", "key": "a", "default": False}]
    tool = RenderedTool(spec_of({"kind": "check", "bind": "a", "label": "x"}, state))
    (check,) = of_type(tool, Gtk.CheckButton)
    events: list[object] = []
    tool.runtime.store.subscribe(events.append)

    # `_guarded` is what the context's own `user_input` calls, once it has found the change to be
    # the user's: a failure there is one the wrapper must catch.
    with mock.patch.object(BuildContext, "_guarded", side_effect=RuntimeError("boom")) as guarded:
        check.set_active(True)  # the handler fails: the state still says False

    assert check.get_active() is False  # shown the state's value again
    assert tool.runtime.store.value(StateKey("a")) is False
    assert guarded.call_count == 1  # putting it back was not taken for another edit by the user
    assert events == []


def test_a_switch_is_put_back_too() -> None:
    from tumbleweed.shell._gi import Gtk

    tool = two_switches()
    first, second = of_type(tool, Gtk.Switch)

    with mock.patch.object(BuildContext, "_guarded", side_effect=RuntimeError("boom")):
        first.set_active(True)

    assert (first.get_active(), second.get_active()) == (False, False)


def test_a_binding_that_cannot_be_put_back_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tumbleweed.shell._gi import Gtk

    tool = two_switches()
    first, second = of_type(tool, Gtk.Switch)

    def broken(value: object, stale: bool) -> None:
        raise RuntimeError("cannot show")

    # the first binding's update fails on resync
    context_of(tool)._bindings[0].update = broken  # noqa: SLF001

    with (
        caplog.at_level("ERROR"),
        mock.patch.object(BuildContext, "_guarded", side_effect=RuntimeError("boom")),
    ):
        second.set_active(True)  # fails, then the resync runs through the broken binding first

    assert second.get_active() is False  # the later binding was still restored
    assert "could not be put back" in caplog.text
    assert FAILED in caplog.text
    assert first.get_active() is False


def test_a_stale_entry_stays_stale_when_the_widgets_are_put_back() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [
        {"kind": "bool", "key": "a", "default": False},
        {"kind": "string", "key": "s", "default": "hello"},
    ]
    root: Json = {
        "kind": "box",
        "children": [{"kind": "switch", "bind": "a"}, {"kind": "label", "bind": "s"}],
    }
    tool = RenderedTool(spec_of(root, state))
    (switch,) = of_type(tool, Gtk.Switch)
    assert labels_text(tool) == ["hello"]

    with (
        mock.patch.object(tool.runtime.store, "is_stale", return_value=True),
        mock.patch.object(BuildContext, "_guarded", side_effect=RuntimeError("boom")),
    ):
        switch.set_active(True)

    assert labels_text(tool) == ["Out of date"]  # resync passed the mark on: it did not hide it


def test_a_dropdown_row_handler_is_behind_the_boundary_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [{"kind": "enum", "key": "d", "options": ["a", "b"], "default": "a"}]
    tool = RenderedTool(spec_of({"kind": "dropdown", "bind": "d"}, state))
    (lst,) = of_type(tool, Gtk.ListBox)

    with (
        caplog.at_level("ERROR"),
        mock.patch.object(BuildContext, "_guarded", side_effect=RuntimeError("boom")),
    ):
        lst.select_row(lst.get_row_at_index(1))

    assert FAILED in caplog.text
    assert lst.get_selected_row() is lst.get_row_at_index(0)  # the state's option is shown again


def test_a_text_gesture_handler_is_behind_the_boundary_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

    state: list[Json] = [{"kind": "string", "key": "s", "default": ""}]
    tool = RenderedTool(spec_of({"kind": "entry", "bind": "s"}, state))
    (entry,) = of_type(tool, Gtk.Entry)
    delegate = entry.get_delegate()
    assert isinstance(delegate, Gtk.Text)
    controllers = delegate.observe_controllers()
    clicks = [
        controllers.get_item(i)
        for i in range(controllers.get_n_items())
        if isinstance(controllers.get_item(i), Gtk.GestureClick)
    ]

    with (
        caplog.at_level("ERROR"),
        mock.patch("tumbleweed.ui.gtk.guard.on_press", side_effect=RuntimeError("boom")),
    ):
        for click in clicks:
            # Ours only: the capture-phase one. (GTK's own click gesture on the text, emitted with
            # no event, makes GTK itself log `gdk_event_triggers_context_menu: event != NULL`.)
            if (
                isinstance(click, Gtk.GestureClick)
                and not click.get_touch_only()
                and click.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
            ):
                click.emit("pressed", 1, 0.0, 0.0)

    assert FAILED in caplog.text
