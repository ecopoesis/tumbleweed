"""Save, reuse and refine, end to end with the fake provider (S24 acceptance).

A real broker daemon on a thread (a fake sandbox that records what it is given, a scripted user),
the real link, task session, tool registry, responder and chat controller, a fake provider that
plays the model, and a canvas that runs the real UI runtime and the real `HandlerRuns` and executor.
Every check on the broker's side reads what the broker did (the audit log, the policy the sandbox
was given), not what the shell said.
"""

import json
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import final

from pydantic import JsonValue

from tests.agent.broker_thread import BrokerThread
from tests.agent.support import ManualDispatcher, Rig, make_rig
from tests.broker.rig import ScriptedPrompts, canned_result
from tests.tools.support import SOURCE, csv_ui
from tests.ui.spec.support import golden_text
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
from tumbleweed.agent.code import (
    BrokerHandlerExecutor,
    HandlerCall,
    HandlerOutcome,
    register_code_tools,
)
from tumbleweed.agent.fake import FakeToolCall, FakeTurn
from tumbleweed.agent.handler_runs import HandlerRuns
from tumbleweed.agent.llm import ToolResultBlock
from tumbleweed.agent.saved_tools import register_saved_tool_tools
from tumbleweed.agent.tasks import TaskSession
from tumbleweed.agent.tool_access import BrokerToolAccess, ToolAccessKind
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.broker.audit import GrantRevoked, SandboxRun, ToolOpened, ToolSaved
from tumbleweed.broker.client import BrokerClient
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.prompts import Allowed
from tumbleweed.core.ids import GrantId, TaskId, ToolId, ToolInstanceId
from tumbleweed.core.types import ContentHash
from tumbleweed.ipc.tool_messages import ToolGrantInfo
from tumbleweed.tools.package import CodeModule
from tumbleweed.ui.runtime import UiEvent, UiRuntime
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.names import HandlerName, ModuleName, StateKey

SPEC = json.loads(golden_text("csv_viewer"))
CODE: list[JsonValue] = [{"name": "csv_view", "source": SOURCE}]
PORTS: dict[str, JsonValue] = {
    "inputs": [{"name": "csv_path", "type": "text"}],
    "outputs": [{"name": "data", "type": "table"}],
}
RAN = canned_result(stdout=b'{"ok": true, "outputs": {"status": "loaded"}}')


def call(tool: str, /, **arguments: JsonValue) -> FakeToolCall:
    return FakeToolCall(name=tool, input=dict(arguments))


class _NoTimers:
    def call_later(self, seconds: float, callback: Callable[[], None]) -> Callable[[], None]:
        raise AssertionError("a click with no debounce waits for nothing")


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
class StoredCanvas:
    """A canvas host with the real runtime and handler runs, and nothing of GTK."""

    def __init__(self, link: BrokerLink, dispatcher: ManualDispatcher) -> None:
        self._executor = BrokerHandlerExecutor(link, dispatcher, wall_seconds=5)
        self.shown: list[InstanceSource] = []
        self.instances: list[ToolInstanceId] = []
        self.targets: dict[ToolInstanceId, _Target] = {}
        self.runtimes: dict[ToolInstanceId, UiRuntime] = {}
        self._sources: dict[ToolInstanceId, InstanceSource] = {}
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
            scheduler=_NoTimers(),
            target=target,
            stored=stored,
        )
        runtime = UiRuntime(spec, sandbox=runs)
        target.runtime = runtime
        if placement == "replace":
            self.runtimes.clear()
        self.runtimes[instance] = runtime
        self.targets[instance] = target
        source = InstanceSource(spec, code, stored, task)
        self._sources[instance] = source
        self.shown.append(source)
        self.instances.append(instance)
        return ShownTool(instance=instance, on_canvas=len(self.runtimes))

    def source(self, instance: ToolInstanceId) -> InstanceSource:
        found = self._sources.get(instance)
        if found is None or instance not in self.runtimes:
            raise UnknownInstanceError("gone")
        return found

    def patch(self, instance: ToolInstanceId, patch: Mapping[str, object]) -> tuple[StateKey, ...]:
        raise AssertionError("not used here")

    def click(self, instance: ToolInstanceId) -> None:
        """The user presses the tool's button: a direct input event."""
        self.runtimes[instance].user_event(
            UiEvent(handler=HandlerName("load"), component="button", event="click")
        )


