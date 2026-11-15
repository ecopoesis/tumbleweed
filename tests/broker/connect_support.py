"""Helpers for the composition tests (S25, D31): tools with ports, saved and opened over IPC."""

import json
from collections.abc import Mapping

from tests.broker.rig import Rig
from tests.tools.support import module
from tests.ui.spec.support import Json, accepted, build
from tumbleweed.broker.client import BrokerClient
from tumbleweed.core.ids import RunId, TaskId, ToolId
from tumbleweed.ipc.tool_messages import ToolOpened
from tumbleweed.tools import ToolDraft

_KINDS = {"text": "string", "json": "json", "table": "table", "image": "image", "file": "file"}
SOURCE = "def go(inputs):\n    return {'status': 'ok'}\n"


def tool_draft(
    name: str,
    inputs: Mapping[str, str] | None = None,
    outputs: Mapping[str, str] | None = None,
    source: str = SOURCE,
) -> ToolDraft:
    """A tool with the given ports (name -> port type) and one sandbox handler that reads its
    inputs and writes its outputs."""
    inputs, outputs = inputs or {}, outputs or {}
    entries: list[Json] = []
    for key, kind in {**inputs, **outputs}.items():
        entry: Json = {"kind": _KINDS[kind], "key": key}
        if kind == "text":
            entry["default"] = ""
        entries.append(entry)
    entries.append({"kind": "string", "key": "status", "default": "", "max_length": 4096})
    handler: Json = {
        "kind": "sandbox",
        "name": "run",
        "module": "m",
        "function": "go",
        "inputs": list(inputs),
        "outputs": [*outputs, "status"],
    }
    return ToolDraft.model_validate(
        {
            "name": name,
            "description": "A tool of the composition tests.",
            "tags": [],
            "capabilities": [],
            "inputs": [{"name": key, "type": kind} for key, kind in inputs.items()],
            "outputs": [{"name": key, "type": kind} for key, kind in outputs.items()],
            "ui": accepted(build(state=entries, handlers=[handler])),
            "modules": [module("m", source)],
        }
    )


async def stored_tool(
    client: BrokerClient,
    name: str,
    inputs: Mapping[str, str] | None = None,
    outputs: Mapping[str, str] | None = None,
    source: str = SOURCE,
) -> ToolId:
    """Save a tool from a clean throwaway task (so its taint is none and any task may open it)."""
    task = (await client.start_task()).task
    text = json.dumps(tool_draft(name, inputs, outputs, source).model_dump(mode="json"))
    stored = await client.save_tool(task, text)
    await client.end_task(task)
    return stored.summary.tool


async def opened(client: BrokerClient, task: TaskId, tool: ToolId) -> ToolOpened:
    return await client.open_tool(task, tool)


async def fs_tainted(rig: Rig, client: BrokerClient, *, previous: TaskId | None = None) -> TaskId:
    """A task that was granted a folder: fs-tainted."""
    task = (await client.start_task(previous=previous)).task
    rig.prompts.allow("task")
    assert (await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")).granted
    return task


async def net_tainted(rig: Rig, client: BrokerClient, *, previous: TaskId | None = None) -> TaskId:
    task = (await client.start_task(previous=previous)).task
    rig.prompts.allow("task")
    assert (await client.request_network(task, RunId.new(), "x")).granted
    return task
