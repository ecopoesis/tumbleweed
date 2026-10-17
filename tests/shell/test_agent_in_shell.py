"""The agent inside the real shell: the GLib dispatcher, Esc, and a streamed reply through GTK.

The responder is the real `AgentResponder` on its worker thread with a scripted model, and the
dispatcher is the shell's own (`GLib.idle_add`), so a reply crosses from the worker thread to the
main loop exactly as it does in the running shell.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

import pytest

from tests.agent.support import MemoryLog
from tests.shell.fakes import ManualResponder
from tests.shell.gtk_support import Shell, click_send, pump_until, rows, type_text
from tumbleweed.agent.fake import FakeProvider, FakeTurn
from tumbleweed.agent.limits import AgentLimits
from tumbleweed.agent.recorder import InvocationRecorder
from tumbleweed.agent.responder import Responder
from tumbleweed.agent.runtime import AgentResponder
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.core.types import ModelName
from tumbleweed.shell.viewmodel.transcript import MessageState

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk

pytestmark = pytest.mark.gtk

type ShellBuilder = Callable[[Responder], Shell]
type AgentShellBuilder = Callable[[list[FakeTurn]], tuple[Shell, FakeProvider]]


def escape_shortcuts(widget: "Gtk.Widget") -> list["Gtk.Shortcut"]:
    """The shortcuts on `widget` that Esc triggers."""
    from tumbleweed.shell._gi import Gdk, Gtk

    found = []
    for controller in widget.observe_controllers():
        if not isinstance(controller, Gtk.ShortcutController):
            continue
        for shortcut in controller:
            trigger = shortcut.get_trigger()
            if isinstance(trigger, Gtk.KeyvalTrigger) and trigger.get_keyval() == Gdk.KEY_Escape:
                found.append(shortcut)
    return found


def press_escape(shell: Shell) -> bool:
    """Esc, as the window's shortcut handles it; True if the key was consumed."""
    from tumbleweed.shell._gi import Gtk

    (shortcut,) = escape_shortcuts(shell.window)
    action = shortcut.get_action()
    assert action is not None
    return bool(action.activate(Gtk.ShortcutActionFlags(0), shell.window, None))


class TestGlibDispatcher:
    def test_a_callback_posted_from_another_thread_runs_on_the_main_thread(self) -> None:
        from tumbleweed.shell.dispatch import GlibDispatcher

        ran: list[int] = []
        thread = threading.Thread(
            target=lambda: GlibDispatcher().post(lambda: ran.append(threading.get_ident()))
        )

        thread.start()
        thread.join(5)

        assert pump_until(lambda: bool(ran))
        assert ran == [threading.get_ident()]

    def test_callbacks_run_in_the_order_posted(self) -> None:
        from tumbleweed.shell.dispatch import GlibDispatcher

        order: list[int] = []
        dispatcher = GlibDispatcher()
        for number in range(20):
            dispatcher.post(lambda n=number: order.append(n))  # type: ignore[misc]  # bound per loop

        assert pump_until(lambda: len(order) == 20)
        assert order == list(range(20))

    def test_a_callback_that_raises_is_logged_and_does_not_stop_the_next(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from tumbleweed.shell.dispatch import GlibDispatcher

        ran: list[str] = []
        dispatcher = GlibDispatcher()

        def broken() -> None:
            raise RuntimeError("callback bug")

        with caplog.at_level(logging.ERROR):
            dispatcher.post(broken)
            dispatcher.post(lambda: ran.append("next"))
            assert pump_until(lambda: bool(ran))

        assert "failed" in caplog.text
        assert ran == ["next"]


class TestEscape:
    def test_the_handler_is_on_the_window_not_on_the_chat_panel(
        self, build_shell: ShellBuilder
    ) -> None:
        shell = build_shell(ManualResponder())

        assert len(escape_shortcuts(shell.window)) == 1  # so it works wherever the focus is
        assert escape_shortcuts(shell.window.chat_panel) == []
        assert escape_shortcuts(shell.window.canvas) == []

    def test_the_shortcut_is_the_bare_escape_key_in_the_bubble_phase(
        self, build_shell: ShellBuilder
    ) -> None:
        """GTK4 has no public way to make a key event, so the key path is checked piece by piece:
        a key press goes from the focus widget up through its ancestors (the canvas, the chat
        panel and the input are all inside the window), and the window's controller sees it last
        (a popover or an entry that wants Esc takes it first), for Esc with no modifier."""
        from tumbleweed.shell._gi import Gdk, Gtk

        shell = build_shell(ManualResponder())
        (controller,) = (
            c
            for c in shell.window.observe_controllers()
            if isinstance(c, Gtk.ShortcutController)
            and any(
                isinstance(sc.get_trigger(), Gtk.KeyvalTrigger)
                and sc.get_trigger().get_keyval() == Gdk.KEY_Escape
                for sc in c
            )
        )
        (shortcut,) = escape_shortcuts(shell.window)
        trigger = shortcut.get_trigger()

        assert isinstance(trigger, Gtk.KeyvalTrigger)
        assert trigger.get_keyval() == Gdk.KEY_Escape
        assert int(trigger.get_modifiers()) == 0
        assert controller.get_propagation_phase() == Gtk.PropagationPhase.BUBBLE
        for widget in (
            shell.window.canvas,
            shell.window.chat_panel,
            shell.window.chat_panel.input_view,
        ):
            assert widget.get_root() is shell.window

    def test_esc_works_with_the_focus_on_the_canvas_too(self, build_shell: ShellBuilder) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "question")
        click_send(shell)
        shell.window.set_focus(shell.window.canvas)  # not the chat panel

        assert press_escape(shell)

        assert responder.handle.cancelled

    def test_esc_cancels_the_reply_in_flight(self, build_shell: ShellBuilder) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "question")
        click_send(shell)

        handled = press_escape(shell)

        assert handled
        assert responder.handle.cancelled
        message = shell.window.transcript.messages[1]
        assert (message.state, message.error) == (MessageState.FAILED, "Cancelled.")

    def test_esc_with_no_reply_does_nothing_and_is_left_to_others(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)

        assert not press_escape(shell)

        assert shell.window.transcript.messages == ()
        assert responder.handles == []

    def test_esc_after_the_reply_ended_is_left_to_others(self, build_shell: ShellBuilder) -> None:
        from tumbleweed.agent.responder import ReplyFinished

        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "q")
        click_send(shell)
        responder.sink.emit(ReplyFinished())

        assert not press_escape(shell)
        assert not responder.handle.cancelled


