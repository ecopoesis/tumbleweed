"""The expression interpreter: a total, hand-written evaluator of the AST (I1, I9).

It implements `docs/ui-spec.md` section 7, Evaluation, over the closed node set of
`tumbleweed.ui.spec.expr`; there is no route from a spec to Python evaluation. Properties:

- **Total.** `evaluate` returns a value for every validated expression and every reader:
  `div` by zero is 0.0, an `int` result is held to +-(2**53 - 1), a `float` result that is
  infinite becomes the largest finite float of its sign and one that is not a number becomes 0.0.
  A value the reader gives that is not of the type the checker promised (a validated spec cannot
  cause it) counts as the zero of the type asked for, rather than raising.
- **Iterative.** An expression is flattened to post-order once (`Program`) and evaluated with a
  value stack, so the depth of the Python stack does not depend on the expression, and a caller
  close to the recursion limit still gets a value (#62).
- **Eager.** Every operand of every node is evaluated, as `if` and the type checker do (both
  branches are checked). That makes `if(c, 1, 2.5)` a `float` whichever branch is chosen: both
  values are known, and the chosen one is converted when either is a `float`.
- **Bounded.** An expression has at most `MAX_EXPR_NODES` nodes. A string is cut to
  `MAX_STRING_LENGTH` characters at each `concat`, the largest a `string` entry can hold, so a
  tree that repeats a long string cannot build a string of hundreds of megabytes; `format_number`
  writes at most about 420 characters (309 digits, 10 decimals, 103 separators).
"""

import operator
from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Final, assert_never, final

from tumbleweed.ui.runtime.values import (
    Value,
    clamp_int,
    finite,
    number_of,
    round_half_away,
    to_float,
)
from tumbleweed.ui.spec.expr import (
    Abs,
    Add,
    And,
    BoolLit,
    Concat,
    Div,
    Eq,
    Expr,
    FloatLit,
    FormatNumber,
    Ge,
    Gt,
    If,
    IntLit,
    Le,
    Lt,
    Max,
    Min,
    Mul,
    Ne,
    Not,
    Or,
    Ref,
    Round,
    StrLit,
    Sub,
)
from tumbleweed.ui.spec.limits import MAX_DECIMALS, MAX_STRING_LENGTH
from tumbleweed.ui.spec.names import StateKey

type Reader = Callable[[StateKey], object]
"""How an expression reads state. The interpreter sanitises what it returns."""

# A double has at most 309 digits before the point, and `format_number` adds at most
# `MAX_DECIMALS` after it: this precision makes `quantize` unable to fail.
_CONTEXT: Final = Context(prec=400, rounding=ROUND_HALF_UP)
_STEPS: Final = tuple(Decimal(1).scaleb(-places) for places in range(MAX_DECIMALS + 1))


def _sanitised(value: object) -> Value:
    """A value read from state, in the range an expression works with."""
    if isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return clamp_int(value)
    if isinstance(value, float):
        return finite(value)
    return False


def _number(value: Value) -> int | float:
    found = number_of(value)
    return 0 if found is None else found


def _flag(value: Value) -> bool:
    return value is True


def _text(value: Value) -> str:
    return value if isinstance(value, str) else ""


def _as_float(number: int | float) -> float:
    return to_float(number) if isinstance(number, int) else number


def _arith(
    left: Value,
    right: Value,
    on_ints: Callable[[int, int], int],
    on_floats: Callable[[float, float], float],
) -> Value:
    """An `int` if both operands are, else a `float`; held to the range of its type."""
    first, second = _number(left), _number(right)
    if isinstance(first, int) and isinstance(second, int):
        return clamp_int(on_ints(first, second))
    return finite(on_floats(_as_float(first), _as_float(second)))


def _divide(left: Value, right: Value) -> float:
    divisor = _as_float(_number(right))
    if divisor == 0.0:
        return 0.0
    return finite(_as_float(_number(left)) / divisor)


def _extreme(args: Sequence[Value], *, largest: bool) -> Value:
    """`min` or `max`: an `int` if all operands are, else a `float`."""
    numbers = [_number(arg) for arg in args]
    ints = [n for n in numbers if isinstance(n, int)]
    if len(ints) == len(numbers):
        return max(ints) if largest else min(ints)
    floats = [_as_float(n) for n in numbers]
    return max(floats) if largest else min(floats)


def _compare(left: Value, right: Value, *, order: Callable[[float, float], bool]) -> bool:
    return order(_number(left), _number(right))


