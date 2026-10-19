"""Controls of `sandbox` handlers are disabled, with the reason in the tool, until S20 (S12, D14).

A sandbox handler has nowhere to go yet, so the renderer builds its control insensitive and puts a
plain line under it that says why. The line is in the tool and never a tooltip (a tooltip is a
surface of its own, #53). And the guard does not rest on the widget being insensitive: whatever
reaches the widget's signals, the handler is not fired while no sandbox is connected.
"""

from typing import TYPE_CHECKING

import pytest

from tests.ui.gtk.support import descendants, of_type, spec_of
from tests.ui.spec.support import Json
from tumbleweed.ui.runtime import UiRuntime
from tumbleweed.ui.spec.names import StateKey

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool

pytestmark = pytest.mark.gtk

STATE: list[Json] = [
    {"kind": "string", "key": "text", "default": "x"},
    {"kind": "string", "key": "out", "default": ""},
    {"kind": "bool", "key": "flag", "default": False},
    {"kind": "int", "key": "n", "default": 1, "minimum": 0, "maximum": 10},
    {"kind": "enum", "key": "choice", "options": ["a", "b"], "default": "a"},
]
HANDLERS: list[Json] = [
    {
        "kind": "sandbox",
        "name": "run",
        "module": "m",
        "function": "f",
        "inputs": ["text", "flag", "n", "choice"],
        "outputs": ["out"],
    },
    {"kind": "agent", "name": "ask"},
]
CONTROLS: dict[str, Json] = {
    "button": {"kind": "button", "label": "Go", "on_click": "run"},
    "entry change": {"kind": "entry", "bind": "text", "on_change": "run"},
    "entry activate": {"kind": "entry", "bind": "text", "on_activate": "run"},
    "switch": {"kind": "switch", "bind": "flag", "on_change": "run"},
    "check": {"kind": "check", "label": "Flag", "bind": "flag", "on_change": "run"},
    "slider": {"kind": "slider", "bind": "n", "on_change": "run"},
    "spin": {"kind": "spin", "bind": "n", "on_change": "run"},
    "dropdown": {"kind": "dropdown", "bind": "choice", "on_change": "run"},
    "text view": {"kind": "text_view", "bind": "text", "editable": True, "on_change": "run"},
}


class RecordingSandbox:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, handler: object, event: object, inputs: object) -> None:
        self.calls += 1


def render(control: Json, *, sandbox: RecordingSandbox | None = None) -> "RenderedTool":
    from tumbleweed.ui.gtk import RenderedTool

    spec = spec_of(
        {"kind": "box", "children": [control, {"kind": "label", "bind": "out"}]}, STATE, HANDLERS
    )
    return RenderedTool(spec, runtime=UiRuntime(spec, sandbox=sandbox))


def reason_lines(tool: "RenderedTool") -> list[str]:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk.widgets import SANDBOX_UNAVAILABLE

    return [
        w.get_text()
        for w in descendants(tool.widget)
        if isinstance(w, Gtk.Label) and w.get_text() == SANDBOX_UNAVAILABLE
    ]


def drive(tool: "RenderedTool", name: str) -> None:
    """Act on the control the way its widget reports the user acting (the signals GTK sends)."""
    from tumbleweed.shell._gi import Gtk

    match name:
        case "button":
            of_type(tool, Gtk.Button)[0].emit("clicked")
        case "entry change":
            of_type(tool, Gtk.Entry)[0].set_text("typed")
        case "entry activate":
            of_type(tool, Gtk.Entry)[0].emit("activate")
        case "switch":
            of_type(tool, Gtk.Switch)[0].set_active(True)
        case "check":
            of_type(tool, Gtk.CheckButton)[0].set_active(True)
        case "slider":
            of_type(tool, Gtk.Scale)[0].set_value(7.0)
        case "spin":
            of_type(tool, Gtk.SpinButton)[0].set_value(7.0)
        case "dropdown":
            from tumbleweed.ui.gtk.choice import ChoiceList

            of_type(tool, ChoiceList)[0].select("b")
        case "text view":
            of_type(tool, Gtk.TextView)[0].get_buffer().set_text("typed")
        case _:
            raise AssertionError(name)


def interactive(tool: "RenderedTool", name: str) -> "Gtk.Widget":
    """The widget the user would work: the one inside the box that carries the reason."""
    from tumbleweed.shell._gi import Gtk

    kinds: dict[str, type[Gtk.Widget]] = {
        "button": Gtk.Button,
        "entry change": Gtk.Entry,
        "entry activate": Gtk.Entry,
        "switch": Gtk.Switch,
        "check": Gtk.CheckButton,
        "slider": Gtk.Scale,
        "spin": Gtk.SpinButton,
        "text view": Gtk.TextView,
    }
    if name == "dropdown":
        from tumbleweed.ui.gtk.choice import ChoiceList

        return of_type(tool, ChoiceList)[0]
    return of_type(tool, kinds[name])[0]


@pytest.mark.parametrize("name", list(CONTROLS))
def test_the_control_is_disabled_and_the_reason_is_in_the_tool_not_a_tooltip(name: str) -> None:
    tool = render(CONTROLS[name])

    assert not interactive(tool, name).is_sensitive()
    assert len(reason_lines(tool)) == 1
    assert all(w.get_tooltip_text() is None for w in descendants(tool.widget))
    assert all(w.get_tooltip_markup() is None for w in descendants(tool.widget))


@pytest.mark.parametrize("name", list(CONTROLS))
def test_whatever_reaches_the_widget_the_sandbox_handler_does_not_fire(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired: list[object] = []
    monkeypatch.setattr(UiRuntime, "_run_sandbox", lambda self, handler, event: fired.append(event))
    tool = render(CONTROLS[name])

    drive(tool, name)

    assert fired == []
    assert tool.runtime.store.value(StateKey("out")) == ""


@pytest.mark.parametrize("name", list(CONTROLS))
def test_with_a_sandbox_connected_the_control_is_enabled_and_fires(name: str) -> None:
    """The rule is about there being nowhere to go: S20 connects a sink and the controls work."""
    sandbox = RecordingSandbox()
    tool = render(CONTROLS[name], sandbox=sandbox)

    assert interactive(tool, name).is_sensitive()
    assert reason_lines(tool) == []
    drive(tool, name)

    assert sandbox.calls >= 1


def test_a_control_of_a_set_or_agent_handler_is_not_disabled() -> None:
    from tumbleweed.shell._gi import Gtk

    spec = spec_of(
        {
            "kind": "box",
            "children": [
                {"kind": "button", "label": "Ask", "on_click": "ask"},
                {"kind": "button", "label": "Nothing"},
            ],
        },
        STATE,
        HANDLERS,
    )
    from tumbleweed.ui.gtk import RenderedTool

    tool = RenderedTool(spec)

    assert all(button.is_sensitive() for button in of_type(tool, Gtk.Button))
    assert reason_lines(tool) == []


def test_the_user_may_still_edit_the_value_of_a_disabled_entry_state_but_nothing_is_sent() -> None:
    """The edit is the state's own (a widget forced to change); only the handler is withheld."""
    tool = render(CONTROLS["entry change"])

    drive(tool, "entry change")

    assert tool.runtime.store.value(StateKey("text")) == "typed"


def test_widget_at_gives_the_box_that_holds_the_control_and_its_reason() -> None:
    from tumbleweed.shell._gi import Gtk

    tool = render(CONTROLS["button"])

    held = tool.widget_at("root.children[0]")

    assert isinstance(held, Gtk.Box)
    assert [type(w) for w in descendants(held) if isinstance(w, Gtk.Button)] == [Gtk.Button]
