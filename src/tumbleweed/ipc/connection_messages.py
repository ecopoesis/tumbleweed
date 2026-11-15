"""The messages of tool composition (S25, D31): connect, list and remove connections.

Registered in `tumbleweed.ipc.messages`. A connection joins an output port of one stored tool's
instance to an input port of another's, inside the task that holds the consumer. The broker decides
every part from its own records (which instances exist, whose they are, their ports and types, the
taint of their tasks); the request only names them. Names of tools in the answers are the tools'
own text from the broker's index: the receiver shows them plain, capped and as the tools'.
"""

from typing import Annotated, Final, Literal, final

from pydantic import Field

from tumbleweed.core.ids import ConnectionId, TaskId, ToolInstanceId
from tumbleweed.ipc.base import IpcMessage, Request, Response
from tumbleweed.ipc.broker_messages import Taint
from tumbleweed.tools.limits import MAX_NAME_LENGTH
from tumbleweed.ui.spec.names import StateKey

WirePortType = Literal["text", "json", "table", "image", "file"]
"""The value types of a port, spelled as the wire has them (a test holds it equal to `PortType`)."""

MAX_CONNECTIONS_LISTED: Final = 64
"""The most connections a listing holds; the broker's per-task cap is below it."""


@final
class ConnectTools(Request):
    """Connect `from_port` of `from_instance` to `to_port` of `to_instance`, in `task` (the
    consumer's task). `from_instance` is an instance of `task`, or of an ended task of this
    connection's owner."""

    kind: Literal["connect_tools"]
    task: TaskId
    from_instance: ToolInstanceId
    from_port: StateKey
    to_instance: ToolInstanceId
    to_port: StateKey


@final
class ToolsConnected(Response):
    """The connection was made. `passes_on` is the taint it handed the consumer's task (empty:
    none; within one task, or the producer had none); `crossing` says the user approved a crossing
    for it (then nothing was passed on: I12)."""

    kind: Literal["tools_connected"]
    connection: ConnectionId
    port_type: WirePortType
    passes_on: Annotated[tuple[Taint, ...], Field(max_length=1)]
    crossing: bool


@final
class ConnectionInfo(IpcMessage):
    """One connection of a task, for the chrome and the router."""

    connection: ConnectionId
    from_instance: ToolInstanceId
    from_port: StateKey
    from_name: Annotated[str, Field(min_length=1, max_length=MAX_NAME_LENGTH)]
    to_instance: ToolInstanceId
    to_port: StateKey
    to_name: Annotated[str, Field(min_length=1, max_length=MAX_NAME_LENGTH)]
    port_type: WirePortType
    same_task: bool
    passes_on: Annotated[tuple[Taint, ...], Field(max_length=1)]
    crossing: bool


@final
class ListConnections(Request):
    """The connections of `task`, oldest first."""

    kind: Literal["list_connections"]
    task: TaskId


@final
class ConnectionList(Response):
    kind: Literal["connection_list"]
    connections: Annotated[tuple[ConnectionInfo, ...], Field(max_length=MAX_CONNECTIONS_LISTED)]


@final
class RemoveConnection(Request):
    """Remove a connection of `task`. The user's own action from the chrome: no model tool sends
    it. Removing one never lowers a taint."""

    kind: Literal["remove_connection"]
    task: TaskId
    connection: ConnectionId


@final
class ConnectionRemoved(Response):
    kind: Literal["connection_removed"]