def _equal(left: Value, right: Value) -> bool:
    """Numbers by value (`1` equals `1.0`), strings by code point, bools as they are."""
    first, second = number_of(left), number_of(right)
    if first is not None and second is not None:
        return first == second
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, bool) and isinstance(right, bool):
        return left == right
    return False  # unlike types: not reachable in a spec that type-checks


def format_number(value: Value, decimals: int, *, grouping: bool) -> str:
    """The number rounded half away from zero to `decimals` places, as the spec writes it.

    `.` before the decimals and, with `grouping`, `,` between groups of three digits; `-` for a
    negative number but never `-0`; no locale. A float is rounded as the shortest decimal that
    reads back as it (`repr`), the digits a person sees and JSON writes: 2.675 is `2.68`, where
    its exact binary value 2.67499999999999982... would give `2.67`.
    """
    number = _number(value)
    exact = Decimal(number) if isinstance(number, int) else Decimal(repr(finite(number)))
    places = min(max(decimals, 0), MAX_DECIMALS)
    rounded = exact.quantize(_STEPS[places], context=_CONTEXT)
    if rounded.is_zero():
        rounded = rounded.copy_abs()  # `-0.00` is `0.00`
    return format(rounded, ",f" if grouping else "f")


def _apply(node: Expr, args: list[Value], read: Reader) -> Value:
    """The value of one node, given the values of its operands in the order they are written."""
    match node:
        case BoolLit() | IntLit() | StrLit():
            return node.value
        case FloatLit():
            return finite(float(node.value))
        case Ref():
            return _sanitised(read(node.key))
        case Add():
            return _arith(args[0], args[1], operator.add, operator.add)
        case Sub():
            return _arith(args[0], args[1], operator.sub, operator.sub)
        case Mul():
            return _arith(args[0], args[1], operator.mul, operator.mul)
        case Div():
            return _divide(args[0], args[1])
        case Min():
            return _extreme(args, largest=False)
        case Max():
            return _extreme(args, largest=True)
        case Abs():
            number = _number(args[0])
            return clamp_int(abs(number)) if isinstance(number, int) else abs(number)
        case Round():
            number = _number(args[0])
            return number if isinstance(number, int) else round_half_away(number)
        case Eq():
            return _equal(args[0], args[1])
        case Ne():
            return not _equal(args[0], args[1])
        case Lt():
            return _compare(args[0], args[1], order=operator.lt)
        case Le():
            return _compare(args[0], args[1], order=operator.le)
        case Gt():
            return _compare(args[0], args[1], order=operator.gt)
        case Ge():
            return _compare(args[0], args[1], order=operator.ge)
        case And():
            return all(_flag(arg) for arg in args)
        case Or():
            return any(_flag(arg) for arg in args)
        case Not():
            return not _flag(args[0])
        case If():
            chosen = args[1] if _flag(args[0]) else args[2]
            branches = (number_of(args[1]), number_of(args[2]))
            mixed = any(isinstance(branch, float) for branch in branches)
            chosen_number = number_of(chosen)
            if mixed and chosen_number is not None:
                return _as_float(chosen_number)  # the value of whichever branch, as a float
            return chosen
        case Concat():
            return "".join(_text(arg) for arg in args)[:MAX_STRING_LENGTH]
        case FormatNumber():
            return format_number(args[0], node.decimals, grouping=node.grouping)
        case _ as unreachable:
            assert_never(unreachable)


@final
class Program:
    """An expression flattened to post-order: every node after its operands.

    Built once per expression (the runtime keeps one per computed entry and per assignment), so
    that evaluating it is a loop and not a recursion.
    """

    __slots__ = ("steps",)

    def __init__(self, expr: Expr) -> None:
        order: list[Expr] = []
        stack: list[Expr] = [expr]
        while stack:
            node = stack.pop()
            order.append(node)
            stack.extend(node.operands())
        order.reverse()  # children (operands, in written order) before their parent
        self.steps: Final = tuple((node, len(node.operands())) for node in order)


def evaluate(program: Program | Expr, read: Reader) -> Value:
    """The value of an expression. Never raises (see the module docstring)."""
    steps = (program if isinstance(program, Program) else Program(program)).steps
    values: list[Value] = []
    for node, arity in steps:
        operands = values[len(values) - arity :]
        del values[len(values) - arity :]
        values.append(_apply(node, operands, read))
    return values[0]
