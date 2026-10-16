"""Builders for the requests and runs of the broker's decision tests."""

from pathlib import Path

from tests.broker.consts import HASH_A
from tests.broker.support import resolve
from tumbleweed.broker import (
    AccessRequest,
    CapabilityKind,
    RunContext,
    RunTrigger,
    ToolBinding,
)
from tumbleweed.core.ids import RunId, TaskId, ToolId, ToolInstanceId
from tumbleweed.core.types import ContentHash


def run_context(
    trigger: RunTrigger = RunTrigger.MODEL,
    *,
    tool: ToolId | None = None,
    content_hash: ContentHash = HASH_A,
    run: RunId | None = None,
) -> RunContext:
    binding = (
        None
        if tool is None
        else ToolBinding(instance=ToolInstanceId.new(), tool=tool, content_hash=content_hash)
    )
    return RunContext(run=run or RunId.new(), trigger=trigger, tool=binding)


def request(
    task: TaskId,
    path: Path | str | None = None,
    *,
    capability: CapabilityKind = CapabilityKind.FS_READ,
    run: RunContext | None = None,
) -> AccessRequest:
    return AccessRequest(
        task=task,
        run=run or run_context(),
        capability=capability,
        path=None if path is None else resolve(path),
    )
