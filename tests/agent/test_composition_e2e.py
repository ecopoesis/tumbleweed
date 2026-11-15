"""Tool composition end to end with the fake provider (S25 acceptance).

"Feed the photo tool's output into the collage tool": a real broker daemon on a thread (a fake
sandbox that records what it is given, a scripted user), the real link, task session, registry,
responder and chat controller, a fake provider that plays the model, and a canvas that runs the
real UI runtime, the real handler runs and the real connection router. What the broker did is read
from its audit log and from the policy its sandbox was given, not from what the shell said.
"""

import json
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
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
from tumbleweed.agent.tool_access import BrokerToolAccess
from tumbleweed.agent.tools import ToolError, ToolRegistry
from tumbleweed.broker.audit import ConnectionMade, SandboxRun
from tumbleweed.broker.client import BrokerClient
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.prompts import Allowed
from tumbleweed.core.ids import TaskId, ToolId, ToolInstanceId
from tumbleweed.core.types import WorkspaceRelPath
from tumbleweed.ipc.connection_messages import ConnectionInfo
from tumbleweed.tools.package import CodeModule
from tumbleweed.ui.runtime import Change, UiEvent, UiRuntime
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.names import HandlerName, StateKey
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

    def apply(self, handler: object, values: Mapping[str, JsonValue]) -> None:
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

    def subscribe(self, listener: Callable[[Change], None]) -> Callable[[], None]:
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
            target=target,
            stored=stored,
        )
        runtime = UiRuntime(spec, sandbox=runs)
        target.runtime = runtime
        if placement == "replace":
            for closed in list(
                self.runtimes
            ):  # as `CanvasTools` does: closed ones leave the router
                self.router.detach(closed)
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

    def patch(self, instance: ToolInstanceId, patch: Mapping[str, object]) -> tuple[StateKey, ...]:
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
    canvas_link.attach(canvas)
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
            assert runtime.store.value(StateKey("photo")) == ImageRef(
                path=WorkspaceRelPath("out/a.png")
            )
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
            assert payload["inputs"] == {"photo": {"path": f"/inputs/{entry.connection}/out/a.png"}}
            assert chat.canvas.runtimes[consumer].store.value(StateKey("status")) == "collaged"
            # A new photo is passed on again.
            chat.canvas.patch(producer, {"photo": {"path": "out/b.png"}})
            assert runtime.store.value(StateKey("photo")) == ImageRef(
                path=WorkspaceRelPath("out/b.png")
            )

    def test_a_tool_closed_by_the_default_placement_is_not_told_to_the_model_as_flowing(
        self,
    ) -> None:
        with broker() as thread:
            photo, collage, _ = seed(thread)
            chat = session(thread)
            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("open_tool", tool=str(photo)),)),  # replace
                    FakeTurn(tool_calls=(call("open_tool", tool=str(collage)),)),  # closes it
                    FakeTurn(chunks=("Both are open.",)),
                ]
            ) as rig:
                rig.send_and_wait("open both")
            producer, consumer = chat.canvas.instances
            ports = ConnectArgs(producer, "photo", consumer, "photo")

            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("connect_tools", **ports.as_input()),)),
                    FakeTurn(chunks=("Done.",)),
                ]
            ) as rig:
                rig.send_and_wait("connect them")
                (answer,) = chat.results(rig, 1)

            assert "the first tool is not on the canvas, so nothing flows." in answer.content
            assert "placement stack" in answer.content
            assert "now and whenever it changes" not in answer.content
            assert chat.canvas.router.active == frozenset()

    def test_a_connection_run_gets_no_tool_grant_and_a_click_does(self) -> None:
        with broker() as thread:
            photo, collage, _ = seed(thread)
            chat = session(thread)
            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("open_tool", tool=str(photo), placement="stack"),)),
                    FakeTurn(tool_calls=(call("start_task", title="Collage"),)),
                    FakeTurn(tool_calls=(call("open_tool", tool=str(collage), placement="stack"),)),
                    FakeTurn(chunks=("Open.",)),
                ]
            ) as rig:
                rig.send_and_wait("open both")
            producer, consumer = chat.canvas.instances
            with chat.reply(
                [
                    FakeTurn(
                        tool_calls=(
                            call(
                                "connect_tools",
                                **ConnectArgs(producer, "photo", consumer, "photo").as_input(),
                            ),
                        )
                    ),
                    FakeTurn(chunks=("Connected.",)),
                ]
            ) as rig:
                rig.send_and_wait("connect")
            source = chat.canvas.sources[consumer]
            assert source.stored is not None
            assert source.task is not None
            home = thread.rig.home
            thread.rig.prompts.answers.append(
                Allowed(scope=GrantScope.TOOL, picked=str(home / "Photos"))
            )
            said: list[str] = []
            BrokerToolAccess(chat.link, chat.dispatcher).request(
                source.task, source.stored, "folder", said.append
            )
            chat.dispatcher.run_until(lambda: bool(said))

            chat.canvas.patch(producer, {"photo": {"path": "out/b.png"}})  # a value arrives
            chat.timers.fire()
            before = len(thread.rig.audit_of(SandboxRun))
            chat.dispatcher.run_until(lambda: len(thread.rig.audit_of(SandboxRun)) > before)
            chat.dispatcher.drain(settle=0.2)
            arrived = thread.rig.sandbox.policies[-1]

            chat.canvas.runtimes[consumer].user_event(
                UiEvent(HandlerName("run"), "button", "click")
            )
            chat.dispatcher.run_until(lambda: len(thread.rig.audit_of(SandboxRun)) > before + 1)
            chat.dispatcher.drain(settle=0.2)
            clicked = thread.rig.sandbox.policies[-1]

            assert arrived.grants == ()  # tool grants serve a user's event only (D30 (2))
            assert len(clicked.grants) == 1

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
            drawn: dict[str, JsonValue] = {
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


class TestACrossTaskAnswerDoesNotShowTheCanvas:
    def answer_with(self, *, producer_shown: bool) -> str:
        with broker() as thread:
            photo, collage, _ = seed(thread)
            chat = session(thread)
            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("open_tool", tool=str(photo), placement="stack"),)),
                    FakeTurn(tool_calls=(call("start_task", title="Collage"),)),
                    FakeTurn(
                        tool_calls=(
                            call(
                                "open_tool",
                                tool=str(collage),
                                placement="stack" if producer_shown else "replace",
                            ),
                        )
                    ),
                    FakeTurn(chunks=("Open.",)),
                ]
            ) as rig:
                rig.send_and_wait("open both")
            producer, consumer = chat.canvas.instances
            assert (producer in chat.canvas.runtimes) == producer_shown
            with chat.reply(
                [
                    FakeTurn(
                        tool_calls=(
                            call(
                                "connect_tools",
                                **ConnectArgs(producer, "photo", consumer, "photo").as_input(),
                            ),
                        )
                    ),
                    FakeTurn(chunks=("Done.",)),
                ]
            ) as rig:
                rig.send_and_wait("connect")
                (answer,) = chat.results(rig, 1)
            assert not answer.is_error
            return re.sub(r"conn_[0-9a-z]+", "<conn>", answer.content)

    def test_the_answer_is_the_same_whether_or_not_the_producer_is_shown(self) -> None:
        shown = self.answer_with(producer_shown=True)
        closed = self.answer_with(producer_shown=False)

        assert shown == closed
        assert "only while it is on the canvas" in shown


