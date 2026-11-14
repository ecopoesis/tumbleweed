"""A stored tool's handler run carries no code and is the user's (S24, D30 (2), (3))."""

import json

from tests.agent.test_handler_runs import CODE, TASK, FakeExecutor, FakeScheduler, Notices, Target
from tests.ui.spec.support import golden_text
from tumbleweed.agent.canvas import StoredBinding
from tumbleweed.agent.code import HandlerCall, _origin_of, handler_run
from tumbleweed.agent.handler_runs import HandlerRuns
from tumbleweed.core.ids import ToolId, ToolInstanceId
from tumbleweed.core.types import ContentHash, ToolVersion
from tumbleweed.ui.runtime import UiEvent, UiRuntime
from tumbleweed.ui.spec import parse_ui_spec
from tumbleweed.ui.spec.handlers import SandboxHandler
from tumbleweed.ui.spec.names import HandlerName, StateKey

STORED = StoredBinding(
    instance=ToolInstanceId.new(),
    tool=ToolId.new(),
    version=ToolVersion(2),
    content_hash=ContentHash("b" * 64),
    capabilities=("fs.read",),
)


def call(stored: StoredBinding | None) -> HandlerCall:
    return HandlerCall("photo", "adjust", {}, CODE, stored)


def test_a_stored_tools_run_sends_the_handler_and_its_inputs_and_no_code() -> None:
    prepared = handler_run(call(STORED))
    unsaved = handler_run(call(None))

    assert prepared is not None
    assert unsaved is not None
    assert json.loads(prepared[1])["code"] == {}
    assert json.loads(unsaved[1])["code"] == {"photo": CODE[0].source}


def test_the_origin_of_a_stored_run_is_a_user_event_with_the_binding_and_other_runs_have_none() -> (
    None
):
    origin = _origin_of(STORED)
    plain = _origin_of(None)

    assert origin.trigger == "user_event"
    assert origin.binding is not None
    assert (origin.binding.instance, origin.binding.tool, origin.binding.content_hash) == (
        STORED.instance,
        STORED.tool,
        STORED.content_hash,
    )
    assert plain.trigger == "model"
    assert plain.binding is None


def test_the_handler_runs_of_an_instance_pass_its_binding_to_the_executor() -> None:
    spec = parse_ui_spec(golden_text("photo_adjuster"))
    executor, notices, scheduler = FakeExecutor(), Notices(), FakeScheduler()
    holder: list[UiRuntime] = []
    runs = HandlerRuns(
        task=TASK,
        code=CODE,
        executor=executor,
        scheduler=scheduler,
        target=Target(lambda: holder[0], notices),
        stored=STORED,
    )
    runtime = UiRuntime(spec, sandbox=runs)
    holder.append(runtime)
    handler = next(h for h in spec.handlers if isinstance(h, SandboxHandler))

    runs.run(
        handler,
        UiEvent(HandlerName(str(handler.name)), "button", "click", None),
        {StateKey(str(key)): runtime.store.value(key) for key in handler.inputs},
    )

    scheduler.advance(5)  # the handler's debounce

    assert [c.stored for c, _ in executor.calls] == [STORED]
