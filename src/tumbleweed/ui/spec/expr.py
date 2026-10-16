"""The expression AST: a closed set of typed nodes, never a string to parse (I1).

An expression is a tree of the 25 node kinds below, discriminated by `kind`. A spec holds
expressions as JSON objects, in a computed state declaration and in the assignments of a `set`
handler. There is no node that calls a function by name, reads an attribute, takes a format
string or holds source text, and no field that takes a string where an expression goes: a
string there is a validation error, not something parsed. So there is no route from a spec to
Python evaluation, and the interpreter (S10) is a plain `match` over these classes.

Typing is a separate pass (`tumbleweed.ui.spec.check`): the models enforce shape and limits,
the checker enforces that the operands of each node have the right types. The four types are
`bool`, `int`, `float` and `string`; a number is never converted to a string except by
`format_number`, whose parameters are bounded integers, not a format string.

Limits (`tumbleweed.ui.spec.limits`): depth at most `MAX_EXPR_DEPTH`, at most `MAX_EXPR_NODES`
nodes, at most `MAX_EXPR_ARGS` operands for the variadic nodes. They are checked by every node
as it is built from its already-checked operands, so an over-deep expression is refused at the
first node that exceeds the limit, wherever it was built and whether or not it ends up in a
spec.
"""

from typing import Annotated, Literal, Self, final

from pydantic import BeforeValidator, Field, model_validator

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.ui.spec.limits import MAX_DECIMALS, MAX_EXPR_ARGS, MAX_EXPR_DEPTH, MAX_EXPR_NODES
from tumbleweed.ui.spec.model import SpecModel
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.text import Block, FiniteFloat, SafeInt, as_tuple

# Declared first: the aliases are lazy, so the classes below can be named in them before they
# exist, and the classes can use the aliases in their fields.
type Expr = Annotated[
    BoolLit
    | IntLit
    | FloatLit
    | StrLit
    | Ref
    | Add
    | Sub
    | Mul
    | Div
    | Min
    | Max
    | Abs
    | Round
    | Eq
    | Ne
    | Lt
    | Le
    | Gt
    | Ge
    | And
    | Or
    | Not
    | If
    | Concat
    | FormatNumber,
    Field(discriminator="kind"),
]
type Args = Annotated[
    tuple[Expr, ...], Field(min_length=2, max_length=MAX_EXPR_ARGS), BeforeValidator(as_tuple)
]


class ExprNode(SpecModel):
    """Base of the node classes: the depth and size limits, and the operands of a node."""

    def operands(self) -> tuple[Expr, ...]:
        """The direct sub-expressions, in the order they are written."""
        return ()

    @model_validator(mode="after")
    def _within_limits(self) -> Self:
        # Every operand was validated before this node, so it is at most MAX_EXPR_DEPTH deep
        # and MAX_EXPR_NODES large and the recursion below is at most that deep.
        depth, nodes = measure(self)
        if depth > MAX_EXPR_DEPTH:
            raise InvalidValueError(
                f"expression is nested {depth} deep, the limit is {MAX_EXPR_DEPTH}"
            )
        if nodes > MAX_EXPR_NODES:
            raise InvalidValueError(f"expression has {nodes} nodes, the limit is {MAX_EXPR_NODES}")
        return self


def measure(expr: ExprNode) -> tuple[int, int]:
    """The depth and the node count of an expression whose operands are already validated."""
    depth = 0
    nodes = 1
    for operand in expr.operands():
        operand_depth, operand_nodes = measure(operand)
        depth = max(depth, operand_depth)
        nodes += operand_nodes
    return depth + 1, nodes


# --- literals and references -----------------------------------------------------------------


@final
class BoolLit(ExprNode):
    """`true` or `false`. Type `bool`."""

    kind: Literal["bool"]
    value: bool


@final
class IntLit(ExprNode):
    """An integer within +-(2**53 - 1). Type `int`; `5.0` is not an integer here."""

    kind: Literal["int"]
    value: SafeInt


@final
class FloatLit(ExprNode):
    """A finite number. Type `float`; `5` is accepted and means 5.0."""

    kind: Literal["float"]
    value: FiniteFloat


@final
class StrLit(ExprNode):
    """Authored text (block rules: no control characters but newline and tab). Type `string`."""

    kind: Literal["string"]
    value: Block


@final
class Ref(ExprNode):
    """The current value of a state entry. Type: that of the entry (an enum is a `string`)."""

    kind: Literal["ref"]
    key: StateKey


