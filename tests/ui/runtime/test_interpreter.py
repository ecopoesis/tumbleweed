"""The expression interpreter: every rule of `docs/ui-spec.md` section 7, Evaluation, with edges.

Each test is one rule, written as the expression it is about. `value_of` evaluates JSON
expressions with a plain dict as the state, with no type checking, so a case can also feed the
interpreter things a validated spec would not (to prove it is total).
"""

import inspect
import sys
import time

import pytest

from tests.ui.runtime.support import value_of
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
from tumbleweed.ui.runtime import evaluate, format_number
from tumbleweed.ui.runtime.interpreter import Program
from tumbleweed.ui.runtime.values import MAX_FLOAT
from tumbleweed.ui.spec.limits import MAX_SAFE_INT, MAX_STRING_LENGTH

MAX_INT = MAX_SAFE_INT


def unary(kind: str, operand: Json, **fields: int | bool) -> Json:
    return {"kind": kind, "operand": operand, **fields}


def choose(condition: Json, then: Json, otherwise: Json) -> Json:
    return {"kind": "if", "condition": condition, "then": then, "otherwise": otherwise}


def same(actual: object, expected: object) -> bool:
    """Equal in value *and* type: `1` is not `1.0`, and a bool is not an int."""
    return type(actual) is type(expected) and actual == expected


YES, NO = lit_bool(value=True), lit_bool(value=False)


class TestLiteralsAndReferences:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            (lit_bool(value=True), True),
            (lit_int(7), 7),
            (lit_float(1.5), 1.5),
            (lit_float(5), 5.0),  # an integer written for a float is a float
            (lit_str("héllo\n"), "héllo\n"),
            (lit_str(""), ""),
        ],
    )
    def test_a_literal_is_its_value(self, expression: Json, expected: object) -> None:
        assert same(value_of(expression), expected)

    @pytest.mark.parametrize("stored", [True, 5, 2.5, "text"])
    def test_a_reference_is_the_value_of_its_entry(self, stored: object) -> None:
        assert same(value_of(ref("x"), {"x": stored}), stored)

    def test_a_reference_to_a_value_of_no_scalar_type_is_false_not_an_error(self) -> None:
        assert value_of(ref("x"), {"x": None}) is False
        assert value_of(ref("x"), {"x": [1, 2]}) is False


class TestArithmetic:
    @pytest.mark.parametrize(
        ("kind", "left", "right", "expected"),
        [
            ("add", 2, 3, 5),
            ("sub", 2, 3, -1),
            ("mul", 4, -3, -12),
            ("add", 0, 0, 0),
        ],
    )
    def test_int_operands_give_an_int(
        self, kind: str, left: int, right: int, expected: int
    ) -> None:
        assert same(value_of(binary(kind, lit_int(left), lit_int(right))), expected)

    @pytest.mark.parametrize(
        ("kind", "left", "right", "expected"),
        [
            ("add", lit_int(1), lit_float(0.5), 1.5),
            ("add", lit_float(0.5), lit_int(1), 1.5),
            ("sub", lit_float(1.5), lit_float(0.5), 1.0),
            ("mul", lit_int(3), lit_float(0.5), 1.5),
            ("add", lit_float(1.0), lit_float(2.0), 3.0),
        ],
    )
    def test_a_float_operand_gives_a_float(
        self, kind: str, left: Json, right: Json, expected: float
    ) -> None:
        assert same(value_of(binary(kind, left, right)), expected)

    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            (lit_int(6), lit_int(3), 2.0),
            (lit_int(1), lit_int(4), 0.25),
            (lit_float(1.0), lit_int(8), 0.125),
            (lit_int(-3), lit_int(2), -1.5),
        ],
    )
    def test_div_is_always_a_float(self, left: Json, right: Json, expected: float) -> None:
        assert same(value_of(binary("div", left, right)), expected)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (lit_int(1), lit_int(0)),
            (lit_int(0), lit_int(0)),
            (lit_float(1.5), lit_float(0.0)),
            (lit_float(-1.5), lit_float(-0.0)),
            (lit_int(-MAX_INT), lit_int(0)),
            (lit_float(MAX_FLOAT), lit_int(0)),
        ],
    )
    def test_div_by_zero_is_zero_point_zero(self, left: Json, right: Json) -> None:
        assert same(value_of(binary("div", left, right)), 0.0)

    def test_div_by_a_zero_that_a_reference_reads_is_zero_point_zero(self) -> None:
        # The divisor is only known at run time, which is the case the rule is for.
        assert same(value_of(binary("div", ref("a"), ref("b")), {"a": 5, "b": 0}), 0.0)
        assert same(value_of(binary("div", ref("a"), ref("b")), {"a": 5.0, "b": 0.0}), 0.0)

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            (unary("abs", lit_int(-5)), 5),
            (unary("abs", lit_int(5)), 5),
            (unary("abs", lit_float(-2.5)), 2.5),
            (unary("abs", lit_int(-MAX_INT)), MAX_INT),
            (unary("abs", lit_float(-0.0)), 0.0),
        ],
    )
    def test_abs_keeps_the_type_of_its_number(self, expression: Json, expected: object) -> None:
        assert same(value_of(expression), expected)


