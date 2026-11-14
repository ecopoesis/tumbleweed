"""The saved-tools panel and the card of a stored tool in a real window (S24, D30 (8), (9)).

Names and descriptions are the tool's author's text: they are shown as literal plain text under a
caption of ours (I5). The buttons are the user's: each one asks for exactly its row.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from tests.shell.gtk_support import Shell, descendants, labels_in
from tests.shell.test_tools_model import FakePort, grant, summary
from tests.tools.support import csv_ui
from tumbleweed.agent.canvas import StoredBinding
from tumbleweed.agent.code import HandlerCall, HandlerOutcome
from tumbleweed.agent.tool_access import ToolAccessKind
from tumbleweed.core.ids import TaskId, ToolId, ToolInstanceId
from tumbleweed.core.types import ContentHash, ToolVersion
from tumbleweed.shell._gi import Gtk
from tumbleweed.shell.canvas_tools import CanvasTools
from tumbleweed.shell.tools_panel import OWN_WORDS, ToolsPanel

pytestmark = pytest.mark.gtk

WHEN = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def buttons(widget: Gtk.Widget, label: str) -> list[Gtk.Button]:
    return [w for w in descendants(widget) if isinstance(w, Gtk.Button) and w.get_label() == label]


def test_the_panel_is_hidden_until_opened_and_shows_the_tools_text_literally() -> None:
    markup = summary(name="<b>Photo</b> &amp; co", description="<i>adjusts</i> photos")
    panel = ToolsPanel(FakePort(markup))
    assert not panel.get_visible()

    panel.open()

    assert panel.get_visible()
    texts = [label.get_text() for label in labels_in(panel)]
    assert "<b>Photo</b> &amp; co" in texts  # literal, never markup
    assert OWN_WORDS in texts
    assert all(not label.get_use_markup() for label in labels_in(panel))


def test_open_and_history_ask_the_port_for_that_row_only() -> None:
    first, second = summary(name="First"), summary(name="Second")
    port = FakePort(first, second)
    port.history[first.tool] = (summary(first.tool, 2), summary(first.tool, 1))
    panel = ToolsPanel(port)
    panel.open()

    buttons(panel, "Open")[1].emit("clicked")
    assert port.calls == [("open", (second.tool, None))]
    port.pending.pop()()
    buttons(panel, "Versions")[0].emit("clicked")
    assert [b.get_label() for b in descendants(panel) if isinstance(b, Gtk.Button)].count(
        "Open this version"
    ) == 2
    assert len(buttons(panel, "Make this the newest")) == 1  # the newest is not restorable
    buttons(panel, "Make this the newest")[0].emit("clicked")

    assert port.calls[-1] == ("restore", (first.tool, ToolVersion(1)))


def test_the_grants_are_listed_with_a_revoke_button_that_revokes_that_grant() -> None:
    tool = summary()
    mine, other = grant(tool.tool, path="/home/u/Photos"), grant(tool.tool, path="/home/u/Docs")
    port = FakePort(tool, grants=(mine, other))
    panel = ToolsPanel(port)
    panel.open()

    texts = [label.get_text() for label in labels_in(panel)]
    assert "Access given to tools" in texts
    assert "/home/u/Photos" in texts
    buttons(panel, "Revoke")[1].emit("clicked")

    assert port.calls == [("revoke", other.grant)]


def test_the_buttons_do_nothing_while_an_action_is_under_way() -> None:
    tool = summary()
    port = FakePort(tool)
    panel = ToolsPanel(port)
    panel.open()

    buttons(panel, "Open")[0].emit("clicked")

    assert all(
        not b.get_sensitive()
        for b in descendants(panel)
        if isinstance(b, Gtk.Button)
        if b.get_label() in {"Open", "Versions"}
    )


class _Access:
    def __init__(self) -> None:
        self.asked: list[tuple[TaskId, StoredBinding, ToolAccessKind]] = []
        self.done: list[Callable[[str], None]] = []

    def request(
        self,
        task: TaskId,
        stored: StoredBinding,
        kind: ToolAccessKind,
        done: Callable[[str], None],
    ) -> None:
        self.asked.append((task, stored, kind))
        self.done.append(done)


class _Executor:
    def __init__(self) -> None:
        self.calls: list[HandlerCall] = []

    def start(
        self, task: TaskId, call: HandlerCall, done: Callable[[HandlerOutcome], None]
    ) -> Callable[[], None]:
        self.calls.append(call)
        return lambda: None


class _Timers:
    def call_later(self, seconds: float, callback: Callable[[], None]) -> Callable[[], None]:
        return lambda: None


def stored(*capabilities: str) -> StoredBinding:
    return StoredBinding(
        instance=ToolInstanceId.new(),
        tool=ToolId.new(),
        version=ToolVersion(1),
        content_hash=ContentHash("a" * 64),
        capabilities=capabilities,
    )


def card_tools(echo_shell: Shell, access: _Access) -> tuple[CanvasTools, _Executor]:
    executor = _Executor()
    return (
        CanvasTools(
            echo_shell.window.canvas,
            echo_shell.window.event_gate,
            executor=executor,
            scheduler=_Timers(),
            tool_access=access,
        ),
        executor,
    )


def test_the_card_of_a_stored_tool_offers_what_its_manifest_declares_and_asks_as_the_user(
    echo_shell: Shell,
) -> None:
    access = _Access()
    tools, _ = card_tools(echo_shell, access)
    task, binding = TaskId.new(), stored("fs.read", "net.full", "fs.write")

    shown = tools.show(csv_ui(), "replace", task, (), binding)
    card = tools.tool(shown.instance)
    assert card is not None
    column = card.widget.get_parent()
    assert column is not None
    labels = [
        w.get_label()
        for w in descendants(column.get_parent() or column)
        if isinstance(w, Gtk.Button)
    ]

    assert shown.instance == binding.instance  # the broker's instance is the one shown
    assert "Choose a file for this tool..." in labels
    assert "Choose a folder for this tool..." in labels
    assert "Allow the network for this tool..." in labels
    assert not any("write" in (label or "").lower() for label in labels)  # a write is the task's
    button = buttons(column.get_parent() or column, "Choose a folder for this tool...")[0]
    button.emit("clicked")
    assert access.asked == [(task, binding, "folder")]
    access.done[0]("This version of the tool can use that access whenever you use it.")
    assert any("whenever you use it" in label.get_text() for label in labels_in(column))


def test_a_tool_the_model_showed_has_no_access_buttons_and_no_binding(echo_shell: Shell) -> None:
    access = _Access()
    tools, _ = card_tools(echo_shell, access)

    shown = tools.show(csv_ui(), "replace", TaskId.new(), ())

    assert tools.source(shown.instance).stored is None
    card = tools.tool(shown.instance)
    assert card is not None
    assert not [
        w
        for w in descendants(echo_shell.window.canvas)
        if isinstance(w, Gtk.Button) and "for this tool" in (w.get_label() or "")
    ]


def test_the_source_of_an_instance_is_what_was_shown_and_it_is_gone_when_closed(
    echo_shell: Shell,
) -> None:
    from tumbleweed.agent.canvas import UnknownInstanceError

    tools, _ = card_tools(echo_shell, _Access())
    binding = stored("fs.read")
    shown = tools.show(csv_ui(), "replace", TaskId.new(), (), binding)

    source = tools.source(shown.instance)
    assert source.spec == csv_ui()
    assert source.stored == binding
    tools.close()
    with pytest.raises(UnknownInstanceError):
        tools.source(shown.instance)
