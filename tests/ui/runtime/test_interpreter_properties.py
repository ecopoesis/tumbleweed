"""Properties of the interpreter over generated, well-typed expressions.

The type checker (`check_expr`) says what type every expression has; the interpreter must give a
value of exactly that type, finite, within range, for any state, and never raise. That one property
covers the promotions (`div` is a float, `round` is an int, an `if` of an int and a float is a
float, `min` of mixed numbers is a float) for every shape the generator can reach.
"""

import math
from collections.abc import Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

from tests.ui.runtime.support import EXPR
from tests.ui.spec.support import (
    Json,
    binary,
    lit_bool,
    lit_float,
    lit_int,
    lit_str,
    ref,
    variadic,
)
from tumbleweed.ui.runtime import evaluate
from tumbleweed.ui.runtime.values import MAX_FLOAT
from tumbleweed.ui.spec import check_expr
from tumbleweed.ui.spec.limits import MAX_SAFE_INT, MAX_STRING_LENGTH, MAX_TEXT_LENGTH
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import StateDecl

DECLS: dict[StateKey, StateDecl] = {
    StateKey(key): TypeAdapter(StateDecl).validate_python({"key": key, "default": default, **rest})
    for key, default, rest in (
        ("n", 0, {"kind": "int"}),
        ("f", 0.0, {"kind": "float"}),
        ("b", False, {"kind": "bool"}),
        ("s", "", {"kind": "string", "max_length": MAX_STRING_LENGTH}),
    )
}

INTS = st.one_of(
    st.integers(-MAX_SAFE_INT, MAX_SAFE_INT),
    st.sampled_from([0, 1, -1, 2, MAX_SAFE_INT, -MAX_SAFE_INT]),
)
FLOATS = st.one_of(
    st.floats(allow_nan=False, allow_infinity=False),
    st.sampled_from([0.0, -0.0, 0.5, -0.5, 1e308, -1e308, MAX_FLOAT, 5e-324, 1e-300]),
)
LITERALS = st.one_of(
    st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cf", "Cs", "Zl", "Zp")), max_size=20
    ),
    st.just("x" * MAX_TEXT_LENGTH),
)
STRINGS = st.one_of(st.text(max_size=20), st.just("x" * MAX_STRING_LENGTH))  # what state holds
STATES = st.fixed_dictionaries({"n": INTS, "f": FLOATS, "b": st.booleans(), "s": STRINGS})
DEPTH = 4  # at most 3 operands a node: 121 nodes, inside the limits of an expression


class Generator:
    """Draws well-typed expressions of each type, recursively, from one hypothesis data object."""

    def __init__(self, data: st.DataObject) -> None:
        self._data = data

    def _pick[T](self, options: list[T]) -> T:
        return self._data.draw(st.sampled_from(options))

    def _build(self, options: list[Callable[[], Json]]) -> Json:
        return self._data.draw(st.sampled_from(options))()

    def _leaf(self, kind: str) -> Json:
        draw = self._data.draw
        if kind == "int":
            return draw(st.one_of(INTS.map(lit_int), st.just(ref("n"))))
        if kind == "float":
            return draw(st.one_of(FLOATS.map(lit_float), st.just(ref("f"))))
        if kind == "bool":
            return draw(
                st.one_of(st.booleans().map(lambda v: lit_bool(value=v)), st.just(ref("b")))
            )
        return draw(st.one_of(LITERALS.map(lit_str), st.just(ref("s"))))

    def _number(self, depth: int) -> Json:
        return self.make(self._pick(["int", "float"]), depth)

    def _choose(self, depth: int, then: Json, otherwise: Json) -> Json:
        return {
            "kind": "if",
            "condition": self.make("bool", depth),
            "then": then,
            "otherwise": otherwise,
        }

    def make(self, kind: str, depth: int) -> Json:
        if depth == 0 or self._pick([False, True, True]) is False:
            return self._leaf(kind)
        below = depth - 1
        if kind == "int":
            return self._build(
                [
                    lambda: binary(
                        self._pick(["add", "sub", "mul"]),
                        self.make("int", below),
                        self.make("int", below),
                    ),
                    lambda: variadic(
                        self._pick(["min", "max"]), self.make("int", below), self.make("int", below)
                    ),
                    lambda: {"kind": "abs", "operand": self.make("int", below)},
                    lambda: {"kind": "round", "operand": self._number(below)},
                    lambda: self._choose(below, self.make("int", below), self.make("int", below)),
                ]
            )
        if kind == "float":
            # At least one float operand makes the result a float; the other is any number.
            return self._build(
                [
                    lambda: binary(
                        self._pick(["add", "sub", "mul"]),
                        self.make("float", below),
                        self._number(below),
                    ),
                    lambda: binary(
                        self._pick(["add", "sub", "mul"]),
                        self._number(below),
                        self.make("float", below),
                    ),
                    lambda: binary("div", self._number(below), self._number(below)),
                    lambda: variadic(
                        self._pick(["min", "max"]), self._number(below), self.make("float", below)
                    ),
                    lambda: {"kind": "abs", "operand": self.make("float", below)},
                    lambda: self._choose(below, self.make("float", below), self._number(below)),
                    lambda: self._choose(below, self._number(below), self.make("float", below)),
                ]
            )
        if kind == "bool":
            return self._build(
                [
                    lambda: binary(
                        self._pick(["eq", "ne", "lt", "le", "gt", "ge"]),
                        self._number(below),
                        self._number(below),
                    ),
                    lambda: binary(
                        self._pick(["eq", "ne"]),
                        self.make("string", below),
                        self.make("string", below),
                    ),
                    lambda: variadic(
                        self._pick(["and", "or"]),
                        self.make("bool", below),
                        self.make("bool", below),
                    ),
                    lambda: {"kind": "not", "operand": self.make("bool", below)},
                ]
            )
        return self._build(
            [
                lambda: variadic(
                    "concat",
                    self.make("string", below),
                    self.make("string", below),
                    self.make("string", below),
                ),
                lambda: {
                    "kind": "format_number",
                    "operand": self._number(below),
                    "decimals": self._data.draw(st.integers(0, 10)),
                    "grouping": self._data.draw(st.booleans()),
                },
                lambda: self._choose(below, self.make("string", below), self.make("string", below)),
            ]
        )


def run(kind: str, data: st.DataObject, state: dict[str, object]) -> None:
    expression = EXPR.validate_python(Generator(data).make(kind, DEPTH))
    assert check_expr(expression, DECLS) == kind  # the generator is well typed

    result = evaluate(expression, lambda key: state[key])

    if kind == "int":
        assert type(result) is int
        assert abs(result) <= MAX_SAFE_INT
    elif kind == "float":
        assert type(result) is float
        assert math.isfinite(result)
    elif kind == "bool":
        assert type(result) is bool
    else:
        assert type(result) is str
        assert len(result) <= MAX_STRING_LENGTH


@pytest.mark.parametrize("kind", ["int", "float", "bool", "string"])
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data(), state=STATES)
def test_the_value_has_the_type_the_checker_gave_the_expression(
    kind: str, data: st.DataObject, state: dict[str, object]
) -> None:
    run(kind, data, state)