class TestSaturation:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            (binary("add", lit_int(MAX_INT), lit_int(1)), MAX_INT),
            (binary("add", lit_int(MAX_INT), lit_int(MAX_INT)), MAX_INT),
            (binary("sub", lit_int(-MAX_INT), lit_int(1)), -MAX_INT),
            (binary("sub", lit_int(0), lit_int(MAX_INT)), -MAX_INT),
            (binary("mul", lit_int(MAX_INT), lit_int(MAX_INT)), MAX_INT),
            (binary("mul", lit_int(MAX_INT), lit_int(-2)), -MAX_INT),
            (binary("mul", lit_int(-MAX_INT), lit_int(-MAX_INT)), MAX_INT),
        ],
    )
    def test_an_int_result_is_clamped_to_the_safe_range(
        self, expression: Json, expected: int
    ) -> None:
        assert same(value_of(expression), expected)

    def test_clamping_happens_at_each_step_not_only_at_the_end(self) -> None:
        # (MAX + 1) - 1 is MAX - 1 if the middle value is clamped to MAX first, MAX otherwise.
        expression = binary("sub", binary("add", lit_int(MAX_INT), lit_int(1)), lit_int(1))

        assert value_of(expression) == MAX_INT - 1

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            (binary("mul", lit_float(MAX_FLOAT), lit_int(10)), MAX_FLOAT),
            (binary("mul", lit_float(MAX_FLOAT), lit_float(-10.0)), -MAX_FLOAT),
            (binary("add", lit_float(MAX_FLOAT), lit_float(MAX_FLOAT)), MAX_FLOAT),
            (binary("sub", lit_float(-MAX_FLOAT), lit_float(MAX_FLOAT)), -MAX_FLOAT),
            (binary("div", lit_float(MAX_FLOAT), lit_float(1e-300)), MAX_FLOAT),
            (binary("div", lit_float(-MAX_FLOAT), lit_float(1e-300)), -MAX_FLOAT),
        ],
    )
    def test_an_infinite_float_becomes_the_largest_finite_float_of_its_sign(
        self, expression: Json, expected: float
    ) -> None:
        result = value_of(expression)

        assert same(result, expected)

    def test_a_float_that_is_not_a_number_becomes_zero_point_zero(self) -> None:
        # No finite operation gives NaN, but a reader could hand over one; so could a bug.
        assert same(value_of(ref("x"), {"x": float("nan")}), 0.0)
        assert same(value_of(binary("add", ref("x"), lit_float(1.0)), {"x": float("nan")}), 1.0)

    def test_an_infinity_that_a_reader_gives_is_the_largest_finite_float(self) -> None:
        assert same(value_of(ref("x"), {"x": float("inf")}), MAX_FLOAT)
        assert same(value_of(ref("x"), {"x": float("-inf")}), -MAX_FLOAT)
        # ... so that arithmetic on it is never NaN: inf - inf would be.
        both = binary("sub", ref("x"), ref("y"))
        assert same(value_of(both, {"x": float("inf"), "y": float("inf")}), 0.0)

    def test_an_int_beyond_the_safe_range_that_a_reader_gives_is_clamped(self) -> None:
        assert value_of(ref("x"), {"x": 10**400}) == MAX_INT
        assert value_of(ref("x"), {"x": -(10**400)}) == -MAX_INT
        assert same(value_of(binary("add", ref("x"), lit_float(0.5)), {"x": 10**400}), 2.0**53)


