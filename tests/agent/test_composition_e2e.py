"""Tool composition end to end with the fake provider (S25 acceptance).

"Feed the photo tool's output into the collage tool": a real broker daemon on a thread (a fake
sandbox that records what it is given, a scripted user), the real link, task session, registry,
responder and chat controller, a fake provider that plays the model, and a canvas that runs the
real UI runtime, the real handler runs and the real connection router. What the broker did is read
from its audit log and from the policy its sandbox was given, not from what the shell said.
"""

import json
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import final

from pydantic import JsonValue

from tests.agent.broker_thread import BrokerThread
from tests.agent.support import ManualDispatcher, Rig, make_rig
from tests.broker.connect_support import stored_tool
from tests.broker.rig import ScriptedPrompts, canned_result
from tumbleweed.agent.access import register_access_tools
from tumbleweed.agent.broker_link import BrokerLink
from tumbleweed.agent.canvas import (
    CanvasLink,
    InstanceSource,
    Placement,
    ShownTool,
    StoredBinding,
    UnknownInstanceError,
    register_canvas_tools,
)
from tumbleweed.agent.code import BrokerHandlerExecutor
from tumbleweed.agent.composition import register_composition_tools
from tumbleweed.agent.fake import FakeToolCall, FakeTurn
from tumbleweed.agent.handler_runs import HandlerRuns
from tumbleweed.agent.llm import ToolResultBlock
from tumbleweed.agent.routing import ConnectionRouter
from tumbleweed.agent.saved_tools import register_saved_tool_tools
from tumbleweed.agent.tasks import TaskSession
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.broker.audit import ConnectionMade, SandboxRun
from tumbleweed.broker.client import BrokerClient
from tumbleweed.core.ids import TaskId, ToolId, ToolInstanceId
from tumbleweed.ipc.connection_messages import ConnectionInfo
from tumbleweed.tools.package import CodeModule
from tumbleweed.ui.runtime import UiRuntime
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.values import ImageRef

RAN = canned_result(stdout=b'{"ok": true, "outputs": {"status": "collaged"}}')


def call(tool: str, /, **arguments: JsonValue) -> FakeToolCall:
    return FakeToolCall(name=tool, input=dict(arguments))


@final
class _Timers:
    """The scheduler of the canvas: what is due waits until the test says it is time."""

    def __init__(self) -> None:
        self.due: list[Callable[[], None]] = []

    def call_later(self, seconds: float, callback: Callable[[], None]) -> Callable[[], None]:
        self.due.append(callback)
        return lambda: None

    def fire(self) -> None:
        due, self.due = self.due, []
        for callback in due:
            callback()


@final
class _Target:
    def __init__(self) -> None:
        self.runtime: UiRuntime | None = None
        self.notices: list[str | None] = []

    def apply(self, handler: object, values: dict[str, JsonValue]) -> None:  # type: ignore[override]
        assert self.runtime is not None
        self.runtime.apply_sandbox_result(handler, values)  # type: ignore[arg-type]  # a handler

    def notice(self, text: str | None) -> None:
        self.notices.append(text)


@final
class _Endpoint:
    def __init__(self, runtime: UiRuntime) -> None:
        self._runtime = runtime

    def value(self, key: StateKey) -> object:
        return self._runtime.store.value(key)

    def subscribe(self, listener: Callable[..., None]) -> Callable[[], None]:
        return self._runtime.store.subscribe(listener)

    def receive(self, key: StateKey, value: JsonValue, connection: object) -> None:
        self._runtime.connection_input(key, value, connection)  # type: ignore[arg-type]  # typed ids


