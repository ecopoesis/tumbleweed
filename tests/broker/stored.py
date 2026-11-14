"""Helpers for the stored-tool tests (S24): saving, opening and running a tool through a client."""

import json

from tests.broker.rig import Rig
from tests.tools.support import draft
from tumbleweed.broker.client import BrokerClient
from tumbleweed.core.ids import RunId, TaskId, ToolId
from tumbleweed.ipc.broker_messages import RunOrigin, RunResult
from tumbleweed.ipc.tool_messages import ToolOpened, ToolStored
from tumbleweed.sandbox.handler_protocol import handler_command
from tumbleweed.tools import ToolDraft

SHELL_CODE = "def load(*a):\n    return {'status': 'the shell wrote this'}\n"


def draft_text(source: ToolDraft | None = None) -> str:
    return json.dumps((source or draft()).model_dump(mode="json"), ensure_ascii=False)


def call_document(*, code: dict[str, str] | None = None, function: str = "load") -> str:
    """What the shell sends for a stored tool's handler: module, function, inputs, no code."""
    return json.dumps(
        {
            "module": "csv_view",
            "function": function,
            "inputs": {"csv_path": "/x.csv", "delimiter": ",", "has_header": True},
            "code": {} if code is None else code,
        }
    )


def origin_of(
    opened: ToolOpened,
    *,
    trigger: str = "user_event",
    tool: ToolId | None = None,
    instance: object | None = None,
    content_hash: object | None = None,
) -> RunOrigin:
    return RunOrigin.model_validate(
        {
            "trigger": trigger,
            "binding": {
                "instance": str(opened.instance if instance is None else instance),
                "tool": str(opened.summary.tool if tool is None else tool),
                "content_hash": str(opened.summary.hash if content_hash is None else content_hash),
            },
        }
    )


async def save(client: BrokerClient, task: TaskId, *, tool: ToolId | None = None) -> ToolStored:
    return await client.save_tool(task, draft_text(), tool=tool)


async def run_tool(
    client: BrokerClient,
    task: TaskId,
    origin: RunOrigin,
    *,
    payload: str | None = None,
    command: tuple[str, ...] | None = None,
) -> RunResult:
    return await client.run_in_sandbox(
        task,
        RunId.new(),
        handler_command() if command is None else command,
        payload=call_document() if payload is None else payload,
        origin=origin,
    )


def sent_payload(rig: Rig) -> dict[str, object]:
    """The payload the last sandbox run was given, parsed."""
    raw = rig.sandbox.policies[-1].payload
    assert raw is not None
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed
