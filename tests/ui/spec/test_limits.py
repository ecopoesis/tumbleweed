"""Every hard limit, at exactly the limit and one past it (ARCHITECTURE I4 and I9).

The table below has one entry per `MAX_*` constant of `tumbleweed.ui.spec.limits`, read from the
module itself, and a test fails if one is missing: a new limit cannot be added untested. Each
entry builds something that is exactly `n` big in the way the limit counts; `n = limit` must be
accepted and `n = limit + 1` refused, so what is refused is refused for being one too many.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from tests.ui.spec.support import (
    Json,
    binary,
    build,
    lit_int,
    lit_str,
    minimal,
)
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec import limits
from tumbleweed.ui.spec.errors import SpecError
from tumbleweed.ui.spec.limits import (
    MAX_COMPONENT_DEPTH,
    MAX_CONTAINER_CHILDREN,
    MAX_EXPR_NODES,
    MAX_SAFE_INT,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    all_limits,
)
from tumbleweed.ui.spec.parse import parse_ui_spec, validate_ui_spec
from tumbleweed.ui.spec.state import check_value
from tumbleweed.ui.spec.values import Table

REFUSED = (ValidationError, SpecError, InvalidValueError)


def valid(data: Json) -> None:
    validate_ui_spec(data)


def state_entry(**fields: JsonValue) -> Json:
    return {"key": "x", **fields}


def one_state(entry: Json) -> Json:
    return build(state=[entry])


def with_handler(handler: Json, *state: Json) -> Json:
    return build(state=state, handlers=[handler])


def ints(count: int, prefix: str = "s") -> list[Json]:
    return [{"kind": "int", "key": f"{prefix}{i}", "default": 0} for i in range(count)]


def tree(nodes: int) -> Json:
    """An int expression of exactly `nodes` nodes, kept shallow by balancing."""
    if nodes == 1:
        return lit_int(1)
    if nodes == 2:
        return {"kind": "abs", "operand": lit_int(1)}
    left = (nodes - 1) // 2
    return binary("add", tree(left), tree(nodes - 1 - left))


def chain(depth: int) -> Json:
    node: Json = {"kind": "bool", "value": True}
    for _ in range(depth - 1):
        node = {"kind": "not", "operand": node}
    return node


def computed_int(key: str, expression: Json) -> Json:
    return {"kind": "int", "key": key, "default": 0, "computed": expression}


def components(count: int) -> Json:
    """A tree of exactly `count` components: boxes with at most 64 children, shallow."""
    if count == 1:
        return {"kind": "box"}
    children = min(MAX_CONTAINER_CHILDREN, count - 1)
    share, extra = divmod(count - 1, children)
    sizes = [share + 1 if i < extra else share for i in range(children)]
    nodes: list[JsonValue] = [components(size) for size in sizes]
    return {"kind": "box", "children": nodes}


def count_components(node: JsonValue) -> int:
    assert isinstance(node, dict)
    children = node.get("children", [])
    assert isinstance(children, list)
    return 1 + sum(count_components(child) for child in children)


def nested_boxes(depth: int) -> Json:
    node: Json = {"kind": "box"}
    for _ in range(depth - 1):
        node = {"kind": "box", "children": [node]}
    return node


def table_value(columns: int, rows: int) -> Json:
    cols: list[JsonValue] = [{"name": f"c{i}", "type": "int"} for i in range(columns)]
    data: list[JsonValue] = [[0] * columns for _ in range(rows)]
    return {"columns": cols, "rows": data}


def table_of_cells(cells: int) -> Json:
    for columns in range(1, MAX_TABLE_COLUMNS + 1):
        if cells % columns == 0 and cells // columns <= MAX_TABLE_ROWS:
            return table_value(columns, cells // columns)
    raise AssertionError(f"no table shape has exactly {cells} cells")


def nested_list(depth: int) -> JsonValue:
    value: JsonValue = 1
    for _ in range(depth - 1):
        value = [value]
    return value


def text_of(spec_text: str, size: int) -> str:
    return spec_text + " " * (size - len(spec_text.encode()))


def check_decl_value(kind: str, raw: object, **fields: JsonValue) -> None:
    from pydantic import TypeAdapter

    from tumbleweed.ui.spec.state import StateDecl

    decl = TypeAdapter(StateDecl).validate_python({"kind": kind, "key": "x", **fields})
    check_value(decl, raw)


@dataclass(frozen=True)
class Boundary:
    limit: str
    what: str
    check: Callable[[int], object]


def c(limit: str, what: str, check: Callable[[int], object]) -> Boundary:
    return Boundary(limit, what, check)


BOUNDARIES = [
    c("MAX_SPEC_BYTES", "raw text", lambda n: parse_ui_spec(text_of(json.dumps(minimal()), n))),
    c("MAX_TITLE_LENGTH", "title", lambda n: valid(minimal(title="t" * n))),
    c(
        "MAX_NAME_LENGTH",
        "state key",
        lambda n: valid(one_state({"kind": "bool", "key": "k" * n, "default": True})),
    ),
    c(
        "MAX_LINE_LENGTH",
        "button label",
        lambda n: valid(build({"kind": "button", "label": "x" * n})),
    ),
    c("MAX_TEXT_LENGTH", "label text", lambda n: valid(build({"kind": "label", "text": "x" * n}))),
    c(
        "MAX_TEXT_LENGTH",
        "string literal",
        lambda n: valid(
            one_state({"kind": "string", "key": "x", "default": "", "computed": lit_str("x" * n)})
        ),
    ),
    c("MAX_STATE_ENTRIES", "entries", lambda n: valid(build(state=ints(n)))),
    c(
        "MAX_HANDLERS",
        "handlers",
        lambda n: valid(build(handlers=[{"kind": "agent", "name": f"h{i}"} for i in range(n)])),
    ),
    c(
        "MAX_HANDLER_ITEMS",
        "set assignments",
        lambda n: valid(
            build(
                state=ints(n),
                handlers=[
                    {
                        "kind": "set",
                        "name": "h",
                        "assignments": [{"target": f"s{i}", "value": lit_int(1)} for i in range(n)],
                    }
                ],
            )
        ),
    ),
    c(
        "MAX_HANDLER_ITEMS",
        "sandbox inputs",
        lambda n: valid(
            with_handler(
                {
                    "kind": "sandbox",
                    "name": "h",
                    "module": "m",
                    "function": "f",
                    "inputs": [f"s{i}" for i in range(n)],
                    "outputs": ["s0"],
                },
                *ints(n),
            )
        ),
    ),
    c(
        "MAX_HANDLER_ITEMS",
        "sandbox outputs",
        lambda n: valid(
            with_handler(
                {
                    "kind": "sandbox",
                    "name": "h",
                    "module": "m",
                    "function": "f",
                    "outputs": [f"s{i}" for i in range(n)],
                },
                *ints(n),
            )
        ),
    ),
    c(
        "MAX_DEBOUNCE_MS",
        "debounce",
        lambda n: valid(
            with_handler(
                {
                    "kind": "sandbox",
                    "name": "h",
                    "module": "m",
                    "function": "f",
                    "outputs": ["s0"],
                    "debounce_ms": n,
                },
                *ints(1),
            )
        ),
    ),
    c(
        "MAX_EXPR_DEPTH",
        "expression depth",
        lambda n: valid(
            one_state({"kind": "bool", "key": "x", "default": True, "computed": chain(n)})
        ),
    ),
    c(
        "MAX_EXPR_NODES",
        "nodes in one expression",
        lambda n: valid(build(state=[computed_int("x", tree(n))])),
    ),
    c(
        "MAX_EXPR_ARGS",
        "operands",
        lambda n: valid(
            one_state(
                {
                    "kind": "string",
                    "key": "x",
                    "default": "",
                    "computed": {"kind": "concat", "args": [lit_str("a")] * n},
                }
            )
        ),
    ),
    c(
        "MAX_TOTAL_EXPR_NODES",
        "nodes in all expressions",
        lambda n: valid(
            build(
                state=[
                    computed_int(f"c{i}", tree(min(MAX_EXPR_NODES, n - i * MAX_EXPR_NODES)))
                    for i in range(-(-n // MAX_EXPR_NODES))
                ]
            )
        ),
    ),
    c(
        "MAX_DECIMALS",
        "format_number decimals",
        lambda n: valid(
            one_state(
                {
                    "kind": "string",
                    "key": "x",
                    "default": "",
                    "computed": {"kind": "format_number", "operand": lit_int(1), "decimals": n},
                }
            )
        ),
    ),
    c("MAX_COMPONENT_DEPTH", "nesting", lambda n: valid(build(nested_boxes(n)))),
    c("MAX_COMPONENTS", "components", lambda n: valid(build(components(n)))),
    c(
        "MAX_CONTAINER_CHILDREN",
        "box children",
        lambda n: valid(build({"kind": "box", "children": [{"kind": "separator"}] * n})),
    ),
    c(
        "MAX_CONTAINER_CHILDREN",
        "grid children",
        lambda n: valid(
            build({"kind": "grid", "columns": 2, "children": [{"kind": "separator"}] * n})
        ),
    ),
    c(
        "MAX_CONTAINER_CHILDREN",
        "tab pages",
        lambda n: valid(
            build({"kind": "tabs", "pages": [{"title": "t", "content": {"kind": "box"}}] * n})
        ),
    ),
    c("MAX_GRID_COLUMNS", "grid columns", lambda n: valid(build({"kind": "grid", "columns": n}))),
    c("MAX_SPACING", "box spacing", lambda n: valid(build({"kind": "box", "spacing": n}))),
    c(
        "MAX_OPTION_LENGTH",
        "enum option",
        lambda n: valid(
            one_state({"kind": "enum", "key": "x", "options": ["o" * n], "default": "o" * n})
        ),
    ),
    c(
        "MAX_ENUM_OPTIONS",
        "enum options",
        lambda n: valid(
            one_state(
                {
                    "kind": "enum",
                    "key": "x",
                    "options": [f"o{i}" for i in range(n)],
                    "default": "o0",
                }
            )
        ),
    ),
    c(
        "MAX_STRING_LENGTH",
        "declared string length",
        lambda n: valid(one_state({"kind": "string", "key": "x", "default": "", "max_length": n})),
    ),
    c(
        "MAX_STRING_LENGTH",
        "a string value at the largest declared length",
        lambda n: check_decl_value(
            "string", "x" * n, default="", max_length=min(n, limits.MAX_STRING_LENGTH)
        ),
    ),
    c(
        "MAX_CELL_LENGTH",
        "table cell",
        lambda n: Table.model_validate(
            {"columns": [{"name": "a", "type": "string"}], "rows": [["x" * n]]}
        ),
    ),
    c(
        "MAX_CELL_LENGTH",
        "column name",
        lambda n: Table.model_validate({"columns": [{"name": "c" * n, "type": "int"}], "rows": []}),
    ),
    c(
        "MAX_CELL_LENGTH",
        "list item",
        lambda n: check_decl_value("list", ["x" * n], item_type="string"),
    ),
    c(
        "MAX_LIST_ITEMS",
        "declared item count",
        lambda n: valid(
            one_state({"kind": "list", "key": "x", "item_type": "int", "max_items": n})
        ),
    ),
    c(
        "MAX_LIST_ITEMS",
        "items of a value",
        lambda n: check_decl_value(
            "list", [0] * n, item_type="int", max_items=min(n, limits.MAX_LIST_ITEMS)
        ),
    ),
    c(
        "MAX_TABLE_COLUMNS",
        "columns",
        lambda n: Table.model_validate(table_value(n, 1)),
    ),
    c(
        "MAX_TABLE_ROWS",
        "declared row count",
        lambda n: valid(one_state({"kind": "table", "key": "x", "max_rows": n})),
    ),
    c("MAX_TABLE_ROWS", "rows of a value", lambda n: Table.model_validate(table_value(1, n))),
    c("MAX_TABLE_CELLS", "cells", lambda n: Table.model_validate(table_of_cells(n))),
    c(
        "MAX_JSON_DEPTH",
        "depth",
        lambda n: valid(one_state({"kind": "json", "key": "x", "default": nested_list(n)})),
    ),
    c(
        "MAX_JSON_NODES",
        "nodes",
        lambda n: valid(one_state({"kind": "json", "key": "x", "default": [0] * (n - 1)})),
    ),
    c(
        "MAX_SAFE_INT",
        "integer literal",
        lambda n: valid(
            one_state({"kind": "int", "key": "x", "default": 0, "computed": lit_int(n)})
        ),
    ),
    c(
        "MAX_SAFE_INT",
        "int default",
        lambda n: valid(one_state({"kind": "int", "key": "x", "default": n})),
    ),
    c(
        "MAX_SAFE_INT",
        "negative int default",
        lambda n: valid(one_state({"kind": "int", "key": "x", "default": -n})),
    ),
]


def test_every_limit_has_a_boundary_test() -> None:
    assert {b.limit for b in BOUNDARIES} == set(all_limits())
    assert all(limit.startswith("MAX_") for limit in all_limits())
    assert len(all_limits()) >= 30


@pytest.mark.parametrize("count", [1, 2, 3, 64, 65, 66, 100, 511, 512, 513, 2000])
def test_the_component_builder_builds_exactly_that_many(count: int) -> None:
    assert count_components(components(count)) == count


def test_the_limits_are_the_documented_ones() -> None:
    assert limits.MAX_EXPR_DEPTH == 16  # ARCHITECTURE section 4: depth <= 16
    assert limits.MAX_DECIMALS == 10  # decimals 0-10
    assert MAX_SAFE_INT == 2**53 - 1
    assert MAX_COMPONENT_DEPTH == 16
    assert MAX_EXPR_NODES == 256


@pytest.mark.parametrize("boundary", BOUNDARIES, ids=lambda b: f"{b.limit}:{b.what}")
class TestBoundary:
    def test_exactly_the_limit_is_accepted(self, boundary: Boundary) -> None:
        boundary.check(all_limits()[boundary.limit])

    def test_one_past_the_limit_is_refused(self, boundary: Boundary) -> None:
        with pytest.raises(REFUSED):
            boundary.check(all_limits()[boundary.limit] + 1)