@pytest.fixture
def agent_shell(build_shell: ShellBuilder) -> Iterator[AgentShellBuilder]:
    from tumbleweed.shell.dispatch import GlibDispatcher

    responders: list[AgentResponder] = []

    def build(script: list[FakeTurn]) -> tuple[Shell, FakeProvider]:
        provider = FakeProvider(script)
        responder = AgentResponder(
            provider=provider,
            recorder=InvocationRecorder(MemoryLog(), prompt_version="test"),
            registry=ToolRegistry(),
            model=ModelName("claude-test-1"),
            system_prompt="test",
            limits=AgentLimits(),
            dispatcher=GlibDispatcher(),
        )
        responders.append(responder)
        return build_shell(responder), provider

    yield build
    for responder in responders:
        responder.close()


class TestModelTextThatWouldWedgeTheChat:
    def test_a_lone_surrogate_and_a_nul_show_as_replacement_characters_and_the_chat_stays_usable(
        self, agent_shell: AgentShellBuilder
    ) -> None:
        shell, _ = agent_shell(
            [FakeTurn(chunks=("bad \ud800 and \x00 text",)), FakeTurn(chunks=("fine",))]
        )
        type_text(shell, "one")
        click_send(shell)

        assert pump_until(lambda: not shell.window.controller.busy)
        assert rows(shell)[1] == ("Assistant", "bad \ufffd and \ufffd text", "")

        type_text(shell, "two")
        click_send(shell)
        assert pump_until(lambda: not shell.window.controller.busy)
        assert rows(shell)[3] == ("Assistant", "fine", "")


class TestAgentReplyThroughGtk:
    def test_a_reply_streams_into_the_window_from_the_worker_thread(
        self, agent_shell: AgentShellBuilder
    ) -> None:
        shell, _ = agent_shell([FakeTurn(chunks=("Hello", ", ", "world"))])
        type_text(shell, "hi")

        click_send(shell)

        assert pump_until(lambda: not shell.window.controller.busy)
        assert rows(shell) == [("You", "hi", ""), ("Assistant", "Hello, world", "")]

    def test_a_failure_shows_in_the_window_and_the_shell_takes_the_next_message(
        self, agent_shell: AgentShellBuilder
    ) -> None:
        shell, _ = agent_shell([FakeTurn(), FakeTurn(chunks=("fine",))])  # an empty reply fails
        type_text(shell, "one")
        click_send(shell)
        assert pump_until(lambda: not shell.window.controller.busy)
        assert rows(shell)[1][2] == "The model sent an empty reply."

        type_text(shell, "two")
        click_send(shell)

        assert pump_until(lambda: not shell.window.controller.busy)
        assert rows(shell)[3] == ("Assistant", "fine", "")

    def test_esc_stops_a_streaming_reply_and_the_provider_is_released(
        self, agent_shell: AgentShellBuilder
    ) -> None:
        shell, provider = agent_shell([FakeTurn(chunks=("partial",), hang=True)])
        type_text(shell, "hi")
        click_send(shell)
        assert pump_until(lambda: shell.window.transcript.messages[1].text == "partial")

        assert press_escape(shell)

        message = shell.window.transcript.messages[1]
        assert (message.text, message.state, message.error) == (
            "partial",
            MessageState.FAILED,
            "Cancelled.",
        )
        assert pump_until(lambda: provider.calls_cancelled == 1)
        assert not shell.window.controller.busy