# --- arithmetic ------------------------------------------------------------------------------


@final
class Add(ExprNode):
    """`left + right`: `int` if both are, else `float`."""

    kind: Literal["add"]
    left: Expr
    right: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


@final
class Sub(ExprNode):
    """`left - right`: `int` if both are, else `float`."""

    kind: Literal["sub"]
    left: Expr
    right: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


@final
class Mul(ExprNode):
    """`left * right`: `int` if both are, else `float`."""

    kind: Literal["mul"]
    left: Expr
    right: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


@final
class Div(ExprNode):
    """`left / right`: always `float`."""

    kind: Literal["div"]
    left: Expr
    right: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


@final
class Min(ExprNode):
    """The smallest of 2 to `MAX_EXPR_ARGS` numbers: `int` if all are, else `float`."""

    kind: Literal["min"]
    args: Args

    def operands(self) -> tuple[Expr, ...]:
        return self.args


@final
class Max(ExprNode):
    """The largest of 2 to `MAX_EXPR_ARGS` numbers: `int` if all are, else `float`."""

    kind: Literal["max"]
    args: Args

    def operands(self) -> tuple[Expr, ...]:
        return self.args


@final
class Abs(ExprNode):
    """The absolute value of a number, of the same type."""

    kind: Literal["abs"]
    operand: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.operand,)


@final
class Round(ExprNode):
    """A number rounded to the nearest whole number, as an `int`. Always `int`."""

    kind: Literal["round"]
    operand: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.operand,)


# --- comparison and logic --------------------------------------------------------------------


@final
class Eq(ExprNode):
    """`left == right`, for two numbers, two strings or two bools. Type `bool`."""

    kind: Literal["eq"]
    left: Expr
    right: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


@final
class Ne(ExprNode):
    """`left != right`, for two numbers, two strings or two bools. Type `bool`."""

    kind: Literal["ne"]
    left: Expr
    right: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


@final
class Lt(ExprNode):
    """`left < right`, for two numbers. Type `bool`."""

    kind: Literal["lt"]
    left: Expr
    right: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


@final
class Le(ExprNode):
    """`left <= right`, for two numbers. Type `bool`."""

    kind: Literal["le"]
    left: Expr
    right: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


@final
class Gt(ExprNode):
    """`left > right`, for two numbers. Type `bool`."""

    kind: Literal["gt"]
    left: Expr
    right: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


@final
class Ge(ExprNode):
    """`left >= right`, for two numbers. Type `bool`."""

    kind: Literal["ge"]
    left: Expr
    right: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


@final
class And(ExprNode):
    """True when all of 2 to `MAX_EXPR_ARGS` bools are. Type `bool`."""

    kind: Literal["and"]
    args: Args

    def operands(self) -> tuple[Expr, ...]:
        return self.args


@final
class Or(ExprNode):
    """True when any of 2 to `MAX_EXPR_ARGS` bools is. Type `bool`."""

    kind: Literal["or"]
    args: Args

    def operands(self) -> tuple[Expr, ...]:
        return self.args


@final
class Not(ExprNode):
    """The opposite of a bool. Type `bool`."""

    kind: Literal["not"]
    operand: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.operand,)


@final
class If(ExprNode):
    """`then` when `condition` is true, else `otherwise`.

    The branches have the same type, or are both numbers (then the result is `float` if either
    is). Only the chosen branch is meaningful, but both are always type-checked.
    """

    kind: Literal["if"]
    condition: Expr
    then: Expr
    otherwise: Expr

    def operands(self) -> tuple[Expr, ...]:
        return (self.condition, self.then, self.otherwise)


# --- strings ---------------------------------------------------------------------------------


@final
class Concat(ExprNode):
    """2 to `MAX_EXPR_ARGS` strings joined in order. Type `string`; numbers need `format_number`."""

    kind: Literal["concat"]
    args: Args

    def operands(self) -> tuple[Expr, ...]:
        return self.args


@final
class FormatNumber(ExprNode):
    """A number as text, with a fixed count of decimals and optional digit grouping.

    `decimals` is 0 to `MAX_DECIMALS`. `grouping` separates thousands. There is no format
    string and no locale: the renderer always writes `.` before the decimals and `,` between
    the groups, so a tool looks the same everywhere. Type `string`.
    """

    kind: Literal["format_number"]
    operand: Expr
    decimals: Annotated[int, Field(ge=0, le=MAX_DECIMALS)] = 0
    grouping: bool = False

    def operands(self) -> tuple[Expr, ...]:
        return (self.operand,)