@final
@dataclass
class Session:
    """One shell: its own connection to the broker, task session, registry and canvas."""

    broker: BrokerThread
    link: BrokerLink
    tasks: TaskSession
    canvas: StoredCanvas
    dispatcher: ManualDispatcher
    registry: ToolRegistry

    @contextmanager
    def reply(self, script: Sequence[FakeTurn]) -> Iterator[Rig]:
        with make_rig(
            list(script), registry=self.registry, dispatcher=self.dispatcher, tasks=self.tasks
        ) as rig:
            yield rig

    def user_run(self, instance: ToolInstanceId) -> None:
        """Click the tool and wait until the broker has run (or refused) the handler."""
        before = len(self.broker.rig.audit_of(SandboxRun))
        self.canvas.click(instance)
        self.dispatcher.run_until(
            lambda: (
                len(self.broker.rig.audit_of(SandboxRun)) > before
                and not self.broker.rig.sandbox.hang
            )
        )
        self.dispatcher.drain(settle=0.2)

    def give_access(self, stored: StoredBinding, task: TaskId, folder: str) -> str:
        """The user presses the card's button and answers the broker's prompt at `tool`."""
        rig = self.broker.rig
        rig.prompts.answers.append(Allowed(scope=GrantScope.TOOL, picked=str(rig.home / folder)))
        said: list[str] = []
        kind: ToolAccessKind = "folder"
        BrokerToolAccess(self.link, self.dispatcher).request(task, stored, kind, said.append)
        self.dispatcher.run_until(lambda: bool(said))
        return said[0]

    def grants(self) -> tuple[ToolGrantInfo, ...]:
        async def work(client: BrokerClient) -> tuple[ToolGrantInfo, ...]:
            return (await client.list_tool_grants()).grants

        return self.link.call_blocking(work, deadline=10)

    def revoke(self, grant: GrantId) -> None:
        async def work(client: BrokerClient) -> None:
            await client.revoke_tool_grant(grant)

        self.link.call_blocking(work, deadline=10)


@contextmanager
def broker() -> Iterator[BrokerThread]:
    thread = BrokerThread(prompts=ScriptedPrompts())
    thread.rig.sandbox.result = RAN
    try:
        yield thread
    finally:
        thread.stop()


def session(thread: BrokerThread) -> Session:
    dispatcher = ManualDispatcher()
    link = thread.link()
    tasks = TaskSession(link, dispatcher)
    canvas = StoredCanvas(link, dispatcher)
    canvas_link = CanvasLink(dispatcher)
    canvas_link.attach(canvas)
    registry = ToolRegistry()
    register_canvas_tools(registry, canvas_link, tasks)
    register_access_tools(registry, tasks, link)
    register_code_tools(registry, tasks, link)
    register_saved_tool_tools(registry, tasks, link, canvas_link)
    return Session(thread, link, tasks, canvas, dispatcher, registry)


def results(rig: Rig, request_index: int) -> list[ToolResultBlock]:
    message = rig.provider.requests[request_index].messages[-1]
    return [block for block in message.content if isinstance(block, ToolResultBlock)]


def saved_tool_id(thread: BrokerThread) -> ToolId:
    (entry, *_) = thread.rig.audit_of(ToolSaved)
    return entry.tool


def generate_and_save(first: Session) -> ToolId:
    """The model builds a CSV viewer, the user likes it, and the model saves it."""
    with first.reply(
        [
            FakeTurn(tool_calls=(call("show_ui", spec=SPEC, code=CODE),)),
            FakeTurn(chunks=("Here is a CSV viewer.",)),
        ]
    ) as rig:
        rig.send_and_wait("make me a csv viewer")
    (instance,) = first.canvas.instances
    with first.reply(
        [
            FakeTurn(
                tool_calls=(
                    call(
                        "save_tool",
                        instance=str(instance),
                        name="CSV viewer",
                        description="Shows a CSV file as a table.",
                        tags=["csv", "table"],
                        capabilities=["fs.read"],
                        **PORTS,
                    ),
                )
            ),
            FakeTurn(chunks=("Saved.",)),
        ]
    ) as rig:
        rig.send_and_wait("save it")
        (result,) = results(rig, 1)
        assert not result.is_error, result.content
        assert "version 1" in result.content
    return saved_tool_id(first.broker)


