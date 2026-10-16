"""The hand-written type checker for expressions, and the dependency order of computed state.

`check_expr` gives an expression one of four types (`bool`, `int`, `float`, `string`) or raises
`InvalidValueError` naming the node where it goes wrong. The rules, by node:

- Literals have the type their kind names. `ref` has the type of the entry it reads, and an
  `enum` entry reads as a `string`.
- `add`, `sub`, `mul` take two numbers and give an `int` if both are `int`, else a `float`.
  `div` takes two numbers and always gives a `float`.
- `min` and `max` take numbers and give an `int` if all are `int`, else a `float`. `abs` keeps
  the type of its number. `round` takes a number and gives an `int`.
- `eq` and `ne` take two numbers, two strings or two bools. `lt`, `le`, `gt` and `ge` take two
  numbers. All give a `bool`.
- `and`, `or` and `not` take bools and give a `bool`.
- `if` takes a bool and two branches of one type, or two numbers (then a `float` if either
  is); both branches are always checked.
- `concat` takes strings and gives a `string`. `format_number` takes a number and gives a
  `string`.

A number is an `int` or a `float`. There is no implicit conversion between a number and a
string, a bool and a number, or a float and an int (`round` is how a float becomes an int). An
entry of kind `image`, `file`, `table`, `list` or `json` cannot be read by an expression.
Comparing an enum entry with a string literal also checks that the literal is one of its options,
because `eq(ref(size), "Large")` with no such option would be silently always false.

Computed state is evaluated in dependency order; `computed_order` finds that order, and refuses
a cycle (including an entry that refers to itself).
"""

from collections.abc import Mapping, Sequence
from typing import Literal, assert_never

from tumbleweed.core.errors import InvalidValueError, excerpt
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
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import (
    BoolState,
    EnumState,
    FloatState,
    IntState,
    StateDecl,
    StringState,
)

type ExprType = Literal["bool", "int", "float", "string"]

_NUMBERS = frozenset({"int", "float"})


def expr_type_of(decl: StateDecl) -> ExprType | None:
    """The type an expression sees when it reads this entry, or None if it cannot read it."""
    match decl:
        case BoolState():
            return "bool"
        case IntState():
            return "int"
        case FloatState():
            return "float"
        case StringState() | EnumState():
            return "string"
        case _:
            return None


def _arithmetic_type(types: Sequence[ExprType]) -> ExprType:
    return "float" if "float" in types else "int"


type _Wanted = Literal["bool", "number", "string"]