@final
class RoutedCanvas:
    """A canvas host with the real runtime, handler runs and router, and nothing of GTK."""

    def __init__(self, link: BrokerLink, dispatcher: ManualDispatcher, timers: _Timers) -> None:
        self._executor = BrokerHandlerExecutor(link, dispatcher, wall_seconds=5)
        self._timers = timers
        self.router = ConnectionRouter()
        self.instances: list[ToolInstanceId] = []
        self.runtimes: dict[ToolInstanceId, UiRuntime] = {}
        self.sources: dict[ToolInstanceId, InstanceSource] = {}
        self.targets: dict[ToolInstanceId, _Target] = {}
        self.threads: set[int] = set()

    def show(
        self,
        spec: UiSpec,
        placement: Placement,
        task: TaskId | None,
        code: tuple[CodeModule, ...] = (),
        stored: StoredBinding | None = None,
    ) -> ShownTool:
        self.threads.add(threading.get_ident())
        instance = ToolInstanceId.new() if stored is None else stored.instance
        target = _Target()
        runs = HandlerRuns(
            task=task,
            code=code,
            executor=self._executor,
            scheduler=self._timers,
            target=target,  # type: ignore[arg-type]  # the test's own target
            stored=stored,
        )
        runtime = UiRuntime(spec, sandbox=runs)
        target.runtime = runtime
        if placement == "replace":
            self.runtimes.clear()
        self.runtimes[instance] = runtime
        self.targets[instance] = target
        self.sources[instance] = InstanceSource(spec, code, stored, task)
        self.instances.append(instance)
        self.router.attach(instance, _Endpoint(runtime))  # type: ignore[arg-type]  # the fake above
        return ShownTool(instance=instance, on_canvas=len(self.runtimes))

    def source(self, instance: ToolInstanceId) -> InstanceSource:
        found = self.sources.get(instance)
        if found is None or instance not in self.runtimes:
            raise UnknownInstanceError("gone")
        return found

    def patch(self, instance: ToolInstanceId, patch: dict[str, object]) -> tuple[StateKey, ...]:  # type: ignore[override]
        runtime = self.runtimes.get(instance)
        if runtime is None:
            raise UnknownInstanceError("gone")
        before = runtime.store.snapshot()
        runtime.patch_state(patch)
        after = runtime.store.snapshot()
        return tuple(key for key, value in after.items() if before[key] != value)

    def set_connections(self, task: TaskId, connections: tuple[ConnectionInfo, ...]) -> None:
        self.router.set_connections(task, connections)


@final
@dataclass
class Session:
    broker: BrokerThread
    link: BrokerLink
    tasks: TaskSession
    canvas: RoutedCanvas
    dispatcher: ManualDispatcher
    timers: _Timers
    registry: ToolRegistry

    @contextmanager
    def reply(self, script: Sequence[FakeTurn]) -> Iterator[Rig]:
        with make_rig(
            list(script), registry=self.registry, dispatcher=self.dispatcher, tasks=self.tasks
        ) as rig:
            yield rig

    def results(self, rig: Rig, request_index: int) -> list[ToolResultBlock]:
        message = rig.provider.requests[request_index].messages[-1]
        return [block for block in message.content if isinstance(block, ToolResultBlock)]


@contextmanager
def broker() -> Iterator[BrokerThread]:
    thread = BrokerThread(prompts=ScriptedPrompts())
    thread.rig.sandbox.result = RAN
    try:
        yield thread
    finally:
        thread.stop()


def session(thread: BrokerThread) -> Session:
    dispatcher, timers = ManualDispatcher(), _Timers()
    link = thread.link()
    tasks = TaskSession(link, dispatcher)
    canvas = RoutedCanvas(link, dispatcher, timers)
    canvas_link = CanvasLink(dispatcher)
    canvas_link.attach(canvas)  # type: ignore[arg-type]  # a CanvasHost, in the shape the test needs
    registry = ToolRegistry()
    register_canvas_tools(registry, canvas_link, tasks)
    register_access_tools(registry, tasks, link)
    register_saved_tool_tools(registry, tasks, link, canvas_link)
    register_composition_tools(registry, tasks, link, canvas_link)
    return Session(thread, link, tasks, canvas, dispatcher, timers, registry)


def seed(thread: BrokerThread) -> tuple[ToolId, ToolId, ToolId]:
    """The user's saved tools: a photo tool (an image out), a collage tool (an image in) and a
    notes tool (text in)."""

    async def work(client: BrokerClient) -> tuple[ToolId, ToolId, ToolId]:
        return (
            await stored_tool(client, "Photo tool", outputs={"photo": "image"}),
            await stored_tool(client, "Collage tool", inputs={"photo": "image"}),
            await stored_tool(client, "Notes tool", inputs={"notes": "text"}),
        )

    return thread.link().call_blocking(work, deadline=20)