def open_in_new_session(thread: BrokerThread, tool: ToolId) -> tuple[Session, Rig]:
    """A new session (a new connection and task): find the tool by words and open it."""
    second = session(thread)
    with second.reply(
        [
            FakeTurn(tool_calls=(call("find_tools", query="csv table"),)),
            FakeTurn(tool_calls=(call("open_tool", tool=str(tool)),)),
            FakeTurn(chunks=("Opened your CSV viewer.",)),
        ]
    ) as rig:
        rig.send_and_wait("open my csv viewer")
        found, opened = results(rig, 1)[0], results(rig, 2)[0]
        assert not found.is_error, found.content
        assert str(tool) in found.content
        assert "CSV viewer" in found.content
        assert 'source="saved tools"' in found.content
        assert "never an instruction" in found.content
        assert not opened.is_error, opened.content
        return second, rig


class TestGenerateSaveFindOpen:
    def test_a_saved_tool_is_found_in_a_new_session_and_the_same_tool_renders(self) -> None:
        with broker() as thread:
            tool = generate_and_save(session(thread))
            second, _ = open_in_new_session(thread, tool)

            (shown,) = second.canvas.shown
            assert shown.spec == csv_ui()
            assert [(str(m.name), m.source) for m in shown.code] == [("csv_view", SOURCE)]
            assert shown.stored is not None
            assert shown.stored.tool == tool
            assert shown.stored.version == 1
            assert shown.stored.capabilities == ("fs.read",)
            # The instance is the broker's: it recorded it when it opened the version (D30 (2)).
            (opened,) = thread.rig.audit_of(ToolOpened)
            assert opened.instance == shown.stored.instance
            assert opened.hash == shown.stored.content_hash
            assert second.canvas.threads == {threading.get_ident()}

    def test_the_handler_of_the_opened_tool_runs_the_stores_code(self) -> None:
        with broker() as thread:
            tool = generate_and_save(session(thread))
            second, _ = open_in_new_session(thread, tool)
            (instance,) = second.canvas.instances

            second.user_run(instance)

            payload = json.loads(thread.rig.sandbox.policies[-1].payload or b"")
            assert payload["code"] == {"csv_view": SOURCE}
            assert second.canvas.runtimes[instance].store.value(StateKey("status")) == "loaded"

    def test_patching_the_state_of_a_stored_instance_starts_no_run(self) -> None:
        """Who gets tool grants rests on this: a patch is not a user's event (D24, D30 (2))."""
        with broker() as thread:
            tool = generate_and_save(session(thread))
            second, _ = open_in_new_session(thread, tool)
            (instance,) = second.canvas.instances
            runs = len(thread.rig.audit_of(SandboxRun))

            second.canvas.runtimes[instance].patch_state({"csv_path": "/other.csv"})
            second.dispatcher.drain(settle=0.2)

            assert len(thread.rig.audit_of(SandboxRun)) == runs
            assert second.canvas.targets[instance].notices == []

    def test_find_tools_says_when_nothing_matches_and_open_tool_names_a_missing_tool(self) -> None:
        with broker() as thread:
            first = session(thread)
            script = [
                FakeTurn(tool_calls=(call("find_tools", query="spreadsheet"),)),
                FakeTurn(tool_calls=(call("open_tool", tool=str(ToolId.new())),)),
                FakeTurn(chunks=("None.",)),
            ]
            with first.reply(script) as rig:
                rig.send_and_wait("open my spreadsheet tool")

                none, missing = results(rig, 1)[0], results(rig, 2)[0]
                assert none.content.startswith("No saved tool matches.\n")
                assert missing.is_error
                assert "not_found" in missing.content


