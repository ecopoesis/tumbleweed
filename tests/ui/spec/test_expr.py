"""The expression AST: a closed set of typed nodes, with no way to say "evaluate this string"."""

import builtins
import json
import typing
from typing import NoReturn

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.ui.spec.support import (
    EXPR_KINDS,
    Json,
    binary,
    lit_bool,
    lit_float,
    lit_int,
    lit_str,
    minimal,
    ref,
    variadic,
)
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec import expr
from tumbleweed.ui.spec.errors import SpecError
from tumbleweed.ui.spec.expr import (
    Abs,
    Add,
    And,
    BoolLit,
    Concat,
    Expr,
    ExprNode,
    FloatLit,
    FormatNumber,
    If,
    IntLit,
    Max,
    Min,
    Not,
    Ref,
    Round,
    StrLit,
    measure,
)
from tumbleweed.ui.spec.limits import (
    MAX_DECIMALS,
    MAX_EXPR_ARGS,
    MAX_EXPR_DEPTH,
    MAX_EXPR_NODES,
    MAX_SAFE_INT,
    MAX_TEXT_LENGTH,
)
from tumbleweed.ui.spec.parse import parse_ui_spec, validate_ui_spec

EXPR = TypeAdapter(Expr)


def parse(data: Json) -> ExprNode:
    node = EXPR.validate_python(data)
    assert isinstance(node, ExprNode)
    return node


# One valid example of every node kind. The test below makes the set complete: a new kind in the
# union without a sample here fails it.
SAMPLES: dict[str, Json] = {
    "bool": lit_bool(value=True),
    "int": lit_int(3),
    "float": lit_float(2.5),
    "string": lit_str("text"),
    "ref": ref("x"),
    "add": binary("add", lit_int(1), lit_int(2)),
    "sub": binary("sub", lit_int(1), lit_int(2)),
    "mul": binary("mul", lit_int(1), lit_int(2)),
    "div": binary("div", lit_int(1), lit_int(2)),
    "min": variadic("min", lit_int(1), lit_int(2)),
    "max": variadic("max", lit_int(1), lit_int(2)),
    "abs": {"kind": "abs", "operand": lit_int(-1)},
    "round": {"kind": "round", "operand": lit_float(1.5)},
    "eq": binary("eq", lit_int(1), lit_int(2)),
    "ne": binary("ne", lit_int(1), lit_int(2)),
    "lt": binary("lt", lit_int(1), lit_int(2)),
    "le": binary("le", lit_int(1), lit_int(2)),
    "gt": binary("gt", lit_int(1), lit_int(2)),
    "ge": binary("ge", lit_int(1), lit_int(2)),
    "and": variadic("and", lit_bool(value=True), lit_bool(value=False)),
    "or": variadic("or", lit_bool(value=True), lit_bool(value=False)),
    "not": {"kind": "not", "operand": lit_bool(value=True)},
    "if": {
        "kind": "if",
        "condition": lit_bool(value=True),
        "then": lit_int(1),
        "otherwise": lit_int(2),
    },
    "concat": variadic("concat", lit_str("a"), lit_str("b")),
    "format_number": {"kind": "format_number", "operand": lit_int(1), "decimals": 2},
}


def test_the_samples_cover_every_node_kind_of_the_union() -> None:
    assert set(SAMPLES) == set(EXPR_KINDS)
    assert len(EXPR_KINDS) == 25  # the 25 node kinds of ARCHITECTURE section 4 and the plan


@pytest.mark.parametrize("kind", EXPR_KINDS)
class TestEveryNodeKind:
    def test_is_parsed_into_its_class_and_dumps_back(self, kind: str) -> None:
        node = EXPR.validate_json(json.dumps(SAMPLES[kind]))

        assert type(node) is EXPR_KINDS[kind]
        assert json.loads(EXPR.dump_json(node)) == {
            **SAMPLES[kind],
            **json.loads(EXPR.dump_json(node)),
        }
        assert EXPR.validate_json(EXPR.dump_json(node)) == node

    def test_is_final_immutable_and_closed(self, kind: str) -> None:
        cls = EXPR_KINDS[kind]
        node = parse(SAMPLES[kind])

        assert cls.__dict__.get("__final__", False)
        assert issubclass(cls, ExprNode)
        with pytest.raises(ValidationError, match="frozen"):
            setattr(node, "kind", kind)  # noqa: B010  (the assignment is the point of the test)
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            EXPR.validate_python({**SAMPLES[kind], "extra": 1})

    def test_needs_its_kind_tag(self, kind: str) -> None:
        untagged = {key: value for key, value in SAMPLES[kind].items() if key != "kind"}

        with pytest.raises(ValidationError, match="Unable to extract tag"):
            EXPR.validate_python(untagged)

    def test_is_not_a_string_that_means_something(self, kind: str) -> None:
        with pytest.raises(ValidationError):
            EXPR.validate_python(kind)


