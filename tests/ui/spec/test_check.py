"""The type checker for expressions, and the dependency order of computed state."""

import pytest
from pydantic import TypeAdapter

from tests.ui.spec.support import (
    EXPR_KINDS,
    STATE_KINDS,
    Json,
    binary,
    lit_bool,
    lit_float,
    lit_int,
    lit_str,
    ref,
    variadic,
)
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.ui.spec.check import (
    ExprType,
    check_expr,
    computed_order,
    expr_type_of,
    referenced_keys,
    references,
    string_literals,
)
from tumbleweed.ui.spec.expr import Expr
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import StateDecl

EXPR = TypeAdapter(Expr)
DECL = TypeAdapter(StateDecl)

# One declaration of every kind of state, by the key an expression refers to it with.
DECLARATIONS: dict[str, Json] = {
    "n": {"kind": "int", "key": "n", "default": 1},
    "f": {"kind": "float", "key": "f", "default": 1.5},
    "s": {"kind": "string", "key": "s", "default": "x"},
    "b": {"kind": "bool", "key": "b", "default": True},
    "e": {"kind": "enum", "key": "e", "options": ["red", "green"], "default": "red"},
    "e2": {"kind": "enum", "key": "e2", "options": ["x", "y"], "default": "x"},
    "e3": {"kind": "enum", "key": "e3", "options": ["green", "blue"], "default": "blue"},
    "table": {"kind": "table", "key": "table"},
    "list": {"kind": "list", "key": "list", "item_type": "int"},
    "image": {"kind": "image", "key": "image"},
    "file": {"kind": "file", "key": "file"},
    "json": {"kind": "json", "key": "json"},
}
DECLS: dict[StateKey, StateDecl] = {
    StateKey(key): DECL.validate_python(decl) for key, decl in DECLARATIONS.items()
}
N, F, S, B, E = ref("n"), ref("f"), ref("s"), ref("b"), ref("e")
I1, F1, T, TRUE = lit_int(1), lit_float(1.5), lit_str("t"), lit_bool(value=True)


def choose(then: Json, otherwise: Json) -> Json:
    return {"kind": "if", "condition": B, "then": then, "otherwise": otherwise}


# Expressions that can only produce string literals (and one that cannot be known).
IF_OPTIONS = choose(lit_str("red"), lit_str("green"))
NESTED_IF_OPTIONS = choose(choose(lit_str("red"), lit_str("green")), lit_str("red"))
IF_UNKNOWN = choose(S, lit_str("red"))
IF_BAD = choose(lit_str("red"), lit_str("blue"))
NESTED_IF_BAD = choose(choose(lit_str("red"), lit_str("zzz")), lit_str("green"))


def type_of(data: Json) -> ExprType:
    return check_expr(EXPR.validate_python(data), DECLS)


def node(kind: str, **fields: Json | list[Json] | int | bool) -> Json:
    return {"kind": kind, **fields}  # type: ignore[dict-item]  # JSON values by construction


def numeric(kind: str) -> Json:
    return binary(kind, I1, I1)


