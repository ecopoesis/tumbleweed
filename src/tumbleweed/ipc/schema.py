"""The JSON Schema of every IPC frame, committed as `schemas/ipc.v<N>.json`.

The schema is generated from the models (`wire.Frame` and, inside it, `messages.RequestBody`
and `messages.ResponseBody`), so it is complete by construction. The committed file is
a review artefact and a drift guard: `tests/ipc/test_schema.py` fails until it matches what this
module renders. Regenerate it with

    uv run python -m tumbleweed.ipc.schema > schemas/ipc.v1.json
"""

import json
import sys
from typing import Final

from pydantic import JsonValue, TypeAdapter

from tumbleweed.ipc.wire import FRAME_ADAPTER, PROTOCOL_VERSION

SCHEMA_FILE_NAME: Final = f"ipc.v{PROTOCOL_VERSION}.json"
SCHEMA_TITLE: Final = f"tumbleweed.ipc/{PROTOCOL_VERSION}"
_JSON_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
_OBJECT: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])


def build_schema() -> dict[str, JsonValue]:
    """The JSON Schema of a frame, as the receiver validates it."""
    # Validated back into JSON types: pydantic types its schema as `dict[str, Any]`.
    schema = _OBJECT.validate_python(FRAME_ADAPTER.json_schema(mode="validation"))
    return {
        "$schema": _JSON_SCHEMA_DIALECT,
        **schema,
        "title": SCHEMA_TITLE,
        "description": (
            "One frame of the Tumbleweed IPC protocol: the UTF-8 JSON payload of a record "
            "that is prefixed with its length as 4 bytes, big endian. Generated from "
            "tumbleweed.ipc; do not edit."
        ),
    }


def render_schema() -> str:
    """The schema as the committed file holds it: sorted keys, two-space indent, final newline."""
    return json.dumps(build_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render_schema())