class TestTheSetIsClosed:
    @pytest.mark.parametrize(
        "node",
        [
            {"kind": "call", "name": "os.system", "args": ["rm -rf /"]},
            {"kind": "python", "source": "__import__('os').system('id')"},
            {"kind": "eval", "operand": {"kind": "string", "value": "1+1"}},
            {"kind": "attr", "operand": {"kind": "ref", "key": "x"}, "name": "__class__"},
            {"kind": "lambda", "body": {"kind": "int", "value": 1}},
            {"kind": "format", "template": "%s", "args": []},
            {"kind": "Add", "left": SAMPLES["int"], "right": SAMPLES["int"]},
            {"kind": ["add"], "left": SAMPLES["int"], "right": SAMPLES["int"]},
            {"kind": None},
            {"kind": 5},
            {},
            [],
            None,
            5,
        ],
        ids=lambda n: json.dumps(n)[:40],
    )
    def test_unknown_node_kinds_are_refused(self, node: object) -> None:
        with pytest.raises(ValidationError):
            EXPR.validate_python(node)

    def test_the_kinds_are_exactly_these(self) -> None:
        assert set(EXPR_KINDS) == {
            "bool", "int", "float", "string", "ref",
            "add", "sub", "mul", "div", "min", "max", "abs", "round",
            "eq", "ne", "lt", "le", "gt", "ge", "and", "or", "not", "if",
            "concat", "format_number",
        }  # fmt: skip


