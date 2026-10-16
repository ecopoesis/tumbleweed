"""Helpers shared by the UI spec tests: golden fixtures, minimal specs, and registries.

The registries (which kinds exist in each union) are read from the type aliases that define the
unions, not from `__subclasses__()` or from a list kept here, so that a new node kind that the
tests do not know about fails them instead of passing unnoticed.
"""

import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import get_args

from pydantic import BaseModel

from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec import components, expr, handlers, state
from tumbleweed.ui.spec.errors import SpecError
from tumbleweed.ui.spec.parse import validate_ui_spec
from tumbleweed.ui.spec.spec import UiSpec

GOLDEN_DIR = Path(__file__).parent / "golden"
type Json = dict[str, JsonValue]


def golden_text(name: str) -> str:
    return (GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")


def golden(name: str) -> Json:
    """A fresh, mutable copy of a golden spec as parsed JSON."""
    data = json.loads(golden_text(name))
    assert isinstance(data, dict)
    return data


def union_members(alias: object) -> tuple[type[BaseModel], ...]:
    """The classes of a discriminated union written as `type X = Annotated[A | B | ..., Field]`."""
    annotated = alias.__value__  # type: ignore[attr-defined]  # a PEP 695 TypeAliasType
    union = get_args(annotated)[0]
    return get_args(union)


def kind_of(cls: type[BaseModel]) -> str:
    """The `kind` tag of a model class, read from its `Literal` annotation."""
    annotation = cls.model_fields["kind"].annotation
    (tag,) = get_args(annotation)
    assert isinstance(tag, str)
    return tag


def kinds(alias: object) -> dict[str, type[BaseModel]]:
    return {kind_of(cls): cls for cls in union_members(alias)}


EXPR_KINDS = kinds(expr.Expr)
STATE_KINDS = kinds(state.StateDecl)
COMPONENT_KINDS = kinds(components.Component)
HANDLER_KINDS = kinds(handlers.Handler)


# A valid declaration of every kind of state, as a model would write it (defaults omitted).
MINIMAL_STATE: dict[str, Json] = {
    "bool": {"default": True},
    "int": {"default": 5},
    "float": {"default": 1.5},
    "string": {"default": "x"},
    "enum": {"options": ["a", "b"], "default": "a"},
    "image": {},
    "file": {},
    "table": {},
    "list": {"item_type": "int"},
    "json": {},
}


def minimal(**fields: JsonValue) -> Json:
    """The smallest valid spec, with `fields` replacing or adding top-level fields."""
    base: Json = {
        "version": "tumbleweed.ui/1",
        "title": "Test",
        "root": {"kind": "box"},
    }
    base.update(fields)
    return copy.deepcopy(base)


def spec_of(**fields: JsonValue) -> UiSpec:
    return validate_ui_spec(minimal(**fields))


def build(
    root: Json | None = None,
    state: Sequence[Json] = (),
    handlers: Sequence[Json] = (),
    **fields: JsonValue,
) -> Json:
    """A spec as JSON: the empty box (or `root`), with these state entries and handlers."""
    data = minimal(**fields)
    if root is not None:
        data["root"] = root
    data["state"] = [*state]
    data["handlers"] = [*handlers]
    return data


def accepted(data: Json) -> UiSpec:
    """Validate `data` (a SpecError fails the test) and return the spec."""
    return validate_ui_spec(data)


def refused(data: Json) -> list[str]:
    """The problems validating `data` reports; fails the test if it is accepted."""
    try:
        validate_ui_spec(data)
    except SpecError as error:
        return list(error.problems)
    raise AssertionError("the spec was accepted")


def mentions(problems: Sequence[str], *fragments: str) -> bool:
    """Whether one problem contains every fragment."""
    return any(all(fragment in problem for fragment in fragments) for problem in problems)


# Small builders for expression JSON, so that cases read as the expressions they are.
def lit_int(value: int) -> Json:
    return {"kind": "int", "value": value}


def lit_float(value: float) -> Json:
    return {"kind": "float", "value": value}


def lit_bool(*, value: bool) -> Json:
    return {"kind": "bool", "value": value}


def lit_str(value: str) -> Json:
    return {"kind": "string", "value": value}


def ref(key: str) -> Json:
    return {"kind": "ref", "key": key}


def binary(kind: str, left: Json, right: Json) -> Json:
    return {"kind": kind, "left": left, "right": right}


def variadic(kind: str, *args: Json) -> Json:
    operands: list[JsonValue] = [*args]
    return {"kind": kind, "args": operands}


def computed(key: str, kind: str, expression: Json, default: JsonValue) -> Json:
    return {"kind": kind, "key": key, "default": default, "computed": expression}


SEPARATOR: Json = {"kind": "separator"}

# component JSON, the kinds of state it binds, whether it writes to it, whether it needs bounds
BOUND: dict[str, tuple[Json, set[str], bool, bool]] = {
    "label": ({"kind": "label", "bind": "x"}, {"string", "enum"}, False, False),
    "heading": ({"kind": "heading", "bind": "x"}, {"string", "enum"}, False, False),
    "progress": ({"kind": "progress", "bind": "x"}, {"int", "float"}, False, True),
    "image": ({"kind": "image", "bind": "x"}, {"image"}, False, False),
    "table": ({"kind": "table", "bind": "x"}, {"table"}, False, False),
    "entry": ({"kind": "entry", "bind": "x"}, {"string"}, True, False),
    "switch": ({"kind": "switch", "bind": "x"}, {"bool"}, True, False),
    "check": ({"kind": "check", "label": "L", "bind": "x"}, {"bool"}, True, False),
    "slider": ({"kind": "slider", "bind": "x"}, {"int", "float"}, True, True),
    "spin": ({"kind": "spin", "bind": "x"}, {"int", "float"}, True, True),
    "dropdown": ({"kind": "dropdown", "bind": "x"}, {"enum"}, True, False),
    "text_view": ({"kind": "text_view", "bind": "x"}, {"string"}, False, False),
}
UNBOUND: dict[str, Json] = {
    "box": {"kind": "box"},
    "grid": {"kind": "grid", "columns": 2},
    "separator": SEPARATOR,
    "button": {"kind": "button", "label": "Go"},
    "tabs": {"kind": "tabs", "pages": [{"title": "T", "content": {"kind": "box"}}]},
    "expander": {"kind": "expander", "title": "E", "child": {"kind": "box"}},
}
UNBOUNDED_NUMBERS: list[Json] = [
    {"kind": "int", "key": "x", "default": 5},
    {"kind": "int", "key": "x", "default": 5, "minimum": 0},
    {"kind": "int", "key": "x", "default": 5, "maximum": 10},
    {"kind": "float", "key": "x", "default": 0.5},
    {"kind": "float", "key": "x", "default": 0.5, "minimum": 0},
    {"kind": "float", "key": "x", "default": 0.5, "maximum": 1},
]


# Which events each component has (every `on_*` field).
EVENTS = {
    "button": ("on_click",),
    "entry": ("on_change", "on_activate"),
    "switch": ("on_change",),
    "check": ("on_change",),
    "slider": ("on_change",),
    "spin": ("on_change",),
    "dropdown": ("on_change",),
    "text_view": ("on_change",),
}
