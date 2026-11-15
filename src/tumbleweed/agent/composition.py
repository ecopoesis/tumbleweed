"""The model's `connect_tools` (S25, D31): feed one saved tool's output into another's input.

The broker decides everything (which instances exist and are the task's to connect, the ports and
their types, the taint a connection hands on, the user's approval of a crossing); this module
asks, bounds and tells the model what happened in our own words. The one thing it checks itself is
a better text for an instance of the **current task** that was made with `show_ui` (the broker
cannot tell such an instance from one that does not exist, and says so in one text for both); it
never looks at an instance of another task, because that would tell a task that has the network
which instances a task that read files has shown (D31 (1), (8)).

A value that crosses is data from another tool and reaches the model only inside the frames that
already hold tool state (`agent.canvas_events`), never as our words.
"""

from typing import Final, final

from pydantic import BaseModel, ConfigDict, Field

from tumbleweed.agent.broker_link import BrokerLink, BrokerUnavailableError
from tumbleweed.agent.canvas import CanvasLink, UnknownInstanceError
from tumbleweed.agent.tasks import TaskError, TaskSession
from tumbleweed.agent.tools import ToolError, ToolName, ToolRegistry
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.config import PROMPT_WAIT
from tumbleweed.core.ids import ToolInstanceId
from tumbleweed.ipc.connection_messages import ConnectionList, ToolsConnected
from tumbleweed.ui.spec.names import StateKey

BROKER_SECONDS: Final = 20.0
_PROMPT_SECONDS: Final = PROMPT_WAIT.total_seconds() + BROKER_SECONDS
SAVE_FIRST: Final = (
    "That tool was made with show_ui and is not saved, so it cannot be connected. Save it with "
    "save_tool, open it with open_tool, then connect it."
)
_PASSES_ON: Final = {
    "fs": (
        " The task that made the first tool had seen the user's files, so this task now counts "
        "as having seen them."
    ),
    "net": (
        " The task that made the first tool had used the network, so this task now counts as "
        "having used it."
    ),
}
_CROSSING: Final = (
    " The user approved this one connection, which joins data from a task that saw their files "
    "with a task that used the network; this task's own record is unchanged."
)


NOT_ON_CANVAS: Final = (
    "Connected as connection {connection}, but the {which} tool is not on the canvas, so nothing "
    "flows. A closed tool never comes back under the same id, and the connection keeps holding "
    "that input until the user removes it in the Tools panel. A tool opened with the default "
    "placement closes the others: open the tools again with placement stack (at most 4 fit) and "
    "connect them again."
)
CROSS_TASK: Final = (
    "Connected as connection {connection}. A tool of an earlier task passes on what it held when "
    "its task ended, and only while it is on the canvas. If nothing arrives, ask the user to "
    "remove this connection in the Tools panel."
)
NOT_ROUTED_YET: Final = (
    "Connected as connection {connection}, but the canvas could not be told just now, so nothing "
    "flows yet. It starts when the tools are shown again."
)


async def _on_canvas(canvas: CanvasLink, instance: ToolInstanceId) -> bool:
    try:
        await canvas.source(instance)
    except UnknownInstanceError:
        return False
    return True


@final
class ConnectToolsInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    from_instance: ToolInstanceId = Field(
        description="The instance id of the tool whose output is passed on (from open_tool)."
    )
    from_port: StateKey = Field(description="The name of its output port.")
    to_instance: ToolInstanceId = Field(
        description="The instance id of the tool that receives it (from open_tool, in this task)."
    )
    to_port: StateKey = Field(description="The name of its input port. The types must match.")


def register_composition_tools(
    registry: ToolRegistry, tasks: TaskSession, link: BrokerLink, canvas: CanvasLink
) -> None:
    """Add `connect_tools` to `registry`."""

    async def connect_tools(arguments: ConnectToolsInput) -> str:
        try:
            task = await tasks.ensure()
        except TaskError as error:
            raise ToolError(str(error)) from None
        for instance in (arguments.from_instance, arguments.to_instance):
            try:
                source = await canvas.source(instance)
            except UnknownInstanceError:
                continue  # the broker knows ended tasks' instances; the canvas may not hold them
            if source.stored is None and source.task == task:
                raise ToolError(SAVE_FIRST)

        async def work(client: BrokerClient) -> tuple[ToolsConnected, ConnectionList]:
            made = await client.connect_tools(
                task,
                arguments.from_instance,
                str(arguments.from_port),
                arguments.to_instance,
                str(arguments.to_port),
            )
            return made, await client.list_connections(task)

        try:
            made, listing = await link.call(work, deadline=_PROMPT_SECONDS)
        except BrokerRefused as refusal:
            raise ToolError(
                f"The broker said no ({refusal.reason}): {refusal.message.rstrip('.')}."
            ) from None
        except BrokerUnavailableError as error:
            raise ToolError(str(error)) from None
        try:
            await canvas.set_connections(task, listing.connections)
        except (ToolError, TimeoutError):
            # The connection exists (the broker made it); only the canvas could not be told now.
            return NOT_ROUTED_YET.format(connection=made.connection)
        info = next(
            (each for each in listing.connections if each.connection == made.connection), None
        )
        if info is not None and not info.same_task:
            # A tool of an earlier task: whether its card is still shown is not for this task to
            # learn (D31 (8)), so the answer is the same whatever the canvas shows.
            return CROSS_TASK.format(connection=made.connection)
        off = [
            which
            for which, instance in (
                ("first", arguments.from_instance),
                ("second", arguments.to_instance),
            )
            if not await _on_canvas(canvas, instance)
        ]
        if off:
            return NOT_ON_CANVAS.format(connection=made.connection, which=" and ".join(off))
        note = (
            _CROSSING
            if made.crossing
            else _PASSES_ON.get(made.passes_on[0], "")
            if made.passes_on
            else ""
        )
        return (
            f"Connected {arguments.from_port} of {arguments.from_instance} to "
            f"{arguments.to_port} of {arguments.to_instance} as connection {made.connection} "
            f"({made.port_type}). While both tools are on the canvas, what the first tool puts in "
            "that port is passed to the second tool's input now and whenever it changes, and the "
            f"second tool's code runs once for each value that arrives.{note}"
        )

    registry.register(
        ToolName("connect_tools"),
        "Connect an output port of one saved tool on the canvas to an input port of another, so "
        "that its value flows on whenever it changes. Both must be saved tools opened with "
        "open_tool with placement stack (a show_ui tool must be saved first; the default "
        "placement closes the other tools, and values flow only while both are on the canvas, "
        "at most 4), the port types must be the same, and the first tool may belong to an "
        "earlier task of this conversation. A connection that would give one task both the "
        "user's files and the network is asked of the user.",
        ConnectToolsInput,
        connect_tools,
        timeout_seconds=_PROMPT_SECONDS + 5.0,
    )