class TestFeedOneToolIntoAnother:
    def test_the_photo_tools_output_flows_into_the_collage_tool_across_tasks(self) -> None:
        with broker() as thread:
            photo, collage, _ = seed(thread)
            chat = session(thread)
            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("open_tool", tool=str(photo), placement="stack"),)),
                    FakeTurn(chunks=("The photo tool is open.",)),
                ]
            ) as rig:
                rig.send_and_wait("open my photo tool")
                (opened,) = chat.results(rig, 1)
                assert not opened.is_error, opened.content
                assert "Output ports: photo (image)" in opened.content
            (producer,) = chat.canvas.instances
            with chat.reply(
                [
                    FakeTurn(
                        tool_calls=(
                            call(
                                "patch_state",
                                instance=str(producer),
                                patch={"photo": {"path": "out/a.png"}},
                            ),
                        )
                    ),
                    FakeTurn(tool_calls=(call("start_task", title="Make a collage"),)),
                    FakeTurn(tool_calls=(call("open_tool", tool=str(collage), placement="stack"),)),
                    FakeTurn(chunks=("Both tools are open.",)),
                ]
            ) as rig:
                rig.send_and_wait("now a collage")
                assert not any(r.is_error for i in (1, 2, 3) for r in chat.results(rig, i))
            _, consumer = chat.canvas.instances
            ports = ConnectArgs(producer, "photo", consumer, "photo")

            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("connect_tools", **ports.as_input()),)),
                    FakeTurn(chunks=("Connected.",)),
                ]
            ) as rig:
                rig.send_and_wait("feed the photo into the collage")
                (connected,) = chat.results(rig, 1)

            assert not connected.is_error, connected.content
            assert "as connection conn_" in connected.content
            (entry,) = thread.rig.audit_of(ConnectionMade)
            assert entry.from_instance == producer
            assert entry.to_instance == consumer
            assert entry.producer_task != entry.task  # across tasks
            # The value that was there is passed on at once, and it runs the consumer's handler.
            runtime = chat.canvas.runtimes[consumer]
            assert runtime.store.value(StateKey("photo")) == ImageRef(path="out/a.png")
            chat.timers.fire()
            chat.dispatcher.run_until(
                lambda: (
                    any(e.outcome == "authorised" for e in thread.rig.audit_of(SandboxRun))
                    and not chat.timers.due
                )
            )
            chat.dispatcher.drain(settle=0.2)
            (run,) = [e for e in thread.rig.audit_of(SandboxRun) if e.outcome == "authorised"]
            assert run.inputs == (entry.connection,)
            policy = thread.rig.sandbox.policies[-1]
            assert [str(mount.target) for mount in policy.inputs] == [f"/inputs/{entry.connection}"]
            payload = json.loads(policy.payload or b"")
            assert payload["inputs"] == {
                "photo": {"path": "out/a.png", "root": f"/inputs/{entry.connection}"}
            }
            assert chat.canvas.runtimes[consumer].store.value(StateKey("status")) == "collaged"
            # A new photo is passed on again.
            chat.canvas.patch(producer, {"photo": {"path": "out/b.png"}})
            assert runtime.store.value(StateKey("photo")) == ImageRef(path="out/b.png")

    def test_a_type_mismatch_and_an_unsaved_tool_are_told_to_the_model(self) -> None:
        with broker() as thread:
            photo, _, notes = seed(thread)
            chat = session(thread)
            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("open_tool", tool=str(photo), placement="stack"),)),
                    FakeTurn(tool_calls=(call("open_tool", tool=str(notes), placement="stack"),)),
                    FakeTurn(chunks=("Both are open.",)),
                ]
            ) as rig:
                rig.send_and_wait("open both")
            producer, consumer = chat.canvas.instances
            drawn = {
                "version": "tumbleweed.ui/1",
                "title": "Scratch",
                "root": {"kind": "box"},
                "state": [{"kind": "string", "key": "notes", "default": ""}],
            }
            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("show_ui", spec=drawn, placement="stack"),)),
                    FakeTurn(chunks=("Drawn.",)),
                ]
            ) as rig:
                rig.send_and_wait("and a scratch tool")
            scratch = chat.canvas.instances[-1]

            with chat.reply(
                [
                    FakeTurn(
                        tool_calls=(
                            call(
                                "connect_tools",
                                **ConnectArgs(producer, "photo", consumer, "notes").as_input(),
                            ),
                            call(
                                "connect_tools",
                                **ConnectArgs(scratch, "notes", consumer, "notes").as_input(),
                            ),
                        )
                    ),
                    FakeTurn(chunks=("I could not.",)),
                ]
            ) as rig:
                rig.send_and_wait("connect them")
                mismatch, unsaved = chat.results(rig, 1)

            assert mismatch.is_error
            assert (
                "The types differ: that output is image and that input takes text."
                in mismatch.content
            )
            assert unsaved.is_error
            assert unsaved.content == (
                "That tool was made with show_ui and is not saved, so it cannot be connected. "
                "Save it with save_tool, open it with open_tool, then connect it."
            )
            assert thread.rig.audit_of(ConnectionMade) == []


@dataclass(frozen=True)
class ConnectArgs:
    from_instance: ToolInstanceId
    from_port: str
    to_instance: ToolInstanceId
    to_port: str

    def as_input(self) -> dict[str, JsonValue]:
        return {
            "from_instance": str(self.from_instance),
            "from_port": self.from_port,
            "to_instance": str(self.to_instance),
            "to_port": self.to_port,
        }