class TestRound:
    @pytest.mark.parametrize(
        ("number", "expected"),
        [
            (0.0, 0),
            (0.4, 0),
            (0.5, 1),
            (0.6, 1),
            (1.5, 2),
            (2.5, 3),
            (3.5, 4),
            (-0.4, 0),
            (-0.5, -1),
            (-1.5, -2),
            (-2.5, -3),
            (0.49999999999999994, 0),  # adding 0.5 first would round this up
            (-0.49999999999999994, 0),
            (4503599627370495.5, 4503599627370496),
            (2.0**52 + 1, 2**52 + 1),
        ],
    )
    def test_halves_go_away_from_zero(self, number: float, expected: int) -> None:
        assert same(value_of(unary("round", lit_float(number))), expected)

    def test_round_of_an_int_is_that_int(self) -> None:
        assert same(value_of(unary("round", lit_int(-7))), -7)

    @pytest.mark.parametrize(
        ("number", "expected"),
        [(1e300, MAX_INT), (-1e300, -MAX_INT), (MAX_FLOAT, MAX_INT), (-MAX_FLOAT, -MAX_INT)],
    )
    def test_round_is_clamped_to_the_safe_range(self, number: float, expected: int) -> None:
        assert same(value_of(unary("round", lit_float(number))), expected)


class TestMinAndMax:
    def test_all_ints_give_an_int(self) -> None:
        assert same(value_of(variadic("min", lit_int(3), lit_int(-1), lit_int(2))), -1)
        assert same(value_of(variadic("max", lit_int(3), lit_int(-1), lit_int(2))), 3)

    def test_mixed_numbers_give_a_float(self) -> None:
        assert same(value_of(variadic("min", lit_int(1), lit_float(2.5))), 1.0)
        assert same(value_of(variadic("max", lit_int(3), lit_float(2.5))), 3.0)
        assert same(value_of(variadic("max", lit_float(0.5), lit_int(-3), lit_int(-2))), 0.5)

    def test_floats_give_a_float(self) -> None:
        assert same(value_of(variadic("min", lit_float(2.5), lit_float(-0.5))), -0.5)

    def test_the_most_operands_allowed(self) -> None:
        operands = [lit_int(n) for n in (5, 3, 8, 1, 9, 2, 7, 4)]

        assert value_of(variadic("min", *operands)) == 1
        assert value_of(variadic("max", *operands)) == 9


class TestIf:
    def test_it_chooses_by_the_condition(self) -> None:
        assert value_of(choose(YES, lit_str("a"), lit_str("b"))) == "a"
        assert value_of(choose(NO, lit_str("a"), lit_str("b"))) == "b"

    def test_an_int_branch_of_an_if_with_a_float_branch_is_a_float_whichever_is_chosen(
        self,
    ) -> None:
        # `if(c, 1, 2.5)` is `1.0` when c is true; and the same the other way round.
        assert same(value_of(choose(YES, lit_int(1), lit_float(2.5))), 1.0)
        assert same(value_of(choose(NO, lit_int(1), lit_float(2.5))), 2.5)
        assert same(value_of(choose(YES, lit_float(2.5), lit_int(1))), 2.5)
        assert same(value_of(choose(NO, lit_float(2.5), lit_int(1))), 1.0)

    def test_two_int_branches_stay_ints(self) -> None:
        assert same(value_of(choose(YES, lit_int(1), lit_int(2))), 1)
        assert same(value_of(choose(NO, lit_int(1), lit_int(2))), 2)

    def test_the_conversion_looks_at_the_types_of_the_branches_not_their_values(self) -> None:
        # A float branch that is a whole number still makes the result a float.
        assert same(value_of(choose(YES, lit_int(1), lit_float(2.0))), 1.0)

    def test_branches_of_other_types_are_as_they_are(self) -> None:
        assert same(value_of(choose(YES, YES, NO)), True)
        assert same(value_of(choose(NO, lit_str("a"), lit_str("b"))), "b")

    def test_a_branch_that_would_divide_by_zero_does_no_harm_when_not_chosen(self) -> None:
        expression = choose(YES, lit_int(1), unary("round", binary("div", lit_int(1), lit_int(0))))

        assert same(value_of(expression), 1)


