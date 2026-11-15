"""Tools side by side on the canvas, joined by the broker's connections (S25, D31 (4), (9)).

The real window and renderer, with the router and the instances' runtimes: a value one tool's
output takes reaches the other tool's input and widgets, while the connection is listed; it stops
when the listing no longer has it, when the consumer's task ends and when either tool is closed.
"""

import pytest

from tests.shell.gtk_support import Shell, bounds, labels_in, pump, pump_until
from tests.ui.gtk.support import labels_text
from tests.ui.spec.support import Json, accepted, build
from tumbleweed.core.ids import ConnectionId, TaskId
from tumbleweed.ipc.connection_messages import ConnectionInfo
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.names import StateKey

pytestmark = pytest.mark.gtk

TASK = TaskId.new()


def stage(title: str) -> UiSpec:
    return accepted(
        build(
            root={"kind": "box", "children": [{"kind": "label", "bind": "src"}]},
            state=[
                {"kind": "string", "key": "src", "default": "", "max_length": 100},
                {
                    "kind": "string",
                    "key": "out",
                    "default": "",
                    "max_length": 100,
                    "computed": {"kind": "ref", "key": "src"},
                },
            ],
            title=title,
        )
    )


def info(source: object, target: object) -> ConnectionInfo:
    return ConnectionInfo.model_validate(
        {
            "connection": ConnectionId.new(),
            "from_instance": source,
            "from_port": "out",
            "from_name": "A",
            "to_instance": target,
            "to_port": "src",
            "to_name": "B",
            "port_type": "text",
            "same_task": True,
            "passes_on": (),
            "crossing": False,
        }
    )


def test_stacked_tools_sit_side_by_side_and_a_connection_carries_a_value_between_them(
    echo_shell: Shell,
) -> None:
    window = echo_shell.window
    tools = window.canvas_tools
    first = tools.show(stage("One"), "stack", TASK)
    second = tools.show(stage("Two"), "stack", TASK)
    window.present()
    assert pump_until(lambda: window.get_mapped() and window.canvas.get_width() > 0)
    pump(0.2)
    one, two = tools.tool(first.instance), tools.tool(second.instance)
    assert one is not None
    assert two is not None

    _left, top, right, _bottom = bounds(one.widget, window)
    left_two, top_two, _right_two, _bottom_two = bounds(two.widget, window)
    assert left_two >= right  # beside it, not below it
    assert abs(top_two - top) < 40

    tools.set_connections(TASK, (info(first.instance, second.instance),))
    one.runtime.patch_state({"src": "hello"})
    pump(0.1)
    assert two.runtime.store.value(StateKey("src")) == "hello"
    assert "hello" in labels_text(two)

    tools.set_connections(TASK, ())  # the user removed it
    one.runtime.patch_state({"src": "gone"})
    assert two.runtime.store.value(StateKey("src")) == "hello"


def test_a_connection_ends_with_its_consumers_task(echo_shell: Shell) -> None:
    tools = echo_shell.window.canvas_tools
    first = tools.show(stage("One"), "stack", TASK)
    second = tools.show(stage("Two"), "stack", TASK)
    one, two = tools.tool(first.instance), tools.tool(second.instance)
    assert one is not None
    assert two is not None
    tools.set_connections(TASK, (info(first.instance, second.instance),))

    tools.task_ended(TASK)
    one.runtime.patch_state({"src": "late"})

    assert two.runtime.store.value(StateKey("src")) == ""


def test_a_value_the_consumer_does_not_take_says_so_on_its_card(echo_shell: Shell) -> None:
    tools = echo_shell.window.canvas_tools
    small: Json = build(
        state=[
            {"kind": "string", "key": "src", "default": "", "max_length": 3},
            {"kind": "string", "key": "out", "default": ""},
        ]
    )
    first = tools.show(stage("One"), "stack", TASK)
    second = tools.show(accepted(small), "stack", TASK)
    one = tools.tool(first.instance)
    assert one is not None
    tools.set_connections(TASK, (info(first.instance, second.instance),))

    one.runtime.patch_state({"src": "far too long"})

    shown = [label.get_text() for label in labels_in(echo_shell.window.canvas)]
    assert "A value from a connected tool is not one this tool accepts." in shown


