"""Sandboxed code, end to end (S20 acceptance): the scripted model gets a photo through the trusted
picker and shows the photo-adjust golden tool with its code; the user clicks Adjust; the code runs
in the real sandbox through a real broker, writes its image into the task's workspace, and the image
renders in the window.

The model is the scripted fake provider. The user is the test, clicking the real buttons of the real
prompt and choosing in a fake picker (the `FilePicker` Protocol); nothing in `src/` grants by itself
(I5, I10).
"""

import dataclasses
import json
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue

from tests.agent.photo_tool import PHOTO_MODULE, write_photo
from tests.agent.support import MemoryLog
from tests.sandbox.support import PYTHON, landlock_available
from tests.shell.conftest import PromptShell
from tests.shell.gtk_support import (
    Shell,
    click_send,
    descendants,
    pump,
    pump_until,
    type_text,
    user_click,
)
from tests.shell.live_broker import LiveBroker
from tests.shell.prompt_support import Clock, FakePicker, Timers
from tests.shell.test_prompt_flows import clickable, shown
from tests.ui.spec.support import golden_text
from tumbleweed.agent.access import register_access_tools
from tumbleweed.agent.broker_link import BrokerLink
from tumbleweed.agent.canvas import CanvasLink, register_canvas_tools
from tumbleweed.agent.code import BrokerHandlerExecutor, register_code_tools
from tumbleweed.agent.fake import FakeProvider, FakeToolCall, FakeTurn
from tumbleweed.agent.handler_runs import REFUSED_NOTICE
from tumbleweed.agent.limits import AgentLimits
from tumbleweed.agent.recorder import InvocationRecorder
from tumbleweed.agent.runtime import AgentResponder
from tumbleweed.agent.tasks import TaskSession
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.broker.client import BrokerConnector
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.core.types import ModelName
from tumbleweed.ipc.connection import RequestHandler
from tumbleweed.sandbox.runner import SandboxRunner
from tumbleweed.ui.spec.handlers import SandboxHandler
from tumbleweed.ui.spec.names import StateKey

if TYPE_CHECKING:
    from tumbleweed.ipc.sockdir import SocketPath
    from tumbleweed.ui.gtk import RenderedTool

pytestmark = [pytest.mark.gtk, pytest.mark.linux]


def pillow_in_the_runtime() -> bool:
    done = subprocess.run(  # noqa: S603  (our own argv)
        [PYTHON, "-I", "-c", "import PIL, numpy"], capture_output=True, check=False, timeout=30
    )
    return done.returncode == 0


@pytest.fixture(autouse=True)
def runtime_ready() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) is not installed")
    if not pillow_in_the_runtime():
        pytest.skip(
            "the sandbox runtime's Python has no Pillow and numpy (the Linux image has them)"
        )


def call(name: str, **arguments: JsonValue) -> FakeToolCall:
    return FakeToolCall(name=name, input=dict(arguments))


def development(config: BrokerConfig) -> BrokerConfig:
    return dataclasses.replace(config, require_landlock=landlock_available())


@dataclass
class Setup:
    shell: PromptShell
    provider: FakeProvider
    broker: LiveBroker
    tasks: TaskSession
    photo: Path


@contextmanager
def code_shell(script: Callable[[str], list[FakeTurn]]) -> Iterator[Setup]:
    """A window with the access, canvas and code tools, a real broker and the real sandbox.
    `script` gets the photo's path (a file in the broker's scratch home)."""
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
    canvas = CanvasLink(dispatcher)
    registry = ToolRegistry()
    register_canvas_tools(registry, canvas, tasks)
    register_access_tools(registry, tasks, link)
    register_code_tools(registry, tasks, link)
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
    app = ShellApplication(responder, flags=Gio.ApplicationFlags.NON_UNIQUE, canvas_link=canvas)
    app.register(None)
    window = ShellWindow(
        app,
        responder,
        canvas_link=canvas,
        prompt_wiring=PromptWiring(clock=clock, scheduler=timers, picker=picker),
        broker_link=link,
        tasks=tasks,
    )
    shell = PromptShell(Shell(app, window), clock, timers, picker)
    window.present()
    pump_until(window.get_mapped)
    broker = LiveBroker(
        window.broker_prompts, config=development, runner=SandboxRunner(use_systemd_scope=False)
    )
    try:
        socket.append(broker.rig.socket)
        photo = write_photo(broker.rig.home / "Photos")
        provider.extend(script(str(photo)))
        yield Setup(shell, provider, broker, tasks, photo)
    finally:
        broker.stop()
        window.shutdown()
        window.destroy()
        link.close()
        responder.close()


