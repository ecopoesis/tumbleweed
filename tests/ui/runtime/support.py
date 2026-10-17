"""Helpers of the runtime tests: specs from state and handlers, expressions from JSON."""

from collections.abc import Mapping, Sequence

from pydantic import TypeAdapter

from tests.ui.spec.support import Json, build
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.runtime import Change, UiRuntime, Value, evaluate
from tumbleweed.ui.spec import UiSpec, validate_ui_spec
from tumbleweed.ui.spec.expr import Expr
from tumbleweed.ui.spec.names import StateKey

EXPR = TypeAdapter(Expr)


def spec_with(
    state: Sequence[Json] = (), handlers: Sequence[Json] = (), root: Json | None = None
) -> UiSpec:
    """A valid spec with this state and these handlers (an empty box unless `root` is given)."""
    return validate_ui_spec(build(root, state, handlers))


def runtime_with(
    state: Sequence[Json] = (), handlers: Sequence[Json] = (), root: Json | None = None
) -> UiRuntime:
    return UiRuntime(spec_with(state, handlers, root))


def value_of(expression: Json, state: Mapping[str, object] | None = None) -> Value:
    """What `expression` evaluates to when `state` is what a `ref` reads (no type checking)."""
    values = dict(state or {})
    return evaluate(EXPR.validate_python(expression), lambda key: values[key])


def number(key: str, default: int, **fields: JsonValue) -> Json:
    return {"kind": "int", "key": key, "default": default, **fields}


def real(key: str, default: float, **fields: JsonValue) -> Json:
    return {"kind": "float", "key": key, "default": default, **fields}


def text(key: str, default: str = "", **fields: JsonValue) -> Json:
    return {"kind": "string", "key": key, "default": default, **fields}


def flag(key: str, *, default: bool = False) -> Json:
    return {"kind": "bool", "key": key, "default": default}


def choice(key: str, options: Sequence[str], default: str) -> Json:
    names: list[JsonValue] = [*options]
    return {"kind": "enum", "key": key, "options": names, "default": default}


def set_handler(name: str, **assignments: Json) -> Json:
    """A `set` handler: `target=expression` for each assignment, in the order given."""
    listed: list[JsonValue] = [{"target": t, "value": v} for t, v in assignments.items()]
    return {"kind": "set", "name": name, "assignments": listed}


def recorded(runtime: UiRuntime) -> list[Change]:
    """A list that collects every change the runtime's store announces from now on."""
    changes: list[Change] = []
    runtime.store.subscribe(changes.append)
    return changes


def key(name: str) -> StateKey:
    return StateKey(name)