class TestRefineAndRevert:
    def test_refining_makes_version_two_and_revert_returns_to_version_one(self) -> None:
        with broker() as thread:
            first = session(thread)
            tool = generate_and_save(first)
            refined = json.loads(json.dumps(SPEC))
            refined["title"] = "CSV viewer, refined"
            with first.reply(
                [
                    FakeTurn(tool_calls=(call("show_ui", spec=refined, code=CODE),)),
                    FakeTurn(chunks=("Refined.",)),
                ]
            ) as rig:
                rig.send_and_wait("add a title")
            (_, refined_instance) = first.canvas.instances
            with first.reply(
                [
                    FakeTurn(
                        tool_calls=(
                            call(
                                "save_tool",
                                instance=str(refined_instance),
                                name="CSV viewer",
                                description="Shows a CSV file as a table.",
                                refines=str(tool),
                            ),
                        )
                    ),
                    FakeTurn(tool_calls=(call("revert_tool", tool=str(tool), version=1),)),
                    FakeTurn(tool_calls=(call("open_tool", tool=str(tool), source=True),)),
                    FakeTurn(chunks=("Back to the first.",)),
                ]
            ) as rig:
                rig.send_and_wait("save that as an update, then go back to the first one")

                saved, reverted, opened = (results(rig, i)[0] for i in (1, 2, 3))
                assert "version 2" in saved.content
                assert "saved again as version 3" in reverted.content
                assert not opened.is_error, opened.content
                assert "Opened version 3" in opened.content
                assert 'source="the saved tool\'s UI spec"' in opened.content
            versions = [(e.version, e.reverted_from) for e in thread.rig.audit_of(ToolSaved)]
            assert versions == [(1, None), (2, None), (3, 1)]
            shown = first.canvas.shown[-1]
            assert shown.spec == csv_ui()  # the first version's spec, not the refined one
            assert shown.stored is not None
            assert shown.stored.version == 3

    def test_a_refinement_of_a_tool_that_does_not_exist_is_refused_with_the_brokers_words(
        self,
    ) -> None:
        with broker() as thread:
            first = session(thread)
            with first.reply(
                [
                    FakeTurn(tool_calls=(call("show_ui", spec=SPEC, code=CODE),)),
                    FakeTurn(chunks=("ok",)),
                ]
            ) as rig:
                rig.send_and_wait("csv")
            (instance,) = first.canvas.instances
            with first.reply(
                [
                    FakeTurn(
                        tool_calls=(
                            call(
                                "save_tool",
                                instance=str(instance),
                                name="x",
                                refines=str(ToolId.new()),
                            ),
                        )
                    ),
                    FakeTurn(chunks=("no",)),
                ]
            ) as rig:
                rig.send_and_wait("save")

                (result,) = results(rig, 1)
                assert result.is_error
                assert "not_found" in result.content

    def test_a_tool_that_is_not_on_the_canvas_is_not_saved(self) -> None:
        with broker() as thread:
            first = session(thread)
            script = [
                FakeTurn(
                    tool_calls=(call("save_tool", instance=str(ToolInstanceId.new()), name="x"),)
                ),
                FakeTurn(chunks=("no",)),
            ]
            with first.reply(script) as rig:
                rig.send_and_wait("save")

                (result,) = results(rig, 1)
                assert result.is_error
                assert "no tool" in result.content.lower()
                assert thread.rig.audit_of(ToolSaved) == []


