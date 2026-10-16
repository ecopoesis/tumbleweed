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
Comparing an enum entry with a string literal also checks that the literal is one of its options
(a literal reached through the branches of an `if` counts too), and comparing two enum entries
needs an option in common, because `eq(ref(size), "Large")` with no such option, or `eq` of two
enums with no option in common, would be silently always false (and `ne` always true).

Computed state is evaluated in dependency order; `computed_order` finds that order, and refuses
a cycle (including an entry that refers to itself).
"""

from collections.abc import Mapping, Sequence
from typing import Literal, assert_never, final

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
    FileState,
    FloatState,
    ImageState,
    IntState,
    JsonState,
    ListState,
    StateDecl,
    StringState,
    TableState,
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
        case ImageState() | FileState() | TableState() | ListState() | JsonState():
            return None
        case _ as unreachable:
            assert_never(unreachable)


def _direct_values(expr: Expr) -> list[Expr]:
    """The expressions `expr` can evaluate to directly: itself, or the leaves of its `if`s."""
    found: list[Expr] = []
    stack: list[Expr] = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, If):
            stack.extend((node.otherwise, node.then))
        else:
            found.append(node)
    return found


def string_literals(expr: Expr) -> list[str]:
    """The string literals `expr` can evaluate to directly: itself, or through `if` branches.

    Anything else (a reference, a `concat`) is not known until it runs and contributes nothing.
    """
    return [node.value for node in _direct_values(expr) if isinstance(node, StrLit)]


def referenced_keys(expr: Expr) -> list[StateKey]:
    """The entries `expr` can evaluate to directly: itself if a `ref`, or through `if` branches."""
    return [node.key for node in _direct_values(expr) if isinstance(node, Ref)]


def _arithmetic_type(types: Sequence[ExprType]) -> ExprType:
    return "float" if "float" in types else "int"


type _Wanted = Literal["bool", "number", "string"]


@final
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

    def _enum_comparison(self, left: Expr, right: Expr, name: str) -> None:
        """An enum entry compared with literals it cannot have, or with another enum."""
        if isinstance(left, Ref) and isinstance(right, Ref):
            first, second = self._decls[left.key], self._decls[right.key]
            if (
                isinstance(first, EnumState)
                and isinstance(second, EnumState)
                and set(first.options).isdisjoint(second.options)
            ):
                raise InvalidValueError(
                    f"{name}: {left.key} and {right.key} are enums with no option in common"
                )
        for reference, other in ((left, right), (right, left)):
            if not isinstance(reference, Ref):
                continue
            decl = self._decls[reference.key]  # the operands were visited, so it exists
            if isinstance(decl, EnumState):
                for literal in string_literals(other):
                    if literal not in decl.options:
                        raise InvalidValueError(
                            f"{name}: {excerpt(literal)} is not an option of {reference.key}"
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
                self._enum_comparison(expr.left, expr.right, expr.kind)
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