class TestAStringIsNeverParsed:
    """I1: a spec holds expression trees, never expression text. A string is an error."""

    HOSTILE: typing.ClassVar[list[str]] = [
        "1 + 1",
        "bill * tip / 100",
        "__import__('os').system('touch /tmp/pwned')",
        "(lambda: 1)()",
        "{{ 7 * 7 }}",
        "${bill}",
        "%(bill)s",
        "eval('1')",
    ]

    @pytest.mark.parametrize("text", HOSTILE)
    def test_a_string_where_an_expression_goes_is_a_type_error(self, text: str) -> None:
        # The three places an expression can appear in a spec, plus an operand inside one.
        specs: list[Json] = [
            minimal(state=[{"kind": "int", "key": "x", "default": 0, "computed": text}]),
            minimal(
                state=[{"kind": "int", "key": "x", "default": 0}],
                handlers=[
                    {
                        "kind": "set",
                        "name": "h",
                        "assignments": [{"target": "x", "value": text}],
                    }
                ],
            ),
            minimal(
                state=[
                    {
                        "kind": "int",
                        "key": "x",
                        "default": 0,
                        "computed": {"kind": "add", "left": text, "right": lit_int(1)},
                    }
                ]
            ),
        ]
        for data in specs:
            with pytest.raises(SpecError, match="object"):
                validate_ui_spec(data)
            with pytest.raises(SpecError):
                parse_ui_spec(json.dumps(data))

    @pytest.mark.parametrize("text", HOSTILE)
    def test_nothing_evaluates_or_compiles_while_a_spec_is_validated(
        self, text: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        class Trap:
            def __init__(self, name: str) -> None:
                self.name = name

            def __call__(self, *args: object, **kwargs: object) -> NoReturn:
                calls.append(self.name)
                raise AssertionError(f"{self.name} was called while validating a spec")

        for name in ("eval", "exec", "compile", "__import__"):
            monkeypatch.setattr(builtins, name, Trap(name))
        data = minimal(
            state=[
                {"kind": "string", "key": "s", "default": text},
                {"kind": "string", "key": "t", "default": "", "computed": lit_str(text)},
            ],
            root={"kind": "label", "text": text},
        )

        spec = validate_ui_spec(data)
        hostile = minimal(state=[{"kind": "int", "key": "x", "default": 0, "computed": text}])
        with pytest.raises(SpecError):
            validate_ui_spec(hostile)

        assert spec.title == "Test"
        assert calls == []

    def test_a_string_literal_is_text_and_stays_text(self) -> None:
        node = EXPR.validate_python(lit_str("1 + 1"))

        assert isinstance(node, StrLit)
        assert node.value == "1 + 1"


class TestLiterals:
    def test_an_int_is_not_written_as_a_float(self) -> None:
        for bad in (5.0, "5", True, None, 2**53, -(2**53)):
            with pytest.raises(ValidationError):
                EXPR.validate_python({"kind": "int", "value": bad})
        with pytest.raises(ValidationError):
            EXPR.validate_json('{"kind": "int", "value": 5.0}')
        assert EXPR.validate_python(lit_int(MAX_SAFE_INT)) == IntLit(kind="int", value=MAX_SAFE_INT)

    def test_a_float_may_be_written_as_an_int_and_is_a_float(self) -> None:
        node = EXPR.validate_json('{"kind": "float", "value": 5}')

        assert isinstance(node, FloatLit)
        assert type(node.value) is float
        assert node.value == 5.0

    @pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e999"])
    def test_a_float_is_finite(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            EXPR.validate_json(f'{{"kind": "float", "value": {raw}}}')

    def test_a_bool_is_a_bool(self) -> None:
        for bad in (1, 0, "true", None):
            with pytest.raises(ValidationError):
                EXPR.validate_python({"kind": "bool", "value": bad})
        assert EXPR.validate_python(lit_bool(value=False)) == BoolLit(kind="bool", value=False)

    def test_string_literals_are_authored_block_text_of_bounded_length(self) -> None:
        parse(lit_str("line\nbreak\ttab"))
        parse(lit_str("x" * MAX_TEXT_LENGTH))
        for bad in ("x" * (MAX_TEXT_LENGTH + 1), "a\0b", "a\u202eb", "a\r\nb"):
            with pytest.raises(ValidationError):
                EXPR.validate_python(lit_str(bad))

    @pytest.mark.parametrize("key", ["", "Bill", "a b", "a-b", "x" * 65, "_x", "1x"])
    def test_a_reference_names_a_valid_state_key(self, key: str) -> None:
        with pytest.raises(ValidationError):
            EXPR.validate_python(ref(key))


class TestOperands:
    @pytest.mark.parametrize("kind", ["min", "max", "and", "or", "concat"])
    def test_variadic_nodes_take_two_to_the_limit(self, kind: str) -> None:
        operand = lit_bool(value=True) if kind in {"and", "or"} else lit_str("a")
        operand = lit_int(1) if kind in {"min", "max"} else operand

        parse(variadic(kind, *[operand] * 2))
        parse(variadic(kind, *[operand] * MAX_EXPR_ARGS))
        for count in (0, 1, MAX_EXPR_ARGS + 1):
            with pytest.raises(ValidationError, match=r"at (least|most) "):
                EXPR.validate_python(variadic(kind, *[operand] * count))

    def test_operands_come_in_written_order(self) -> None:
        node = EXPR.validate_python(SAMPLES["if"])

        assert isinstance(node, If)
        assert [type(o) for o in node.operands()] == [BoolLit, IntLit, IntLit]
        values = [o.value for o in node.operands()[1:] if isinstance(o, IntLit)]
        assert values == [1, 2]

    def test_a_leaf_has_none(self) -> None:
        for kind in ("bool", "int", "float", "string", "ref"):
            assert parse(SAMPLES[kind]).operands() == ()

    def test_a_list_is_accepted_for_args_and_held_as_a_tuple(self) -> None:
        node = EXPR.validate_python(SAMPLES["min"])

        assert isinstance(node, Min)
        assert isinstance(node.args, tuple)


class TestFormatNumber:
    """Number formatting takes bounded parameters, never a format string."""

    def test_defaults_are_no_decimals_and_no_grouping(self) -> None:
        node = EXPR.validate_python({"kind": "format_number", "operand": lit_int(1)})

        assert isinstance(node, FormatNumber)
        assert (node.decimals, node.grouping) == (0, False)

    @pytest.mark.parametrize("decimals", [0, 1, MAX_DECIMALS])
    def test_decimals_up_to_the_limit(self, decimals: int) -> None:
        node = EXPR.validate_python(
            {"kind": "format_number", "operand": lit_int(1), "decimals": decimals}
        )

        assert isinstance(node, FormatNumber)
        assert node.decimals == decimals

    @pytest.mark.parametrize("decimals", [-1, MAX_DECIMALS + 1, 100, 2.0, 2.5, "2", True, None])
    def test_nothing_else_is_a_count_of_decimals(self, decimals: JsonValue) -> None:
        with pytest.raises(ValidationError):
            EXPR.validate_python(
                {"kind": "format_number", "operand": lit_int(1), "decimals": decimals}
            )

    @pytest.mark.parametrize("grouping", [1, "true", None, 0])
    def test_grouping_is_a_bool(self, grouping: JsonValue) -> None:
        with pytest.raises(ValidationError):
            EXPR.validate_python(
                {"kind": "format_number", "operand": lit_int(1), "grouping": grouping}
            )

    @pytest.mark.parametrize(
        "field", ["format", "pattern", "template", "locale", "fmt", "spec", "separator"]
    )
    def test_there_is_no_field_for_a_format_string_or_a_locale(self, field: str) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            EXPR.validate_python({"kind": "format_number", "operand": lit_int(1), field: "%.2f"})

    def test_the_node_has_only_these_parameters(self) -> None:
        assert set(FormatNumber.model_fields) == {"kind", "operand", "decimals", "grouping"}


def chain(depth: int) -> Json:
    """An expression of exactly `depth` levels: `depth - 1` nots around one literal."""
    node = lit_bool(value=True)
    for _ in range(depth - 1):
        node = {"kind": "not", "operand": node}
    return node


def tree(nodes: int) -> Json:
    """An expression of exactly `nodes` nodes (at least 1), built balanced so it stays shallow."""
    if nodes == 1:
        return lit_int(1)
    if nodes == 2:
        return {"kind": "abs", "operand": lit_int(1)}
    left = (nodes - 1) // 2
    return binary("add", tree(left), tree(nodes - 1 - left))


class TestDepth:
    def test_depth_is_counted_from_the_leaf(self) -> None:
        assert measure(parse(lit_int(1))) == (1, 1)
        assert measure(parse(SAMPLES["add"]))[0] == 2
        assert measure(parse(chain(5)))[0] == 5

    def test_the_limit_is_sixteen_and_a_seventeenth_level_is_refused(self) -> None:
        assert MAX_EXPR_DEPTH == 16
        parse(chain(16))
        with pytest.raises(ValidationError, match="nested 17 deep, the limit is 16"):
            EXPR.validate_python(chain(17))

    def test_the_depth_of_the_deepest_branch_counts(self) -> None:
        wide = variadic("and", chain(16), lit_bool(value=True), chain(3))
        with pytest.raises(ValidationError, match="the limit is 16"):
            EXPR.validate_python(wide)
        parse(variadic("and", chain(15), lit_bool(value=True)))

    def test_a_node_built_in_python_is_checked_too(self) -> None:
        node: Expr = BoolLit(kind="bool", value=True)
        for _ in range(MAX_EXPR_DEPTH - 1):
            node = Not(kind="not", operand=node)
        with pytest.raises(ValidationError, match="nested 17 deep"):
            Not(kind="not", operand=node)

    def test_very_deep_json_is_refused_without_a_crash(self) -> None:
        text = '{"kind":"not","operand":' * 5000 + '{"kind":"bool","value":true}' + "}" * 5000

        with pytest.raises(ValidationError):
            EXPR.validate_json(text)
        with pytest.raises(SpecError):
            parse_ui_spec(text)

    def test_very_deep_python_data_is_refused_without_a_crash(self) -> None:
        data: Json = lit_bool(value=True)
        for _ in range(100_000):
            data = {"kind": "not", "operand": data}

        with pytest.raises(ValidationError):
            EXPR.validate_python(data)


class TestNodeCount:
    def test_the_limit_is_256_nodes_in_one_expression(self) -> None:
        assert MAX_EXPR_NODES == 256
        node = parse(tree(MAX_EXPR_NODES))
        assert measure(node)[1] == MAX_EXPR_NODES
        with pytest.raises(ValidationError, match="257 nodes, the limit is 256"):
            EXPR.validate_python(tree(MAX_EXPR_NODES + 1))

    def test_every_node_counts_including_leaves(self) -> None:
        assert measure(parse(tree(1)))[1] == 1
        assert measure(parse(tree(3)))[1] == 3
        assert measure(parse(tree(101)))[1] == 101


def test_nodes_are_hashable_values_with_equality() -> None:
    first = EXPR.validate_python(SAMPLES["add"])
    second = EXPR.validate_python(SAMPLES["add"])

    assert first == second
    assert hash(first) == hash(second)
    assert first != EXPR.validate_python(SAMPLES["sub"])


def test_node_classes_are_found_in_the_module_only_through_the_union() -> None:
    # `expr.Expr` is the one definition of the set: every class it names is defined here.
    assert {cls.__module__ for cls in EXPR_KINDS.values()} == {expr.__name__}
    assert {Add, And, Abs, Concat, FormatNumber, Max, Min, Not, Ref, Round} <= set(
        EXPR_KINDS.values()
    )
