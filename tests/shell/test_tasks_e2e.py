"""The agent, the window and a real broker together (S19): a scripted model asks for access, the
broker asks the window's trusted prompt over the shell's one connection, the test plays the user by
clicking the real button, and the model reads the file it was given.

Nothing here answers a prompt except a click on the real banner (I5, I10); the model is scripted.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue

from tests.agent.support import MemoryLog
from tests.shell.conftest import PromptShell
from tests.shell.gtk_support import Shell, click_send, pump_until, type_text, user_click
from tests.shell.live_broker import LiveBroker
from tests.shell.prompt_support import Clock, FakePicker, Timers
from tests.shell.test_prompt_flows import clickable, shown
from tumbleweed.agent.access import register_access_tools
from tumbleweed.agent.broker_link import BrokerLink
from tumbleweed.agent.fake import FakeProvider, FakeToolCall, FakeTurn
from tumbleweed.agent.limits import AgentLimits
from tumbleweed.agent.llm import ToolResultBlock
from tumbleweed.agent.recorder import InvocationRecorder
from tumbleweed.agent.runtime import AgentResponder
from tumbleweed.agent.tasks import TaskSession
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.broker.client import BrokerConnector
from tumbleweed.core.types import ModelName
from tumbleweed.ipc.connection import RequestHandler
from tumbleweed.shell.status_strip import TASK_PREFIX

if TYPE_CHECKING:
    from tumbleweed.ipc.sockdir import SocketPath

pytestmark = pytest.mark.gtk


def call(name: str, **arguments: JsonValue) -> FakeToolCall:
    return FakeToolCall(name=name, input=dict(arguments))


@dataclass
class Setup:
    shell: PromptShell
    provider: FakeProvider
    broker: LiveBroker
    tasks: TaskSession


@contextmanager
def agent_shell(script: Callable[[str], list[FakeTurn]]) -> Iterator[Setup]:
    from tumbleweed.shell._gi import Gio
    from tumbleweed.shell.app import ShellApplication
    from tumbleweed.shell.dispatch import GlibDispatcher
    from tumbleweed.shell.window import PromptWiring, ShellWindow

    clock = Clock()
    timers = Timers(clock)
    picker = FakePicker()
    dispatcher = GlibDispatcher()
    socket: list[SocketPath] = []  # known once the broker is up

    def connector(handler: RequestHandler) -> BrokerConnector:
        return BrokerConnector(socket[0], handler=handler)

    link = BrokerLink(connector)
    tasks = TaskSession(link, dispatcher)
    registry = ToolRegistry()
    register_access_tools(registry, tasks, link)
    provider = FakeProvider([])
    responder = AgentResponder(
        provider=provider,
        recorder=InvocationRecorder(MemoryLog(), prompt_version="test"),
        registry=registry,
        model=ModelName("claude-test-1"),
        system_prompt="test",
        limits=AgentLimits(),
        dispatcher=dispatcher,
        tasks=tasks,
    )
    app = ShellApplication(responder, flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    window = ShellWindow(
        app,
        responder,
        prompt_wiring=PromptWiring(clock=clock, scheduler=timers, picker=picker),
        broker_link=link,
        tasks=tasks,
    )
    shell = PromptShell(Shell(app, window), clock, timers, picker)
    window.present()
    pump_until(window.get_mapped)
    broker = LiveBroker(window.broker_prompts)
    try:
        socket.append(broker.rig.socket)
        provider.extend(script(str(broker.rig.home / "Photos")))
        yield Setup(shell, provider, broker, tasks)
    finally:
        broker.stop()
        window.shutdown()
        window.destroy()
        link.close()
        responder.close()


def test_a_model_asks_the_user_through_the_trusted_prompt_and_reads_what_was_allowed() -> None:
    def script(photos: str) -> list[FakeTurn]:
        return [
            FakeTurn(tool_calls=(call("request_access", kind="read", path=photos, reason="look"),)),
            FakeTurn(tool_calls=(call("read_file", path=f"{photos}/a.jpg"),)),
            FakeTurn(tool_calls=(call("start_task", title="Second job"),)),
            FakeTurn(chunks=("done",)),
        ]

    with agent_shell(script) as s:
        window = s.shell.window
        type_text(s.shell.shell, "show me my photo")
        click_send(s.shell.shell)

        assert pump_until(lambda: shown(s.shell))  # the broker asked the window, not a fake
        assert not window.canvas.is_sensitive()
        s.shell.wait_armed()
        user_click(window.prompt_banner, clickable(s.shell, "Allow for this task"))
        assert pump_until(lambda: not window.controller.busy)

        last = window.transcript.messages[-1]
        assert last.text == "done"
        read = [
            b for b in s.provider.requests[2].messages[-1].content if isinstance(b, ToolResultBlock)
        ]
        assert "jpeg" in read[0].content
        assert last.task is not None  # the reply is tagged with the task it ended in
        assert window.status_strip.task_text == f"{TASK_PREFIX}Second job"


def settled_with_a_task(s: "Setup") -> None:
    window = s.shell.window
    type_text(s.shell.shell, "list it")
    click_send(s.shell.shell)
    assert pump_until(lambda: not window.controller.busy)
    assert s.tasks.current is not None


def inert_notes(s: "Setup") -> int:
    from tests.shell.gtk_support import descendants
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.shell.canvas_tools import INERT_NOTICE

    return [
        w.get_text() for w in descendants(s.shell.window.canvas) if isinstance(w, Gtk.Label)
    ].count(INERT_NOTICE)


def test_a_task_that_starts_makes_an_instance_that_belongs_to_no_task_inert() -> None:
    from tests.shell.test_canvas_agent import asking_spec
    from tumbleweed.ui.spec import validate_ui_spec

    with agent_shell(
        lambda photos: [FakeTurn(tool_calls=(call("list_workspace"),)), FakeTurn(chunks=("ok",))]
    ) as s:
        s.shell.window.canvas_tools.show(validate_ui_spec(asking_spec()), "replace", None)
        assert inert_notes(s) == 0

        settled_with_a_task(s)  # the default task started: the window was told on the main thread

        assert inert_notes(s) == 1


def test_a_new_chat_session_makes_the_instances_of_the_task_and_of_no_task_inert() -> None:
    from tests.shell.test_canvas_agent import asking_spec
    from tumbleweed.ui.spec import validate_ui_spec

    with agent_shell(
        lambda photos: [FakeTurn(tool_calls=(call("list_workspace"),)), FakeTurn(chunks=("ok",))]
    ) as s:
        settled_with_a_task(s)
        tools = s.shell.window.canvas_tools
        task = s.tasks.current
        tools.show(validate_ui_spec(asking_spec()), "stack", task)
        tools.show(validate_ui_spec(asking_spec()), "stack", None)
        assert inert_notes(s) == 0

        s.shell.window.session.new_session()  # ends the task: the window hears it
        assert pump_until(lambda: inert_notes(s) == 2)