class TestComparison:
    @pytest.mark.parametrize(
        ("left", "right", "equal"),
        [
            (lit_int(1), lit_float(1.0), True),  # by value
            (lit_float(1.0), lit_int(1), True),
            (lit_int(1), lit_float(1.5), False),
            (lit_float(0.0), lit_float(-0.0), True),
            (lit_int(MAX_INT), lit_float(float(MAX_INT)), True),
            (lit_int(MAX_INT - 1), lit_float(float(MAX_INT)), False),  # exact, not rounded
            (lit_str("a"), lit_str("a"), True),
            (lit_str("a"), lit_str("A"), False),
            (lit_str(""), lit_str(""), True),
            (YES, YES, True),
            (YES, NO, False),
        ],
    )
    def test_eq_and_ne(self, left: Json, right: Json, equal: bool) -> None:
        assert value_of(binary("eq", left, right)) is equal
        assert value_of(binary("ne", left, right)) is (not equal)

    def test_strings_are_compared_by_code_point_with_no_normalisation(self) -> None:
        composed, decomposed = "é", "é"  # the same letter, two spellings

        assert value_of(binary("eq", lit_str(composed), lit_str(decomposed))) is False
        assert value_of(binary("ne", lit_str(composed), lit_str(decomposed))) is True
        assert value_of(binary("eq", lit_str(composed), lit_str(composed))) is True

    def test_a_bool_is_not_a_number_in_eq(self) -> None:
        assert value_of(binary("eq", YES, lit_int(1))) is False  # not reachable in a checked spec

    @pytest.mark.parametrize(
        ("kind", "left", "right", "expected"),
        [
            ("lt", 1, 2, True),
            ("lt", 2, 2, False),
            ("le", 2, 2, True),
            ("le", 3, 2, False),
            ("gt", 3, 2, True),
            ("gt", 2, 2, False),
            ("ge", 2, 2, True),
            ("ge", 1, 2, False),
        ],
    )
    def test_ordering(self, kind: str, left: int, right: int, expected: bool) -> None:
        assert value_of(binary(kind, lit_int(left), lit_int(right))) is expected
        assert value_of(binary(kind, lit_float(left), lit_int(right))) is expected
        assert value_of(binary(kind, lit_int(left), lit_float(right))) is expected

    def test_ordering_is_exact_beyond_the_precision_of_a_float(self) -> None:
        big = binary("lt", lit_int(MAX_INT - 1), lit_float(float(MAX_INT)))

        assert value_of(big) is True


class TestLogic:
    @pytest.mark.parametrize(
        ("args", "all_of", "any_of"),
        [
            ([True, True], True, True),
            ([True, False], False, True),
            ([False, False], False, False),
            ([False, False, True], False, True),
            ([True] * 8, True, True),
            ([False] * 8, False, False),
        ],
    )
    def test_and_or(self, args: list[bool], all_of: bool, any_of: bool) -> None:
        operands = [lit_bool(value=a) for a in args]

        assert value_of(variadic("and", *operands)) is all_of
        assert value_of(variadic("or", *operands)) is any_of

    def test_not(self) -> None:
        assert value_of(unary("not", YES)) is False
        assert value_of(unary("not", NO)) is True


class TestConcat:
    def test_strings_join_in_order(self) -> None:
        assert value_of(variadic("concat", lit_str("a"), lit_str("b"), lit_str("c"))) == "abc"
        assert value_of(variadic("concat", lit_str(""), lit_str(""))) == ""

    def test_text_is_joined_as_it_is(self) -> None:
        assert value_of(variadic("concat", lit_str("<b>"), lit_str("%s {0}"))) == "<b>%s {0}"

    def test_a_result_is_cut_at_the_longest_string_an_entry_can_hold(self) -> None:
        long = "x" * MAX_STRING_LENGTH
        expression = variadic("concat", ref("s"), ref("s"), ref("s"))

        result = value_of(expression, {"s": long})

        assert isinstance(result, str)
        assert len(result) == MAX_STRING_LENGTH

    def test_the_widest_and_the_deepest_trees_of_long_strings_stay_bounded(self) -> None:
        # Without the cut, a chain of 16 concats of a 64 KiB string is a megabyte, and a tree of
        # 73 nodes (every operand a concat of eight) is half a megabyte per level; with it, no
        # step is longer than eight strings of the limit.
        long = "y" * MAX_STRING_LENGTH
        leaf = variadic("concat", *([ref("s")] * 8))
        wide = variadic("concat", *([leaf] * 8))
        chain: Json = ref("s")
        for _ in range(15):
            chain = variadic("concat", chain, ref("s"))
        started = time.monotonic()

        for expression in (wide, chain):
            result = value_of(expression, {"s": long})
            assert isinstance(result, str)
            assert len(result) == MAX_STRING_LENGTH

        assert time.monotonic() - started < 5