class TestToolGrantsEndToEnd:
    def test_a_tool_grant_serves_the_users_run_and_not_the_models_nor_the_next_version(
        self,
    ) -> None:
        with broker() as thread:
            first = session(thread)
            tool = generate_and_save(first)
            second, _ = open_in_new_session(thread, tool)
            (instance,) = second.canvas.instances
            stored = second.canvas.shown[0].stored
            assert stored is not None
            task = second.tasks.current
            assert task is not None

            said = second.give_access(stored, task, "Photos")
            second.user_run(instance)
            granted_mounts = mounted(thread)

            assert "whenever you use it" in said
            assert granted_mounts == [str(thread.rig.home / "Photos")]
            # The model's own code run in the same task gets none (a tool grant is not a task's).
            with second.reply(
                [
                    FakeTurn(tool_calls=(call("run_code", source="print(1)"),)),
                    FakeTurn(chunks=("ran",)),
                ]
            ) as rig:
                rig.send_and_wait("try something")
            assert mounted(thread) == []

            # A new version is another hash: the grant does not reach it, and must be given again.
            with second.reply(
                [
                    FakeTurn(
                        tool_calls=(
                            call(
                                "save_tool",
                                instance=str(instance),
                                name="CSV viewer",
                                description="v2",
                                refines=str(tool),
                            ),
                        )
                    ),
                    FakeTurn(tool_calls=(call("open_tool", tool=str(tool), placement="stack"),)),
                    FakeTurn(chunks=("v2",)),
                ]
            ) as rig:
                rig.send_and_wait("save an update and open it")
                assert not any(r.is_error for i in (1, 2) for r in results(rig, i))
            version_two = second.canvas.instances[-1]
            assert second.canvas.shown[-1].stored is not None
            assert second.canvas.shown[-1].stored.version == 2
            second.user_run(version_two)
            assert mounted(thread) == []
            second.user_run(instance)  # version 1 still has its grant
            assert len(mounted(thread)) == 1

    def test_modified_code_never_gets_the_grant_whatever_the_shell_says(self) -> None:
        with broker() as thread:
            tool = generate_and_save(session(thread))
            second, _ = open_in_new_session(thread, tool)
            (instance,) = second.canvas.instances
            stored = second.canvas.shown[0].stored
            assert stored is not None
            task = second.tasks.current
            assert task is not None
            second.give_access(stored, task, "Photos")
            executor = BrokerHandlerExecutor(second.link, second.dispatcher, wall_seconds=5)
            runs_before = len(thread.rig.sandbox.policies)

            forgeries = (
                _binding(stored, content_hash=ContentHash("0" * 64)),  # the hash of other code
                _binding(stored, instance=ToolInstanceId.new()),  # an instance it never opened
                _binding(stored, tool=ToolId.new()),  # another tool
            )

            outcomes: list[HandlerOutcome] = []
            for forged in forgeries:
                executor.start(task, _call_with_code(forged), outcomes.append)
                second.dispatcher.run_until(_has(outcomes, len(outcomes) + 1))

            assert [o.failure for o in outcomes] == ["rejected"] * 3
            assert len(thread.rig.sandbox.policies) == runs_before
            assert instance in second.canvas.instances

    def test_revoking_works_takes_effect_for_the_next_run_and_is_audited(self) -> None:
        with broker() as thread:
            tool = generate_and_save(session(thread))
            second, _ = open_in_new_session(thread, tool)
            (instance,) = second.canvas.instances
            stored = second.canvas.shown[0].stored
            assert stored is not None
            task = second.tasks.current
            assert task is not None
            second.give_access(stored, task, "Photos")
            second.user_run(instance)
            assert len(mounted(thread)) == 1
            (info,) = second.grants()

            second.revoke(info.grant)
            second.user_run(instance)

            assert mounted(thread) == []
            assert second.grants() == ()
            (entry,) = thread.rig.audit_of(GrantRevoked)
            assert (entry.grant, entry.reason, entry.task) == (info.grant, "revoked_by_user", None)


def _has(outcomes: list[HandlerOutcome], count: int) -> Callable[[], bool]:
    return lambda: len(outcomes) >= count


def mounted(thread: BrokerThread) -> list[str]:
    """What the last run the broker gave the sandbox had mounted from the user's files."""
    return [str(grant.target) for grant in thread.rig.sandbox.policies[-1].grants]


def _binding(
    stored: StoredBinding,
    *,
    content_hash: ContentHash | None = None,
    instance: ToolInstanceId | None = None,
    tool: ToolId | None = None,
) -> StoredBinding:
    return StoredBinding(
        instance=instance or stored.instance,
        tool=tool or stored.tool,
        version=stored.version,
        content_hash=content_hash or stored.content_hash,
        capabilities=stored.capabilities,
    )


def _call_with_code(stored: StoredBinding) -> HandlerCall:
    """What a compromised shell might send: a stored tool's handler, with code of its own."""
    return HandlerCall(
        "csv_view",
        "load",
        {StateKey("csv_path"): "/x.csv"},
        (CodeModule(name=ModuleName("csv_view"), source="print('modified')"),),
        stored,
    )