OK: list[tuple[str, Json, ExprType]] = [
    ("bool literal", TRUE, "bool"),
    ("int literal", I1, "int"),
    ("float literal", F1, "float"),
    ("string literal", T, "string"),
    ("int ref", N, "int"),
    ("float ref", F, "float"),
    ("string ref", S, "string"),
    ("bool ref", B, "bool"),
    ("enum ref reads as a string", E, "string"),
    ("add int int", binary("add", N, I1), "int"),
    ("add int float", binary("add", N, F), "float"),
    ("add float int", binary("add", F, N), "float"),
    ("add float float", binary("add", F, F1), "float"),
    ("sub int int", binary("sub", N, I1), "int"),
    ("sub mixed", binary("sub", N, F), "float"),
    ("mul int int", binary("mul", N, I1), "int"),
    ("mul mixed", binary("mul", F, N), "float"),
    ("div int int is a float", binary("div", N, I1), "float"),
    ("div float", binary("div", F, F1), "float"),
    ("min ints", variadic("min", N, I1, lit_int(3)), "int"),
    ("min mixed", variadic("min", N, F), "float"),
    ("max ints", variadic("max", N, I1), "int"),
    ("max floats", variadic("max", F, F1), "float"),
    ("abs int", node("abs", operand=N), "int"),
    ("abs float", node("abs", operand=F), "float"),
    ("round float is an int", node("round", operand=F), "int"),
    ("round int", node("round", operand=N), "int"),
    ("eq numbers", binary("eq", N, F), "bool"),
    ("eq strings", binary("eq", S, T), "bool"),
    ("eq bools", binary("eq", B, TRUE), "bool"),
    ("ne numbers", binary("ne", N, I1), "bool"),
    ("ne strings", binary("ne", S, S), "bool"),
    ("ne bools", binary("ne", B, B), "bool"),
    ("eq enum with an option", binary("eq", E, lit_str("green")), "bool"),
    ("eq an enum with itself", binary("eq", E, E), "bool"),
    ("eq enums that share an option", binary("eq", E, ref("e3")), "bool"),
    ("ne enums that share an option", binary("ne", ref("e3"), E), "bool"),
    ("eq enum with an if of its options", binary("eq", E, IF_OPTIONS), "bool"),
    ("eq an if of its options with the enum", binary("eq", IF_OPTIONS, E), "bool"),
    ("eq enum with a nested if of its options", binary("eq", E, NESTED_IF_OPTIONS), "bool"),
    ("eq enum with an if that has a reference branch", binary("eq", E, IF_UNKNOWN), "bool"),
    ("ne an option with the enum", binary("ne", lit_str("red"), E), "bool"),
    (
        "eq enum with an if that has an enum branch sharing an option",
        binary("eq", E, choose(ref("e3"), lit_str("red"))),
        "bool",
    ),
    ("lt", binary("lt", N, F), "bool"),
    ("le", binary("le", N, I1), "bool"),
    ("gt", binary("gt", F, F1), "bool"),
    ("ge", binary("ge", N, N), "bool"),
    ("and", variadic("and", B, TRUE), "bool"),
    ("or", variadic("or", B, TRUE, B), "bool"),
    ("not", node("not", operand=B), "bool"),
    ("if int int", node("if", condition=B, then=N, otherwise=I1), "int"),
    ("if int float promotes", node("if", condition=B, then=N, otherwise=F), "float"),
    ("if float int promotes", node("if", condition=B, then=F, otherwise=N), "float"),
    ("if strings", node("if", condition=B, then=S, otherwise=T), "string"),
    ("if bools", node("if", condition=B, then=B, otherwise=TRUE), "bool"),
    ("concat", variadic("concat", S, T, E), "string"),
    ("format_number int", node("format_number", operand=N), "string"),
    ("format_number float", node("format_number", operand=F, decimals=2, grouping=True), "string"),
    ("nested", node("round", operand=binary("mul", binary("div", N, F), lit_int(100))), "int"),
]

# The same nodes with an operand of the wrong type, and what the checker says.
BAD: list[tuple[str, Json, str]] = [
    ("unknown key", ref("nope"), "unknown state key 'nope'"),
    ("a table", ref("table"), "table, which expressions cannot read"),
    ("a list", ref("list"), "list, which expressions cannot read"),
    ("an image", ref("image"), "image, which expressions cannot read"),
    ("a file", ref("file"), "file, which expressions cannot read"),
    ("json", ref("json"), "json, which expressions cannot read"),
    ("add a string", binary("add", N, S), "add: expected two numbers, got int and string"),
    ("add bools", binary("add", B, B), "add: expected two numbers, got bool and bool"),
    ("sub a string", binary("sub", S, N), "sub: expected two numbers, got string and int"),
    ("mul a bool", binary("mul", N, B), "mul: expected two numbers"),
    ("div a string", binary("div", T, I1), "div: expected two numbers"),
    ("min with a string", variadic("min", N, S), "min[1]: expected a number, got string"),
    ("max with a bool", variadic("max", B, N), "max[0]: expected a number, got bool"),
    ("abs a string", node("abs", operand=S), "abs.operand: expected a number, got string"),
    ("round a bool", node("round", operand=B), "round.operand: expected a number, got bool"),
    ("eq number with string", binary("eq", N, S), "eq: expected two numbers, two strings or two"),
    ("eq string with bool", binary("eq", S, B), "eq: expected two numbers, two strings or two"),
    ("ne bool with number", binary("ne", B, N), "ne: expected two numbers, two strings or two"),
    ("lt strings", binary("lt", S, T), "lt: expected two numbers, got string and string"),
    ("le bools", binary("le", B, B), "le: expected two numbers"),
    ("gt string and number", binary("gt", S, N), "gt: expected two numbers"),
    ("ge bool", binary("ge", N, B), "ge: expected two numbers"),
    ("and with a number", variadic("and", B, N), "and[1]: expected a bool, got int"),
    ("or with a string", variadic("or", S, B), "or[0]: expected a bool, got string"),
    ("not a number", node("not", operand=N), "not.operand: expected a bool, got int"),
    ("if on a number", node("if", condition=N, then=I1, otherwise=I1), "if.condition: expected"),
    ("if string and int", node("if", condition=B, then=S, otherwise=N), "if: the branches must"),
    ("if bool and string", node("if", condition=B, then=B, otherwise=S), "if: the branches must"),
    ("concat with a number", variadic("concat", S, N), "concat[1]: expected a string, got int"),
    ("concat with a bool", variadic("concat", B, S), "concat[0]: expected a string, got bool"),
    ("format a string", node("format_number", operand=S), "format_number.operand: expected a num"),
    ("a bad leaf deep inside", node("round", operand=binary("add", N, ref("nope"))), "nope"),
    ("enum compared with a non-option", binary("eq", E, lit_str("blue")), "'blue' is not an opt"),
    ("a non-option compared with the enum", binary("ne", lit_str("Red"), E), "'Red' is not an opt"),
    ("an if with a non-option branch", binary("eq", E, IF_BAD), "'blue' is not an option of e"),
    ("an if with a non-option, reversed", binary("ne", IF_BAD, E), "'blue' is not an option of e"),
    (
        "a nested if with a non-option",
        binary("eq", E, NESTED_IF_BAD),
        "'zzz' is not an option of e",
    ),
    ("enums with no option in common", binary("eq", E, ref("e2")), "e and e2 are enums with no"),
    (
        "an enum compared with an if that can give an enum with no option in common",
        binary("eq", E, choose(ref("e2"), lit_str("red"))),
        "e and e2 are enums with no",
    ),
    (
        "the same, the if on the left",
        binary("ne", choose(ref("e2"), lit_str("red")), E),
        "e2 and e are enums with no",
    ),
    (
        "two ifs, one branch of each from enums with no option in common",
        binary("eq", choose(E, lit_str("x")), choose(lit_str("y"), ref("e2"))),
        "e and e2 are enums with no",
    ),
    (
        "enums with no option in common, ne",
        binary("ne", ref("e2"), E),
        "e2 and e are enums with no",
    ),
]


