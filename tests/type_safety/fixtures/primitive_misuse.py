"""Deliberate misuse of the domain primitives. Every line marked `# expect: <code>: <message>`
must be rejected by mypy with that error code and a message containing that text, and mypy must
report nothing else (tests/type_safety/test_mypy_rejects_misuse.py checks both).
"""

from pathlib import Path

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


def needs_canonical(path: CanonicalPath) -> None: ...
def needs_workspace_rel(path: WorkspaceRelPath) -> None: ...
def needs_byte_count(size: ByteCount) -> None: ...
def needs_tool_version(version: ToolVersion) -> None: ...
def needs_content_hash(digest: ContentHash) -> None: ...
def needs_model(model: ModelName) -> None: ...
def needs_request_id(request: ProviderRequestId) -> None: ...
def send(payload: JsonValue) -> None: ...


def a_canonical_path_is_not_a_str_a_path_or_a_workspace_path(
    text: str, path: Path, relative: WorkspaceRelPath
) -> None:
    needs_canonical(text)  # expect: arg-type: incompatible type "str"; expected "CanonicalPath"
    needs_canonical(path)  # expect: arg-type: incompatible type "Path"; expected "CanonicalPath"
    needs_canonical(relative)  # expect: arg-type: incompatible type "WorkspaceRelPath"


def a_canonical_path_has_no_constructor_only_the_factory() -> None:
    CanonicalPath("/etc")  # expect: call-arg: Too many arguments for "CanonicalPath"


def a_canonical_path_cannot_be_opened_directly(path: CanonicalPath) -> None:
    open(path)  # expect: call-overload: No overload variant of "open" matches


def a_workspace_path_is_not_a_str_a_path_or_a_canonical_path(
    text: str, path: Path, canonical: CanonicalPath
) -> None:
    needs_workspace_rel(text)  # expect: arg-type: incompatible type "str"; expected "WorkspaceRelPath"
    needs_workspace_rel(path)  # expect: arg-type: incompatible type "Path"; expected "WorkspaceRelPath"
    needs_workspace_rel(canonical)  # expect: arg-type: incompatible type "CanonicalPath"


def counts_and_versions_are_not_bare_ints_or_each_other(number: int, count: ByteCount) -> None:
    needs_byte_count(number)  # expect: arg-type: incompatible type "int"; expected "ByteCount"
    needs_byte_count(5)  # expect: arg-type: incompatible type "int"; expected "ByteCount"
    needs_byte_count(ToolVersion(1))  # expect: arg-type: incompatible type "ToolVersion"
    needs_tool_version(number)  # expect: arg-type: incompatible type "int"; expected "ToolVersion"
    needs_tool_version(count)  # expect: arg-type: incompatible type "ByteCount"
    needs_byte_count(count + count)  # expect: arg-type: incompatible type "int"; expected "ByteCount"


def strings_are_not_bare_strs_or_each_other(text: str, model: ModelName) -> None:
    needs_content_hash(text)  # expect: arg-type: incompatible type "str"; expected "ContentHash"
    needs_content_hash(model)  # expect: arg-type: incompatible type "ModelName"
    needs_model(text)  # expect: arg-type: incompatible type "str"; expected "ModelName"
    needs_model(ProviderRequestId("x"))  # expect: arg-type: incompatible type "ProviderRequestId"
    needs_request_id(text)  # expect: arg-type: incompatible type "str"; expected "ProviderRequestId"
    needs_request_id(model)  # expect: arg-type: incompatible type "ModelName"


def json_values_are_json(path: Path) -> None:
    send({"a": path})  # expect: dict-item: has incompatible type "str": "Path"
    send({1, 2})  # expect: arg-type: incompatible type "set[int]"