def counted_runs(monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], list[int]]:
    """Every run the window's executor starts, and every cancel of one, counted (and still done)."""
    starts: list[int] = []
    cancels: list[int] = []
    original = BrokerHandlerExecutor.start

    def counting(self: BrokerHandlerExecutor, *args: object) -> Callable[[], None]:
        starts.append(1)
        cancel: Callable[[], None] = original(self, *args)  # type: ignore[arg-type]  # forwards

        def counted() -> None:
            cancels.append(1)
            cancel()

        return counted

    monkeypatch.setattr(BrokerHandlerExecutor, "start", counting)
    return starts, cancels


def photo_spec(photo: str) -> dict[str, JsonValue]:
    spec = json.loads(golden_text("photo_adjuster"))
    for entry in spec["state"]:
        if entry["key"] == "photo_path":
            entry["default"] = photo  # the path the user chose in the picker
    return spec  # type: ignore[no-any-return]  # json.loads of our own fixture


def ask_for_the_photo(photo: str) -> FakeTurn:
    return FakeTurn(tool_calls=(call("request_access", kind="pick_file", reason="your photo"),))


def show_the_tool(photo: str, module: str = PHOTO_MODULE) -> FakeTurn:
    return FakeTurn(
        tool_calls=(
            call(
                "show_ui",
                spec=photo_spec(photo),
                code=[{"name": "photo", "source": module}],
            ),
        )
    )


def choose_in_the_picker(s: Setup) -> None:
    window = s.shell.window
    type_text(s.shell.shell, "brighten my photo")
    click_send(s.shell.shell)
    assert pump_until(lambda: shown(s.shell))
    s.shell.wait_armed()
    user_click(window.prompt_banner, clickable(s.shell, "Choose, allow for this task…"))
    assert s.shell.picker.asked == ["file"]
    s.shell.picker.finish(str(s.photo))  # the user chooses the photo
    assert pump_until(lambda: not window.controller.busy, timeout=30)


def the_tool(s: Setup) -> "RenderedTool":
    (instance,) = s.shell.window.canvas_tools.instances
    tool = s.shell.window.canvas_tools.tool(instance)
    assert tool is not None
    return tool


def card_lines(s: Setup) -> list[str]:
    from tumbleweed.shell._gi import Gtk

    return [w.get_text() for w in descendants(s.shell.window.canvas) if isinstance(w, Gtk.Label)]


def press_adjust(s: Setup) -> None:
    from tumbleweed.shell._gi import Gtk

    buttons = [w for w in descendants(the_tool(s).widget) if isinstance(w, Gtk.Button)]
    adjust = buttons[0]  # Adjust, then Reset (the golden spec's order)
    assert adjust.get_sensitive()  # a broker is connected: the control is on
    adjust.emit("clicked")


def test_the_user_picks_a_photo_the_tool_adjusts_it_in_the_sandbox_and_the_image_renders() -> None:
    from tumbleweed.ui.gtk.imageview import ImageView

    with code_shell(
        lambda photo: [ask_for_the_photo(photo), show_the_tool(photo), FakeTurn(chunks=("Here.",))]
    ) as s:
        choose_in_the_picker(s)
        tool = the_tool(s)
        (image,) = [w for w in descendants(tool.widget) if isinstance(w, ImageView)]
        assert image.texture is None

        press_adjust(s)

        assert pump_until(lambda: image.texture is not None, timeout=60), (
            card_lines(s)[-4:],
            str(tool.runtime.store.value(StateKey("status"))),
            str(tool.runtime.store.value(StateKey("result"))),
        )
        status = tool.runtime.store.value(StateKey("status"))
        assert isinstance(status, str)
        assert status.startswith("Done. Average level")
        result = tool.runtime.store.value(StateKey("result"))
        assert "adjusted-" in str(result)
        assert not any("Running" in line for line in card_lines(s))  # the line is cleared