@pytest.mark.parametrize(("data", "expected"), [c[1:] for c in OK], ids=[c[0] for c in OK])
def test_the_type_of_a_well_typed_expression(data: Json, expected: ExprType) -> None:
    assert type_of(data) == expected


@pytest.mark.parametrize(("data", "message"), [c[1:] for c in BAD], ids=[c[0] for c in BAD])
def test_an_ill_typed_expression_is_refused_naming_the_node(data: Json, message: str) -> None:
    with pytest.raises(InvalidValueError) as raised:
        type_of(data)

    assert message in str(raised.value)


def test_the_cases_cover_every_node_kind() -> None:
    """Both an accepting and a refusing case for every kind that has operands to get wrong."""

    def kinds_in(data: Json) -> set[str]:
        found = {str(data["kind"])}
        for value in data.values():
            if isinstance(value, dict):
                found |= kinds_in(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        found |= kinds_in(item)
        return found

    accepted = set().union(*(kinds_in(case[1]) for case in OK))
    refused = set().union(*(kinds_in(case[1]) for case in BAD))

    assert accepted == set(EXPR_KINDS)
    # Literals and refs have no operands to get wrong: a bad `ref` is the refusal for those.
    needs_a_refusal = set(EXPR_KINDS) - {"bool", "int", "float", "string"}
    assert needs_a_refusal <= refused


def test_a_float_is_never_an_int_and_a_number_never_a_string() -> None:
    assert type_of(node("round", operand=F)) == "int"
    assert type_of(binary("div", N, I1)) == "float"  # not int, even for two ints
    with pytest.raises(InvalidValueError):
        type_of(variadic("concat", S, lit_int(1)))


def test_every_state_kind_has_an_expression_type_or_none() -> None:
    types = {kind: expr_type_of(DECLS[StateKey(kind)]) for kind in DECLARATIONS}
    # An enum reads as a string; the kinds with no scalar value cannot be read at all.
    assert types == {
        "n": "int",
        "f": "float",
        "s": "string",
        "b": "bool",
        "e": "string",
        "e2": "string",
        "e3": "string",
        "table": None,
        "list": None,
        "image": None,
        "file": None,
        "json": None,
    }
    # ...and that is every kind of state there is.
    assert {decl.kind for decl in DECLS.values()} == set(STATE_KINDS)


class TestReferences:
    def test_lists_the_keys_read_in_order_of_first_appearance_once_each(self) -> None:
        data = node(
            "if",
            condition=binary("gt", ref("b"), ref("a")),
            then=binary("add", ref("a"), ref("c")),
            otherwise=variadic("min", ref("c"), ref("d"), ref("b")),
        )

        assert references(EXPR.validate_python(data)) == ["b", "a", "c", "d"]

    def test_a_literal_reads_nothing(self) -> None:
        assert references(EXPR.validate_python(I1)) == []


def decls_of(*entries: tuple[str, Json | None]) -> list[StateDecl]:
    """Int entries named in order; each `(key, computed)` computes from an expression or not."""
    declarations: list[Json] = []
    for key, expression in entries:
        decl: Json = {"kind": "int", "key": key, "default": 0}
        if expression is not None:
            decl["computed"] = expression
        declarations.append(decl)
    return [DECL.validate_python(d) for d in declarations]


def reads(*keys: str) -> Json:
    """An expression that reads each key (summed), or a literal when there are none."""
    expression: Json = lit_int(0)
    for key in keys:
        expression = binary("add", expression, ref(key))
    return expression


class TestComputedOrder:
    def test_no_computed_entries_no_order(self) -> None:
        assert computed_order(decls_of(("a", None), ("b", None))) == ()

    def test_a_dependency_comes_first_even_when_declared_last(self) -> None:
        order = computed_order(decls_of(("c", reads("b")), ("b", reads("a")), ("a", None)))

        assert order == ("b", "c")

    def test_independent_entries_keep_declaration_order(self) -> None:
        order = computed_order(decls_of(("z", reads()), ("a", reads()), ("m", reads())))

        assert order == ("z", "a", "m")

    def test_a_diamond_evaluates_each_entry_once_after_its_inputs(self) -> None:
        order = computed_order(
            decls_of(
                ("top", reads("left", "right")),
                ("left", reads("base")),
                ("right", reads("base")),
                ("base", reads("input")),
                ("input", None),
            )
        )

        assert sorted(order) == ["base", "left", "right", "top"]
        assert order.index("base") < order.index("left") < order.index("top")
        assert order.index("base") < order.index("right") < order.index("top")

    def test_plain_and_unknown_keys_are_leaves(self) -> None:
        assert computed_order(decls_of(("a", reads("plain", "nowhere")), ("plain", None))) == ("a",)

    def test_an_entry_that_reads_itself_is_a_cycle(self) -> None:
        with pytest.raises(InvalidValueError, match="computed state has a cycle: a -> a"):
            computed_order(decls_of(("a", reads("a"))))

    def test_a_two_cycle(self) -> None:
        with pytest.raises(InvalidValueError, match="cycle: a -> b -> a"):
            computed_order(decls_of(("a", reads("b")), ("b", reads("a"))))

    def test_the_cycle_named_is_the_cycle_not_the_path_that_led_to_it(self) -> None:
        entries = (("start", reads("x")), ("x", reads("y")), ("y", reads("z")), ("z", reads("x")))

        with pytest.raises(InvalidValueError, match="cycle: x -> y -> z -> x"):
            computed_order(decls_of(*entries))

    def test_a_cycle_is_found_wherever_it_hides(self) -> None:
        entries = (("ok", reads()), ("a", reads("ok", "b")), ("b", reads("c")), ("c", reads("a")))

        with pytest.raises(InvalidValueError, match="cycle: a -> b -> c -> a"):
            computed_order(decls_of(*entries))

    def test_a_long_chain_neither_recurses_nor_loops(self) -> None:
        count = 128
        chain = [(f"k{i}", reads(f"k{i + 1}") if i < count - 1 else reads()) for i in range(count)]

        order = computed_order(decls_of(*chain))

        assert order == tuple(f"k{i}" for i in reversed(range(count)))

    def test_a_long_cycle_is_found(self) -> None:
        count = 128
        entries = [(f"k{i}", reads(f"k{(i + 1) % count}")) for i in range(count)]

        with pytest.raises(InvalidValueError, match="computed state has a cycle"):
            computed_order(decls_of(*entries))


class TestWhatAnExpressionCanEvaluateToDirectly:
    """Literals and references reached through `if` branches: what an assignment is checked on."""

    def test_string_literals_through_nested_ifs(self) -> None:
        expression = EXPR.validate_python(choose(choose(lit_str("a"), lit_str("b")), lit_str("c")))

        assert sorted(string_literals(expression)) == ["a", "b", "c"]

    def test_references_through_nested_ifs(self) -> None:
        expression = EXPR.validate_python(choose(choose(ref("e"), ref("e2")), lit_str("c")))

        assert sorted(referenced_keys(expression)) == ["e", "e2"]

    def test_what_is_computed_is_not_known_before_it_runs(self) -> None:
        concatenated = EXPR.validate_python(variadic("concat", ref("s"), lit_str("x")))

        assert string_literals(concatenated) == []
        assert referenced_keys(concatenated) == []

    def test_a_plain_reference_or_literal_is_itself(self) -> None:
        assert referenced_keys(EXPR.validate_python(ref("e"))) == ["e"]
        assert string_literals(EXPR.validate_python(lit_str("t"))) == ["t"]
        assert string_literals(EXPR.validate_python(ref("e"))) == []