class TestFormatNumber:
    @pytest.mark.parametrize(
        ("number", "decimals", "grouping", "expected"),
        [
            (0, 0, False, "0"),
            (0, 2, False, "0.00"),
            (5, 0, False, "5"),
            (-5, 0, False, "-5"),
            (1234567, 0, True, "1,234,567"),
            (1234567, 0, False, "1234567"),
            (999, 0, True, "999"),
            (1000, 0, True, "1,000"),
            (-1234, 0, True, "-1,234"),
            (-999999, 0, True, "-999,999"),
            (1234567.891, 2, True, "1,234,567.89"),
            (1234567.891, 2, False, "1234567.89"),
            (0.5, 0, False, "1"),  # halves away from zero
            (1.5, 0, False, "2"),
            (2.5, 0, False, "3"),
            (-0.5, 0, False, "-1"),
            (-2.5, 0, False, "-3"),
            (0.125, 2, False, "0.13"),
            (-0.125, 2, False, "-0.13"),
            (2.675, 2, False, "2.68"),  # the digits a person sees, not the double's hair below
            (1.005, 2, False, "1.01"),
            (0.1 + 0.2, 2, False, "0.30"),
            (0.1 + 0.2, 10, False, "0.3000000000"),
            (-0.001, 2, False, "0.00"),  # never -0
            (-0.0, 0, False, "0"),
            (-0.4, 0, False, "0"),
            (-0.004, 2, True, "0.00"),
            (0.999, 2, False, "1.00"),
            (999.995, 2, True, "1,000.00"),
            (1.0, 3, False, "1.000"),
            (MAX_INT, 0, True, "9,007,199,254,740,991"),
            (-MAX_INT, 0, False, "-9007199254740991"),
            (1e21, 0, False, "1" + "0" * 21),
            (1e-7, 10, False, "0.0000001000"),
            (5e-324, 10, False, "0.0000000000"),
            (123456789.123456789, 10, False, "123456789.1234567900"),  # repr: 123456789.12345679
        ],
    )
    def test_format(self, number: float, decimals: int, grouping: bool, expected: str) -> None:
        assert format_number(number, decimals, grouping=grouping) == expected

    @pytest.mark.parametrize("decimals", range(11))
    def test_every_allowed_count_of_decimals_gives_exactly_that_many(self, decimals: int) -> None:
        text = format_number(1.5, decimals, grouping=False)

        assert text.partition(".")[2].__len__() == decimals

    def test_the_largest_float_is_a_bounded_string(self) -> None:
        text = format_number(MAX_FLOAT, 10, grouping=True)

        assert len(text) < 450
        assert text.startswith("179,769,313,486,231,57")
        assert text.endswith(".0000000000")

    def test_through_the_interpreter(self) -> None:
        expression = unary("format_number", ref("x"), decimals=2, grouping=True)

        assert value_of(expression, {"x": 1234.5}) == "1,234.50"
        assert value_of(expression, {"x": 7}) == "7.00"
        assert value_of(unary("format_number", ref("x")), {"x": 2.5}) == "3"

    def test_it_does_not_depend_on_the_locale(self) -> None:
        source = inspect.getsource(sys.modules[format_number.__module__])

        assert "locale" not in source.replace("no locale", "")


class TestEvaluationIsEager:
    def test_every_operand_is_evaluated_so_the_type_of_an_if_does_not_depend_on_the_condition(
        self,
    ) -> None:
        reads: list[str] = []

        def read(key: str) -> object:
            reads.append(key)
            return 1

        from tests.ui.runtime.support import EXPR

        program = EXPR.validate_python(choose(YES, ref("a"), ref("b")))
        evaluate(program, read)

        assert sorted(reads) == ["a", "b"]


class TestProgram:
    def test_a_program_can_be_evaluated_again_with_other_state(self) -> None:
        from tests.ui.runtime.support import EXPR

        program = Program(EXPR.validate_python(binary("add", ref("a"), lit_int(1))))

        assert evaluate(program, lambda _key: 1) == 2
        assert evaluate(program, lambda _key: 10) == 11

    def test_operands_are_evaluated_left_to_right_in_the_order_written(self) -> None:
        from tests.ui.runtime.support import EXPR

        program = Program(EXPR.validate_python(binary("sub", ref("a"), ref("b"))))
        values = {"a": 10, "b": 3}

        assert evaluate(program, lambda key: values[key]) == 7


class TestTheStack:
    def test_a_caller_near_the_recursion_limit_still_gets_a_value(self) -> None:
        # The evaluator is iterative: how deep the expression is does not use up the stack, so
        # even a caller a few frames from the limit gets a value (#62).
        from tests.ui.runtime.support import EXPR

        expression: Json = lit_int(1)
        for _ in range(15):
            expression = binary("add", expression, lit_int(1))
        program = Program(EXPR.validate_python(expression))
        base = len(inspect.stack(0))
        results: list[object] = []

        def descend(levels: int) -> None:
            if levels:
                descend(levels - 1)
                return
            results.append(evaluate(program, lambda _key: 0))

        for headroom in range(20, 70):
            descend(sys.getrecursionlimit() - base - headroom)

        assert results == [16] * 50
