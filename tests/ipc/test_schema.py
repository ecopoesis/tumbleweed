"""The exported JSON Schema: complete, versioned, in step with the models, and strict."""

import json
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest
from pydantic import JsonValue

from tumbleweed.ipc.broker_messages import (
    DirEntry,
    EnvPair,
    NetworkAccess,
    ReadAccess,
    WriteAccess,
)
from tumbleweed.ipc.messages import IpcMessage, Request, RequestBody, Response, ResponseBody
from tumbleweed.ipc.schema import SCHEMA_FILE_NAME, SCHEMA_TITLE, build_schema, render_schema
from tumbleweed.ipc.tool_messages import ToolSummary
from tumbleweed.ipc.wire import PROTOCOL_VERSION, ErrorFrame, Hello, RequestFrame, ResponseFrame

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "schemas" / SCHEMA_FILE_NAME
REGENERATE = f"uv run python -m tumbleweed.ipc.schema > schemas/{SCHEMA_FILE_NAME}"
FRAMES: tuple[type[IpcMessage], ...] = (Hello, RequestFrame, ResponseFrame, ErrorFrame)


def obj(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def arr(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value


def members(union: object) -> tuple[type[IpcMessage], ...]:
    """The classes of a closed `Annotated[A | B, ...]` union."""
    found = get_args(get_args(union)[0])
    classes = tuple(m for m in found if isinstance(m, type) and issubclass(m, IpcMessage))
    assert len(classes) == len(found) >= 2, "not a union of messages"
    return classes


ALL_MESSAGES: tuple[type[IpcMessage], ...] = (
    *FRAMES,
    *members(RequestBody),
    *members(ResponseBody),
)


# Models that are parts of a message, not messages themselves (nested under one).
PARTS: tuple[type[IpcMessage], ...] = (
    ReadAccess,
    WriteAccess,
    NetworkAccess,
    DirEntry,
    EnvPair,
    ToolSummary,
)


def definition(name: str) -> dict[str, JsonValue]:
    return obj(obj(build_schema()["$defs"])[name])


def kind_of(cls: type[IpcMessage]) -> str:
    (literal,) = get_args(cls.model_fields["kind"].annotation)
    assert isinstance(literal, str)
    return literal


def test_the_committed_schema_is_what_the_models_export() -> None:
    assert COMMITTED.read_text(encoding="utf-8") == render_schema(), (
        f"schemas/{SCHEMA_FILE_NAME} is out of date with tumbleweed.ipc: regenerate it with "
        f"{REGENERATE}"
    )


def test_the_documented_command_regenerates_exactly_the_committed_file() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tumbleweed.ipc.schema"],
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    )

    assert result.stdout == COMMITTED.read_text(encoding="utf-8")


def test_the_schema_is_versioned_in_its_file_name_and_its_title() -> None:
    schema = json.loads(COMMITTED.read_text(encoding="utf-8"))

    assert f"ipc.v{PROTOCOL_VERSION}.json" == SCHEMA_FILE_NAME
    assert f"tumbleweed.ipc/{PROTOCOL_VERSION}" == SCHEMA_TITLE == schema["title"]
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_the_schema_has_every_frame_and_every_registered_message_and_nothing_stale() -> None:
    expected = {cls.__name__ for cls in (*ALL_MESSAGES, *PARTS)}

    assert set(obj(build_schema()["$defs"])) == {*expected, "ErrorCode"}


@pytest.mark.parametrize("cls", PARTS, ids=[cls.__name__ for cls in PARTS])
def test_every_part_of_a_message_is_strict_and_closed_too(cls: type[IpcMessage]) -> None:
    assert definition(cls.__name__)["additionalProperties"] is False
    assert getattr(cls, "__final__", False), f"{cls.__name__} must be @final"
    assert cls.model_config == {
        "strict": True,
        "extra": "forbid",
        "frozen": True,
        "allow_inf_nan": False,
    }


def test_the_top_level_is_a_union_of_the_frames_discriminated_on_kind() -> None:
    schema = build_schema()

    refs = {obj(entry)["$ref"] for entry in arr(schema["oneOf"])}
    assert refs == {f"#/$defs/{cls.__name__}" for cls in FRAMES}
    assert obj(schema["discriminator"])["propertyName"] == "kind"


@pytest.mark.parametrize("cls", ALL_MESSAGES, ids=[cls.__name__ for cls in ALL_MESSAGES])
def test_every_message_is_strict_closed_and_requires_its_kind(cls: type[IpcMessage]) -> None:
    schema = definition(cls.__name__)

    assert schema["additionalProperties"] is False
    assert obj(obj(schema["properties"])["kind"]) == {
        "const": kind_of(cls),
        "title": "Kind",
        "type": "string",
    }
    assert "kind" in arr(schema["required"])
    assert getattr(cls, "__final__", False), f"{cls.__name__} must be @final"
    assert cls.model_config == {
        "strict": True,
        "extra": "forbid",
        "frozen": True,
        "allow_inf_nan": False,
    }


@pytest.mark.parametrize("union", [RequestBody, ResponseBody], ids=["requests", "responses"])
def test_the_kinds_within_a_registry_are_unique(union: object) -> None:
    kinds = [kind_of(cls) for cls in members(union)]

    assert len(kinds) == len(set(kinds))


def test_each_registry_holds_only_its_own_kind_of_message() -> None:
    assert all(issubclass(cls, Request) for cls in members(RequestBody))
    assert all(issubclass(cls, Response) for cls in members(ResponseBody))
    assert not set(members(RequestBody)) & set(members(ResponseBody))


def test_request_ids_are_typed_and_patterned_in_the_schema() -> None:
    pattern = obj(obj(definition("RequestFrame")["properties"])["id"])["pattern"]

    assert isinstance(pattern, str)
    assert pattern.startswith("^req_")
