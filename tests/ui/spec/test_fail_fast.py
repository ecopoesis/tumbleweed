"""Every sequence in a spec stops at its first bad item, so a refusal costs one error, not n.

Without it pydantic goes on validating the items of a sequence after one fails, and describes
each failure (four of them for an item that matches no member of a union). The sequences are
read from the JSON Schema, which is generated from the models, so a new one without a case here
fails `test_every_sequence_has_a_case`, and each case is built with as many bad items as the
sequence may hold, so that its own length limit does not stop the work first.
"""

from collections.abc import Callable

import pytest

from tests.ui.spec.support import Json, build, refused
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec.schema import ui_spec_schema


def arrays() -> dict[str, int]:
    """Every array of the schema by name (`Def` or `Def.property`), with the most items it holds."""
    schema = ui_spec_schema()
    found: dict[str, int] = {}

    def maximum(node: JsonValue) -> int | None:
        if isinstance(node, dict) and node.get("type") == "array":
            limit = node["maxItems"]
            assert isinstance(limit, int)
            return limit
        return None

    defs = schema["$defs"]
    assert isinstance(defs, dict)
    for name, definition in defs.items():
        if (limit := maximum(definition)) is not None:
            found[name] = limit
            continue
        properties = definition.get("properties") if isinstance(definition, dict) else None
        if isinstance(properties, dict):
            for prop, node in properties.items():
                if (limit := maximum(node)) is not None:
                    found[f"{name}.{prop}"] = limit
    properties = schema["properties"]
    assert isinstance(properties, dict)
    for prop, node in properties.items():
        if (limit := maximum(node)) is not None:
            found[f"UiSpec.{prop}"] = limit
    return found


def table_default(rows: list[JsonValue], columns: list[JsonValue] | None = None) -> Json:
    columns = columns if columns is not None else [{"name": "a", "type": "int"}]
    return build(
        state=[{"kind": "table", "key": "t", "default": {"columns": columns, "rows": rows}}]
    )


def handler_spec(handler: Json) -> Json:
    return build(state=[{"kind": "int", "key": "x", "default": 0}], handlers=[handler])


def sandbox(**fields: JsonValue) -> Json:
    return handler_spec({"kind": "sandbox", "name": "h", "module": "m", "function": "f", **fields})


# Each builds a spec with `n` bad items in one sequence, each bad in exactly one way.
CASES: dict[str, Callable[[int], Json]] = {
    "Args": lambda n: build(
        state=[
            {
                "kind": "int",
                "key": "x",
                "default": 0,
                "computed": {"kind": "min", "args": [{}] * n},
            }
        ]
    ),
    "Children": lambda n: build({"kind": "box", "children": [{}] * n}),
    "Row": lambda n: table_default([[None] * n]),
    "UiSpec.state": lambda n: build(state=[{}] * n),
    "UiSpec.handlers": lambda n: build(handlers=[{}] * n),
    "Tabs.pages": lambda n: build({"kind": "tabs", "pages": [{"title": "t"}] * n}),
    "Table.columns": lambda n: table_default([], columns=[{"name": "a"}] * n),
    "Table.rows": lambda n: table_default([[None]] * n),
    "SetHandler.assignments": lambda n: handler_spec(
        {"kind": "set", "name": "h", "assignments": [{"target": "x"}] * n}
    ),
    "SandboxHandler.inputs": lambda n: sandbox(inputs=["BAD KEY"] * n, outputs=["x"]),
    "SandboxHandler.outputs": lambda n: sandbox(outputs=["BAD KEY"] * n),
    "EnumState.options": lambda n: build(
        state=[{"kind": "enum", "key": "e", "options": [""] * n, "default": "a"}]
    ),
    "ListState.default": lambda n: build(
        state=[{"kind": "list", "key": "l", "item_type": "int", "default": [None] * n}]
    ),
}


def test_every_sequence_has_a_case() -> None:
    assert set(arrays()) == set(CASES)
    assert len(CASES) == 13


@pytest.mark.parametrize("name", CASES)
def test_a_sequence_full_of_bad_items_is_one_problem(name: str) -> None:
    limit = arrays()[name]

    problems = refused(CASES[name](limit))

    assert len(problems) == 1, problems


@pytest.mark.parametrize("name", CASES)
def test_the_same_sequence_with_one_bad_item_is_the_same_one_problem(name: str) -> None:
    problems_of_one = refused(CASES[name](1))

    assert len(problems_of_one) == 1, problems_of_one
