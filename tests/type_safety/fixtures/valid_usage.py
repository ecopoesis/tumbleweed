"""The correct counterpart of the misuse fixtures: mypy must accept every line here.

It guards the guard: if this file ever fails, the misuse results cannot be trusted.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from tumbleweed.core.ids import TaskId, ToolId
from tumbleweed.core.types import (
    ByteCount,
    CanonicalPath,
    ContentHash,
    JsonValue,
    ModelName,
    ProviderRequestId,
    ToolVersion,
    WorkspaceRelPath,
)


def needs_task(task: TaskId) -> None: ...


def needs_tool(tool: ToolId) -> None: ...


def needs_str(text: str) -> None: ...


def needs_int(number: int) -> None: ...


def needs_canonical(path: CanonicalPath) -> None: ...


def needs_workspace_rel(path: WorkspaceRelPath) -> None: ...


def needs_byte_count(size: ByteCount) -> None: ...


def needs_tool_version(version: ToolVersion) -> None: ...


def needs_content_hash(digest: ContentHash) -> None: ...


def needs_model(model: ModelName) -> None: ...


def needs_request_id(request: ProviderRequestId) -> None: ...


def send(payload: JsonValue) -> None: ...


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    task: TaskId
    size: ByteCount


def correct_usage(untrusted_text: str, untrusted_number: int) -> None:
    needs_task(TaskId.new())
    needs_task(TaskId(untrusted_text))  # parsing text is the sanctioned way in
    needs_tool(ToolId.new())
    needs_str(ModelName(untrusted_text))  # the str-based primitives are still strings
    needs_int(ByteCount(untrusted_number))
    needs_canonical(CanonicalPath.resolve(untrusted_text))
    needs_canonical(CanonicalPath.resolve(Path(untrusted_text)))
    needs_workspace_rel(WorkspaceRelPath(untrusted_text))
    needs_byte_count(ByteCount(untrusted_number))
    needs_byte_count(ByteCount(len(untrusted_text) + 1))
    needs_tool_version(ToolVersion(untrusted_number))
    needs_content_hash(ContentHash(untrusted_text))
    needs_model(ModelName(untrusted_text))
    needs_request_id(ProviderRequestId(untrusted_text))
    send({"a": [1, 2.5, "x", None, True, {"b": []}]})
    record = Record(task=TaskId.new(), size=ByteCount(1))
    needs_task(record.task)
    by_task: dict[TaskId, int] = {record.task: 1}
    assert by_task[record.task] == 1
    assert TaskId.new() == TaskId.new()
