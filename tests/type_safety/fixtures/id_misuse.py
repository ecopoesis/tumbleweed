"""Deliberate misuse of the typed IDs. Every line marked `# expect: <code>: <message>` must be
rejected by mypy with that error code and a message containing that text, and mypy must report
nothing else (tests/type_safety/test_mypy_rejects_misuse.py checks both).
"""

from pydantic import BaseModel

from tumbleweed.core.ids import (
    ConnectionId,
    GrantId,
    InvocationId,
    IpcRequestId,
    MessageId,
    PromptId,
    RunId,
    SessionId,
    SnapshotId,
    TaskId,
    ToolId,
    ToolInstanceId,
)


def needs_session(value: SessionId) -> None: ...
def needs_message(value: MessageId) -> None: ...
def needs_task(value: TaskId) -> None: ...
def needs_tool(value: ToolId) -> None: ...
def needs_instance(value: ToolInstanceId) -> None: ...
def needs_grant(value: GrantId) -> None: ...
def needs_prompt(value: PromptId) -> None: ...
def needs_run(value: RunId) -> None: ...
def needs_snapshot(value: SnapshotId) -> None: ...
def needs_connection(value: ConnectionId) -> None: ...
def needs_request(value: IpcRequestId) -> None: ...
def needs_invocation(value: InvocationId) -> None: ...
def needs_str(value: str) -> None: ...


class Record(BaseModel):
    task: TaskId


def tool_where_task_expected() -> None:
    needs_task(ToolId.new())  # expect: arg-type: incompatible type "ToolId"; expected "TaskId"
    needs_tool(TaskId.new())  # expect: arg-type: incompatible type "TaskId"; expected "ToolId"


def a_bare_str_where_any_id_is_expected(text: str) -> None:
    needs_session(text)  # expect: arg-type: incompatible type "str"; expected "SessionId"
    needs_message(text)  # expect: arg-type: incompatible type "str"; expected "MessageId"
    needs_task(text)  # expect: arg-type: incompatible type "str"; expected "TaskId"
    needs_tool(text)  # expect: arg-type: incompatible type "str"; expected "ToolId"
    needs_instance(text)  # expect: arg-type: incompatible type "str"; expected "ToolInstanceId"
    needs_grant(text)  # expect: arg-type: incompatible type "str"; expected "GrantId"
    needs_prompt(text)  # expect: arg-type: incompatible type "str"; expected "PromptId"
    needs_run(text)  # expect: arg-type: incompatible type "str"; expected "RunId"
    needs_snapshot(text)  # expect: arg-type: incompatible type "str"; expected "SnapshotId"
    needs_connection(text)  # expect: arg-type: incompatible type "str"; expected "ConnectionId"
    needs_request(text)  # expect: arg-type: incompatible type "str"; expected "IpcRequestId"
    needs_invocation(text)  # expect: arg-type: incompatible type "str"; expected "InvocationId"


def a_literal_str_is_no_better() -> None:
    needs_task("tsk_01j9z3c7q4v8s2k5m6n7p8r9t0")  # expect: arg-type: incompatible type "str"


def a_bare_int_or_uuid_where_an_id_is_expected(number: int, raw: bytes) -> None:
    needs_task(number)  # expect: arg-type: incompatible type "int"; expected "TaskId"
    needs_task(raw)  # expect: arg-type: incompatible type "bytes"; expected "TaskId"


def an_id_is_not_a_str() -> None:
    needs_str(TaskId.new())  # expect: arg-type: incompatible type "TaskId"; expected "str"


def assignments_and_returns(text: str) -> TaskId:
    task: TaskId = text  # expect: assignment: expression has type "str", variable has type "TaskId"
    task = ToolId.new()  # expect: assignment: expression has type "ToolId", variable has type "TaskId"
    return ToolId.new()  # expect: return-value: got "ToolId", expected "TaskId"


def a_dict_keyed_by_one_id_type_rejects_another() -> None:
    by_task: dict[TaskId, int] = {}
    by_task[ToolId.new()] = 1  # expect: index: Invalid index type "ToolId"


def there_is_no_construction_without_text() -> None:
    TaskId()  # expect: call-arg: Missing positional argument "text"


def a_pydantic_field_typed_with_an_id_rejects_other_types(text: str) -> None:
    Record(task=text)  # expect: arg-type: incompatible type "str"; expected "TaskId"
    Record(task=ToolId.new())  # expect: arg-type: incompatible type "ToolId"; expected "TaskId"


class OwnId(TaskId, prefix="mine"):  # expect: misc: Cannot inherit from final class "TaskId"
    pass
