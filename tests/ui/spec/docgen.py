"""Writes the parts of `docs/ui-spec.md` that restate the code, so that they cannot drift.

The reference has hand-written prose and generated regions:

    <!-- generated: limits -->
    ...rendered from the code...
    <!-- /generated -->

`tests/ui/spec/test_docs.py` fails when a region differs from what is rendered here, and
`uv run python -m tests.ui.spec.docgen --write` rewrites them. A region is one of:

- `limits`: every `MAX_*` limit with its value and meaning;
- `kinds expr|state|component|handler`: each `kind` of a union with the fields of its model
  (a `?` marks an optional field);
- `ports`: the port type of each kind of state;
- `fixture NAME`: the golden spec `tests/ui/spec/golden/NAME.json`, verbatim;
- `fragment NAME /json/pointer`: a part of a golden spec, pretty-printed.
"""

import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from re import Match
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from tests.ui.spec.support import (
    COMPONENT_KINDS,
    EXPR_KINDS,
    GOLDEN_DIR,
    HANDLER_KINDS,
    MINIMAL_STATE,
    STATE_KINDS,
    golden_text,
)
from tumbleweed.ui.spec.limits import all_limits
from tumbleweed.ui.spec.state import StateDecl, port_type_of
from tumbleweed.ui.spec.values import PORT_TYPES

if TYPE_CHECKING:
    from tumbleweed.core.types import JsonValue

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "ui-spec.md"
REGION = re.compile(
    r"<!-- generated: (?P<name>[^>]+?) -->\n(?P<body>.*?)<!-- /generated -->\n", re.DOTALL
)

DESCRIPTIONS = {
    "MAX_SPEC_BYTES": "bytes of the raw JSON text (`parse_ui_spec`)",
    "MAX_TITLE_LENGTH": "characters in the title",
    "MAX_NAME_LENGTH": "characters in a state key, handler, module or function name",
    "MAX_LINE_LENGTH": "characters in one-line text: button, check, tab and expander labels, "
    "placeholders, alt text",
    "MAX_TEXT_LENGTH": "characters in block text: label and heading text, string literals, "
    "agent notes",
    "MAX_STATE_ENTRIES": "state declarations",
    "MAX_HANDLERS": "handlers",
    "MAX_HANDLER_ITEMS": "assignments of a `set` handler; inputs, and outputs, of a `sandbox` "
    "handler",
    "MAX_DEBOUNCE_MS": "`debounce_ms` of a `sandbox` handler",
    "MAX_EXPR_DEPTH": "levels of one expression (a literal or a reference is level 1)",
    "MAX_EXPR_NODES": "nodes in one expression",
    "MAX_EXPR_ARGS": "operands of `min`, `max`, `and`, `or` and `concat`",
    "MAX_TOTAL_EXPR_NODES": "nodes in all the expressions of one spec",
    "MAX_DECIMALS": "`decimals` of `format_number`",
    "MAX_COMPONENT_DEPTH": "levels of the component tree (the root is level 1)",
    "MAX_COMPONENTS": "components in one spec",
    "MAX_CONTAINER_CHILDREN": "children of a `box` or `grid`, pages of `tabs`",
    "MAX_GRID_COLUMNS": "`columns` of a `grid`",
    "MAX_SPACING": "pixels of spacing",
    "MAX_OPTION_LENGTH": "characters in one enum option",
    "MAX_ENUM_OPTIONS": "options of an `enum`",
    "MAX_STRING_LENGTH": "characters a `string` entry may declare as `max_length`",
    "MAX_CELL_LENGTH": "characters in a table cell, a column name or a list item",
    "MAX_LIST_ITEMS": "items a `list` entry may declare as `max_items`",
    "MAX_TABLE_COLUMNS": "columns of a table",
    "MAX_TABLE_ROWS": "rows a `table` entry may declare as `max_rows`, and rows of a table value",
    "MAX_TABLE_CELLS": "rows times columns of a table value",
    "MAX_JSON_DEPTH": "levels of a `json` value",
    "MAX_JSON_NODES": "nodes of a `json` value (containers, scalars and keys)",
    "MAX_SAFE_INT": "magnitude of an integer anywhere in a spec (exact in canonical JSON)",
}


def render_limits() -> str:
    rows = ["| Constant | Value | Bounds |", "|---|---:|---|"]
    for name, value in all_limits().items():
        rows.append(f"| `{name}` | {value:,} | {DESCRIPTIONS[name]} |")
    return "\n".join(rows) + "\n"


def render_kinds(union: str) -> str:
    registry = {
        "expr": EXPR_KINDS,
        "state": STATE_KINDS,
        "component": COMPONENT_KINDS,
        "handler": HANDLER_KINDS,
    }[union]
    rows = ["| `kind` | Fields (`?` is optional) |", "|---|---|"]
    for kind, cls in registry.items():
        fields = [
            f"`{name}{'' if field.is_required() else '?'}`"
            for name, field in cls.model_fields.items()
            if name != "kind"
        ]
        rows.append(f"| `{kind}` | {', '.join(fields) if fields else '(none)'} |")
    return "\n".join(rows) + "\n"


def render_ports() -> str:
    adapter = TypeAdapter(StateDecl)
    rows = ["| State kind | Port type |", "|---|---|"]
    for kind in STATE_KINDS:
        decl = adapter.validate_python({"kind": kind, "key": "k", **MINIMAL_STATE[kind]})
        port = port_type_of(decl)
        rows.append(f"| `{kind}` | {f'`{port}`' if port else 'none: local to the tool'} |")
    rows.append("")
    rows.append(f"The port types are {', '.join(f'`{port}`' for port in PORT_TYPES)}.")
    return "\n".join(rows) + "\n"


def render_fixture(name: str) -> str:
    return "```json\n" + golden_text(name).rstrip("\n") + "\n```\n"


def render_fragment(name: str, pointer: str) -> str:
    node: JsonValue = json.loads(golden_text(name))
    for part in pointer.strip("/").split("/"):
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict):
            node = node[part]
        else:
            raise ValueError(f"{pointer!r} goes below a scalar in {name}")
    return "```json\n" + json.dumps(node, indent=2) + "\n```\n"


def render(region: str) -> str:
    """The body of the region called `region`, or ValueError if there is no such region."""
    kind, _, argument = region.partition(" ")
    if kind == "limits" and not argument:
        return render_limits()
    if kind == "kinds" and argument in {"expr", "state", "component", "handler"}:
        return render_kinds(argument)
    if kind == "ports" and not argument:
        return render_ports()
    if kind == "fixture" and (GOLDEN_DIR / f"{argument}.json").is_file():
        return render_fixture(argument)
    if kind == "fragment":
        name, _, pointer = argument.partition(" ")
        return render_fragment(name, pointer)
    raise ValueError(f"unknown region {region!r}")


def regenerate(text: str) -> str:
    """`text` with every generated region rewritten from the code."""

    def rewrite(match: Match[str]) -> str:
        name = match["name"]
        return f"<!-- generated: {name} -->\n{render(name)}<!-- /generated -->\n"

    return REGION.sub(rewrite, text)


def main(argv: Sequence[str]) -> int:
    text = DOC.read_text(encoding="utf-8")
    updated = regenerate(text)
    if argv == ["--write"]:
        DOC.write_text(updated, encoding="utf-8")
        return 0
    if argv in ([], ["--check"]):
        return 0 if updated == text else 1
    sys.stderr.write("usage: python -m tests.ui.spec.docgen [--check | --write]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