class TestACanvasThatCannotBeToldNow:
    def test_a_connection_that_exists_is_not_reported_as_an_error(self) -> None:
        with broker() as thread:
            photo, collage, _ = seed(thread)
            chat = session(thread)

            def refuse(task: TaskId, connections: tuple[ConnectionInfo, ...]) -> None:
                raise ToolError("the canvas is not available")

            chat.canvas.set_connections = refuse  # type: ignore[method-assign]  # the test's own fault
            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("open_tool", tool=str(photo), placement="stack"),)),
                    FakeTurn(tool_calls=(call("open_tool", tool=str(collage), placement="stack"),)),
                    FakeTurn(chunks=("Open.",)),
                ]
            ) as rig:
                rig.send_and_wait("open both")
            producer, consumer = chat.canvas.instances
            args = ConnectArgs(producer, "photo", consumer, "photo")
            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("connect_tools", **args.as_input()),)),
                    FakeTurn(chunks=("Done.",)),
                ]
            ) as rig:
                rig.send_and_wait("connect")
                (answer,) = chat.results(rig, 1)

            assert not answer.is_error  # the broker made it: the model is not told "error"
            assert "so nothing flows yet" in answer.content
            assert len(thread.rig.audit_of(ConnectionMade)) == 1


class TestAnUnsavedToolOfAnEarlierTask:
    def test_it_is_answered_with_the_brokers_one_text_not_the_save_first_hint(self) -> None:
        with broker() as thread:
            _, _, notes = seed(thread)
            chat = session(thread)
            drawn: dict[str, JsonValue] = {
                "version": "tumbleweed.ui/1",
                "title": "Scratch",
                "root": {"kind": "box"},
                "state": [{"kind": "string", "key": "notes", "default": ""}],
            }
            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("show_ui", spec=drawn, placement="stack"),)),
                    FakeTurn(tool_calls=(call("start_task", title="Later"),)),
                    FakeTurn(tool_calls=(call("open_tool", tool=str(notes), placement="stack"),)),
                    FakeTurn(chunks=("Ready.",)),
                ]
            ) as rig:
                rig.send_and_wait("draw, then a later task")
            scratch, consumer = chat.canvas.instances
            args = ConnectArgs(scratch, "notes", consumer, "notes")

            with chat.reply(
                [
                    FakeTurn(tool_calls=(call("connect_tools", **args.as_input()),)),
                    FakeTurn(chunks=("No.",)),
                ]
            ) as rig:
                rig.send_and_wait("connect")
                (answer,) = chat.results(rig, 1)

            assert answer.is_error
            assert "No such tool instance can be connected here." in answer.content
            assert "made with show_ui and is not saved" not in answer.content  # no canvas peeking


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