def test_a_tool_of_an_earlier_task_cannot_be_written_and_its_value_is_the_one_it_ended_with(
    echo_shell: Shell,
) -> None:
    """D31 (4): what a connection passes on is the producer's own. An fs task's model patching a
    clean ended producer's card is refused, so the consumer holds what the producer ended with,
    byte for byte, as in a canvas that never had the fs task."""
    from tumbleweed.agent.canvas import FrozenInstanceError

    tools = echo_shell.window.canvas_tools
    later = TaskId.new()
    tools.task_started(TASK)
    producer = tools.show(stage("Producer"), "stack", TASK)
    one = tools.tool(producer.instance)
    assert one is not None
    one.runtime.patch_state({"src": "what it made"})
    tools.task_ended(TASK)
    tools.task_started(later)  # the fs task: its model knows the instance id
    consumer = tools.show(stage("Consumer"), "stack", later)
    two = tools.tool(consumer.instance)
    assert two is not None
    tools.set_connections(later, (info(producer.instance, consumer.instance),))
    assert two.runtime.store.value(StateKey("src")) == "what it made"

    with pytest.raises(FrozenInstanceError):
        tools.patch(producer.instance, {"src": "SECRET from the user's files"})

    assert one.runtime.store.value(StateKey("src")) == "what it made"
    assert two.runtime.store.value(StateKey("src")) == "what it made"

    # No oracle: a guess that matches the frozen state is refused exactly as one that does not,
    # and the refusal is the same whatever the state holds (s25-sec's probe, as a negative test).
    errors = []
    for guess in ("what it made", "something else", ""):
        with pytest.raises(FrozenInstanceError) as caught:
            tools.patch(producer.instance, {"src": guess})
        errors.append(str(caught.value))
    assert len(set(errors)) == 1
    assert two.runtime.store.value(StateKey("src")) == "what it made"


def test_a_tool_of_another_task_that_has_not_ended_is_read_only_to_the_model_too(
    echo_shell: Shell,
) -> None:
    from tumbleweed.agent.canvas import FrozenInstanceError

    tools = echo_shell.window.canvas_tools
    tools.task_started(TASK)
    mine = tools.show(stage("Mine"), "stack", TASK)
    other = TaskId.new()
    tools.task_started(other)  # the conversation moved on

    with pytest.raises(FrozenInstanceError):
        tools.patch(mine.instance, {"src": "x"})
    shown = tools.show(stage("Now"), "stack", other)
    assert tools.patch(shown.instance, {"src": "ok"}) == (StateKey("src"), StateKey("out"))


def test_closing_a_tool_takes_it_out_of_the_router(echo_shell: Shell) -> None:
    tools = echo_shell.window.canvas_tools
    first = tools.show(stage("One"), "stack", TASK)
    second = tools.show(stage("Two"), "stack", TASK)
    tools.set_connections(TASK, (info(first.instance, second.instance),))
    assert tools._router.active  # noqa: SLF001  (the router is the thing under test)

    tools.show(stage("Three"), "replace", TASK)  # closes both

    assert tools._router.active == frozenset()  # noqa: SLF001


def test_the_card_count_the_model_is_told_counts_only_this_tasks_cards(echo_shell: Shell) -> None:
    """#171: an earlier task's cards (an fs task's, say) are no business of this task's model."""
    tools = echo_shell.window.canvas_tools
    mine = TaskId.new()
    alone = tools.show(stage("Alone"), "replace", mine).on_canvas
    tools.close()
    earlier = TaskId.new()
    tools.show(stage("Old one"), "stack", earlier)
    tools.show(stage("Old two"), "stack", earlier)

    with_theirs = tools.show(stage("Mine"), "stack", mine).on_canvas

    assert alone == 1
    assert with_theirs == 1
    assert tools.show(stage("Mine too"), "stack", mine).on_canvas == 2