class _Checker:
    def __init__(self, decls: Mapping[StateKey, StateDecl]) -> None:
        self._decls = decls

    def _expect(self, operand: Expr, path: str, wanted: _Wanted) -> ExprType:
        found = self.visit(operand, path)
        if (found not in _NUMBERS) if wanted == "number" else (found != wanted):
            raise InvalidValueError(f"{path}: expected a {wanted}, got {found}")
        return found

    def _all(self, operands: Sequence[Expr], name: str, wanted: _Wanted) -> list[ExprType]:
        return [self._expect(operand, f"{name}[{i}]", wanted) for i, operand in enumerate(operands)]

    def _pair(self, left: Expr, right: Expr, name: str, *, numbers_only: bool) -> ExprType:
        """The type of two operands that must both be numbers, or (if allowed) of one type."""
        left_type = self.visit(left, f"{name}.left")
        right_type = self.visit(right, f"{name}.right")
        if left_type in _NUMBERS and right_type in _NUMBERS:
            return _arithmetic_type([left_type, right_type])
        if numbers_only or left_type != right_type:
            wanted = "two numbers" if numbers_only else "two numbers, two strings or two bools"
            raise InvalidValueError(f"{name}: expected {wanted}, got {left_type} and {right_type}")
        return left_type

    def _enum_literal(self, left: Expr, right: Expr, name: str) -> None:
        for reference, literal in ((left, right), (right, left)):
            if isinstance(reference, Ref) and isinstance(literal, StrLit):
                decl = self._decls[reference.key]  # the operands were visited, so it exists
                if isinstance(decl, EnumState) and literal.value not in decl.options:
                    raise InvalidValueError(
                        f"{name}: {excerpt(literal.value)} is not an option of {reference.key}"
                    )

    def visit(self, expr: Expr, path: str) -> ExprType:
        # `path` names the node for the error message: `add.left`, `if.then`, `and[1]`.
        match expr:
            case BoolLit():
                return "bool"
            case IntLit():
                return "int"
            case FloatLit():
                return "float"
            case StrLit():
                return "string"
            case Ref():
                decl = self._decls.get(expr.key)
                if decl is None:
                    raise InvalidValueError(f"{path}: unknown state key {excerpt(str(expr.key))}")
                ref_type = expr_type_of(decl)
                if ref_type is None:
                    raise InvalidValueError(
                        f"{path}: {expr.key} is of kind {decl.kind}, which expressions cannot read"
                    )
                return ref_type
            case Add() | Sub() | Mul():
                return self._pair(expr.left, expr.right, expr.kind, numbers_only=True)
            case Div():
                self._pair(expr.left, expr.right, "div", numbers_only=True)
                return "float"
            case Min() | Max():
                return _arithmetic_type(self._all(expr.args, expr.kind, "number"))
            case Abs():
                return self._expect(expr.operand, "abs.operand", "number")
            case Round():
                self._expect(expr.operand, "round.operand", "number")
                return "int"
            case Eq() | Ne():
                self._pair(expr.left, expr.right, expr.kind, numbers_only=False)
                self._enum_literal(expr.left, expr.right, expr.kind)
                return "bool"
            case Lt() | Le() | Gt() | Ge():
                self._pair(expr.left, expr.right, expr.kind, numbers_only=True)
                return "bool"
            case And() | Or():
                self._all(expr.args, expr.kind, "bool")
                return "bool"
            case Not():
                self._expect(expr.operand, "not.operand", "bool")
                return "bool"
            case If():
                self._expect(expr.condition, "if.condition", "bool")
                then_type = self.visit(expr.then, "if.then")
                otherwise_type = self.visit(expr.otherwise, "if.otherwise")
                if then_type in _NUMBERS and otherwise_type in _NUMBERS:
                    return _arithmetic_type([then_type, otherwise_type])
                if then_type != otherwise_type:
                    raise InvalidValueError(
                        f"if: the branches must have one type, got {then_type} and {otherwise_type}"
                    )
                return then_type
            case Concat():
                self._all(expr.args, "concat", "string")
                return "string"
            case FormatNumber():
                self._expect(expr.operand, "format_number.operand", "number")
                return "string"
            case _ as unreachable:
                assert_never(unreachable)


def check_expr(expr: Expr, decls: Mapping[StateKey, StateDecl]) -> ExprType:
    """The type of `expr` over the declared state, or `InvalidValueError` naming the node.

    The error text locates the node by its position, such as `add.left` or `if.then`, in the
    innermost operator that fails: enough for the model to find it, never the whole expression.
    """
    return _Checker(decls).visit(expr, "expression")


def references(expr: Expr) -> list[StateKey]:
    """The state keys an expression reads, in order of first appearance, without repeats."""
    found: dict[StateKey, None] = {}
    stack: list[Expr] = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, Ref):
            found.setdefault(node.key, None)
        stack.extend(reversed(node.operands()))
    return list(found)


def computed_order(decls: Sequence[StateDecl]) -> tuple[StateKey, ...]:
    """The computed entries, each after the computed entries it reads.

    Ties follow declaration order, so the order is deterministic. Raises `InvalidValueError`
    naming a cycle (`a -> b -> a`); an entry that reads itself is a cycle of one. Keys that are
    not computed entries (plain state, and unknown keys, which `check_expr` reports) are leaves.
    """
    reads: dict[StateKey, list[StateKey]] = {}
    for decl in decls:
        if isinstance(decl, BoolState | IntState | FloatState | StringState) and decl.computed:
            reads[decl.key] = references(decl.computed)
    order: list[StateKey] = []
    done: set[StateKey] = set()
    for start in reads:
        if start in done:
            continue
        # Iterative depth-first search; `path` is the chain of entries being resolved.
        path: list[StateKey] = [start]
        pending = [iter(reads[start])]
        while pending:
            for dependency in pending[-1]:
                if dependency in path:
                    cycle = [*path[path.index(dependency) :], dependency]
                    raise InvalidValueError(f"computed state has a cycle: {' -> '.join(cycle)}")
                if dependency in reads and dependency not in done:
                    path.append(dependency)
                    pending.append(iter(reads[dependency]))
                    break
            else:
                finished = path.pop()
                pending.pop()
                done.add(finished)
                order.append(finished)
    return tuple(order)