def test_a_denied_grant_leaves_the_photo_unreadable_and_the_tool_says_so() -> None:
    with code_shell(
        lambda photo: [ask_for_the_photo(photo), show_the_tool(photo), FakeTurn(chunks=("ok",))]
    ) as s:
        window = s.shell.window
        type_text(s.shell.shell, "brighten my photo")
        click_send(s.shell.shell)
        assert pump_until(lambda: shown(s.shell))
        s.shell.wait_armed()
        clickable(s.shell, "Deny").emit("clicked")
        assert pump_until(lambda: not window.controller.busy, timeout=30)
        tool = the_tool(s)

        press_adjust(s)

        assert pump_until(
            lambda: tool.runtime.store.value(StateKey("status")) == "Could not open that photo.",
            timeout=60,
        )
        assert tool.runtime.store.value(StateKey("result")) is None


def test_a_result_that_writes_an_undeclared_output_is_refused_and_nothing_changes() -> None:
    sneaky = "def adjust(inputs):\n    return {'brightness': 99, 'status': 'hacked'}\n"
    with code_shell(
        lambda photo: [
            ask_for_the_photo(photo),
            show_the_tool(photo, sneaky),
            FakeTurn(chunks=("ok",)),
        ]
    ) as s:
        choose_in_the_picker(s)
        tool = the_tool(s)
        before = tool.runtime.store.snapshot()

        press_adjust(s)

        assert pump_until(lambda: REFUSED_NOTICE in card_lines(s), timeout=60)
        assert tool.runtime.store.snapshot() == before


def test_a_burst_of_slider_events_gives_a_bounded_number_of_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tumbleweed.shell._gi import Gtk

    starts, _ = counted_runs(monkeypatch)
    with code_shell(
        lambda photo: [ask_for_the_photo(photo), show_the_tool(photo), FakeTurn(chunks=("ok",))]
    ) as s:
        choose_in_the_picker(s)
        tool = the_tool(s)
        scale = next(w for w in descendants(tool.widget) if isinstance(w, Gtk.Scale))

        for step in range(60):  # a slider dragged: a value every few milliseconds
            scale.set_value(step - 30)
            pump(0.005)
        assert pump_until(
            lambda: str(tool.runtime.store.value(StateKey("status"))).startswith("Done"),
            timeout=60,
        )
        pump(1.0)  # whatever else was going to run has run

        assert tool.runtime.store.value(StateKey("brightness")) == 29
        handlers = sum(isinstance(h, SandboxHandler) for h in tool.runtime.spec.handlers)
        assert 1 <= len(starts) <= 1 + handlers  # 60 events: at most one in flight and one waiting


def test_an_instance_of_an_ended_task_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    starts, _ = counted_runs(monkeypatch)
    with code_shell(
        lambda photo: [ask_for_the_photo(photo), show_the_tool(photo), FakeTurn(chunks=("ok",))]
    ) as s:
        choose_in_the_picker(s)
        s.shell.window.session.new_session()  # the task ends: the instance goes inert
        assert pump_until(lambda: any("has ended" in line for line in card_lines(s)))

        from tumbleweed.shell._gi import Gtk

        adjust = next(
            w for w in descendants(the_tool(s).widget) if isinstance(w, Gtk.Button)
        )  # Adjust is the first button
        adjust.emit("clicked")
        pump(1.0)

        assert starts == []


def test_closing_a_tool_cancels_the_run_it_has_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    _, cancels = counted_runs(monkeypatch)
    slow = "import time\ndef adjust(inputs):\n    time.sleep(5)\n    return {'status': 'late'}\n"
    with code_shell(
        lambda photo: [
            ask_for_the_photo(photo),
            show_the_tool(photo, slow),
            FakeTurn(chunks=("ok",)),
        ]
    ) as s:
        choose_in_the_picker(s)
        tool = the_tool(s)
        press_adjust(s)
        assert pump_until(lambda: any("Running" in line for line in card_lines(s)), timeout=10)

        s.shell.window.canvas_tools.close()  # a replaced or closed tool

        assert cancels == [1]
        pump(0.5)
        assert tool.runtime.store.value(StateKey("status")) != "late"
