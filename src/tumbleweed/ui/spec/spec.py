"""The envelope of a UI spec, `tumbleweed.ui/1`, and the checks that span its parts.

A spec has a `version`, a `title` (shown on the canvas only, never in chrome: I5), the `state`
declarations, the component tree (`root`) and the `handlers`. Each part is validated by its own
models; what only the whole can check is checked here, when the spec is built, so that a
`UiSpec` that exists is consistent and the renderer never meets a dangling name:

- state keys and handler names are unique;
- every expression type-checks against the declarations (`check_expr`), and its type fits the
  entry it computes or is assigned to (`bool` to `bool`, `int` to `int`, `int` or `float` to
  `float`, `string` to `string` or to an `enum` entry; never a `float` to an `int`);
- computed state has no cycles;
- every binding names a declared entry of a kind the component accepts, and a two-way binding
  never targets a computed entry; sliders, spin buttons and progress bars need an entry with
  both bounds;
- every `on_click`, `on_change` and `on_activate` names a declared handler;
- `set` targets and `sandbox` outputs are declared, non-computed entries; `sandbox` inputs are
  declared entries;
- the component tree and the expressions are within their limits.

The problems found are all reported (up to `MAX_REPORTED_PROBLEMS`), one per line, each naming
the component (`root.children[2] (slider)`), state entry or handler it is about.
"""

from collections.abc import Collection, Iterator, Mapping
from typing import Annotated, Final, Literal, Self, assert_never

from pydantic import BeforeValidator, Field, model_validator

from tumbleweed.core.errors import InvalidValueError, excerpt
from tumbleweed.ui.spec.check import ExprType, check_expr, computed_order
from tumbleweed.ui.spec.components import (
    Box,
    Button,
    Check,
    Component,
    Dropdown,
    Entry,
    Expander,
    Grid,
    Heading,
    Image,
    Label,
    Progress,
    Separator,
    Slider,
    Spin,
    Switch,
    TableView,
    Tabs,
    TextView,
)
from tumbleweed.ui.spec.errors import MAX_REPORTED_PROBLEMS, SpecError
from tumbleweed.ui.spec.expr import Expr, StrLit, measure
from tumbleweed.ui.spec.handlers import AgentHandler, Handler, SandboxHandler, SetHandler
from tumbleweed.ui.spec.limits import (
    MAX_COMPONENT_DEPTH,
    MAX_COMPONENTS,
    MAX_HANDLERS,
    MAX_STATE_ENTRIES,
    MAX_TOTAL_EXPR_NODES,
)
from tumbleweed.ui.spec.model import SpecModel
from tumbleweed.ui.spec.names import HandlerName, StateKey
from tumbleweed.ui.spec.state import (
    BoolState,
    EnumState,
    FloatState,
    IntState,
    StateDecl,
    StateValue,
    StringState,
    check_value,
)
from tumbleweed.ui.spec.text import Title, as_tuple

SPEC_VERSION: Final = "tumbleweed.ui/1"


def _computed_expr(decl: StateDecl) -> Expr | None:
    return (
        decl.computed if isinstance(decl, BoolState | IntState | FloatState | StringState) else None
    )


def _accepts(decl: StateDecl, value_type: ExprType) -> bool:
    """Whether an expression of `value_type` may be assigned to the entry `decl` declares."""
    match decl:
        case BoolState():
            return value_type == "bool"
        case IntState():
            return value_type == "int"
        case FloatState():
            return value_type in {"int", "float"}
        case StringState() | EnumState():
            return value_type == "string"
        case _:
            return False


class _Problems:
    """Collects what is wrong with a spec, bounded so that a hostile spec costs little."""

    def __init__(self) -> None:
        self._found: list[str] = []

    def add(self, problem: str) -> None:
        # One more than `SpecError` reports, so that it knows there are more to say so.
        if len(self._found) <= MAX_REPORTED_PROBLEMS:
            self._found.append(problem)

    def raise_if_any(self) -> None:
        if self._found:
            raise InvalidValueError("\n".join(self._found))


def _walk(root: Component) -> Iterator[tuple[Component, str, int]]:
    """Every component with its path and depth, parents first, without recursion.

    Stops after `MAX_COMPONENTS + 1` components, so that an oversized tree is not walked whole.
    """
    stack: list[tuple[Component, str, int]] = [(root, "root", 1)]
    produced = 0
    while stack and produced <= MAX_COMPONENTS:
        component, path, depth = stack.pop()
        produced += 1
        yield component, path, depth
        children: list[tuple[Component, str]] = []
        match component:
            case Box() | Grid():
                children = [(c, f"{path}.children[{i}]") for i, c in enumerate(component.children)]
            case Tabs():
                children = [
                    (page.content, f"{path}.pages[{i}].content")
                    for i, page in enumerate(component.pages)
                ]
            case Expander():
                children = [(component.child, f"{path}.child")]
            case _:
                pass
        stack.extend((c, p, depth + 1) for c, p in reversed(children))


class UiSpec(SpecModel):
    """A complete UI spec. Build it with `parse_ui_spec` (JSON text) or `validate_ui_spec`."""

    version: Literal["tumbleweed.ui/1"]
    title: Title
    state: Annotated[
        tuple[StateDecl, ...], Field(max_length=MAX_STATE_ENTRIES), BeforeValidator(as_tuple)
    ] = ()
    root: Component
    handlers: Annotated[
        tuple[Handler, ...], Field(max_length=MAX_HANDLERS), BeforeValidator(as_tuple)
    ] = ()

    def declarations(self) -> dict[StateKey, StateDecl]:
        """The state declarations by key."""
        return {decl.key: decl for decl in self.state}

    def handlers_by_name(self) -> dict[HandlerName, Handler]:
        """The handlers by name."""
        return {handler.name: handler for handler in self.handlers}

    def computed_order(self) -> tuple[StateKey, ...]:
        """The computed entries in the order to evaluate them: each after those it reads."""
        return computed_order(self.state)

    def validate_patch(
        self, patch: Mapping[str, object], *, only: Collection[StateKey] | None = None
    ) -> dict[StateKey, StateValue]:
        """Check a patch of state values (from `patch_state` or a sandbox) against the spec.

        Every key must be a declared, non-computed entry (and in `only`, when given: a sandbox
        handler's declared outputs), and every value must pass `check_value` for its entry. All
        or nothing: if anything is wrong, `SpecError` lists the problems and no value is
        returned, so a caller never applies half a patch.
        """
        declarations = self.declarations()
        allowed = None if only is None else set(only)
        problems: list[str] = []
        checked: dict[StateKey, StateValue] = {}
        for raw_key, raw_value in patch.items():
            if len(problems) > MAX_REPORTED_PROBLEMS:
                break  # a hostile patch does not get to cost more than this
            try:
                key = StateKey(raw_key)
            except InvalidValueError as error:
                problems.append(str(error))
                continue
            decl = declarations.get(key)
            if decl is None:
                problems.append(f"{key} is not declared in the spec")
            elif _computed_expr(decl) is not None:
                problems.append(f"{key} is computed and cannot be written")
            elif allowed is not None and key not in allowed:
                problems.append(f"{key} is not one of the entries this may write")
            else:
                try:
                    checked[key] = check_value(decl, raw_value)
                except InvalidValueError as error:
                    problems.append(str(error))
        if problems:
            raise SpecError(problems)
        return checked

    @model_validator(mode="after")
    def _cross_references(self) -> Self:
        problems = _Problems()
        decls: dict[StateKey, StateDecl] = {}
        for decl in self.state:
            if decl.key in decls:
                problems.add(f"state key {decl.key} is declared twice")
            decls.setdefault(decl.key, decl)
        handler_names: dict[HandlerName, Handler] = {}
        for handler in self.handlers:
            if handler.name in handler_names:
                problems.add(f"handler {handler.name} is declared twice")
            handler_names.setdefault(handler.name, handler)

        nodes = 0
        for decl in self.state:
            expression = _computed_expr(decl)
            if expression is not None:
                nodes += measure(expression)[1]
                self._check_computed(decl, expression, decls, problems)
        try:
            computed_order(self.state)
        except InvalidValueError as error:
            problems.add(str(error))
        for handler in self.handlers:
            nodes += self._check_handler(handler, decls, problems)
        if nodes > MAX_TOTAL_EXPR_NODES:
            problems.add(
                f"the expressions have {nodes} nodes in all, the limit is {MAX_TOTAL_EXPR_NODES}"
            )
        self._check_components(decls, handler_names, problems)
        problems.raise_if_any()
        return self

    # --- expressions ---------------------------------------------------------------------

    @staticmethod
    def _check_computed(
        decl: StateDecl, expression: Expr, decls: Mapping[StateKey, StateDecl], problems: _Problems
    ) -> None:
        try:
            value_type = check_expr(expression, decls)
        except InvalidValueError as error:
            problems.add(f"state {decl.key} (computed): {error}")
            return
        if not _accepts(decl, value_type):
            problems.add(
                f"state {decl.key} (computed): an entry of kind {decl.kind} cannot be computed "
                f"from an expression of type {value_type}"
            )

    @staticmethod
    def _check_handler(
        handler: Handler, decls: Mapping[StateKey, StateDecl], problems: _Problems
    ) -> int:
        """Check one handler; return the number of expression nodes it holds."""
        nodes = 0
        match handler:
            case SetHandler():
                for assignment in handler.assignments:
                    nodes += measure(assignment.value)[1]
                    where = f"handler {handler.name}: assigning {assignment.target}"
                    target = decls.get(assignment.target)
                    if target is None:
                        problems.add(f"{where}: unknown state key")
                        continue
                    if _computed_expr(target) is not None:
                        problems.add(f"{where}: it is computed, so it cannot be assigned")
                        continue
                    try:
                        value_type = check_expr(assignment.value, decls)
                    except InvalidValueError as error:
                        problems.add(f"{where}: {error}")
                        continue
                    if not _accepts(target, value_type):
                        hint = " (use round for a whole number)" if target.kind == "int" else ""
                        problems.add(
                            f"{where}: an entry of kind {target.kind} cannot be assigned an "
                            f"expression of type {value_type}{hint}"
                        )
                    elif (
                        isinstance(target, EnumState)
                        and isinstance(assignment.value, StrLit)
                        and assignment.value.value not in target.options
                    ):
                        problems.add(
                            f"{where}: {excerpt(assignment.value.value)} is not one of its options"
                        )
            case SandboxHandler():
                for key in handler.inputs:
                    if key not in decls:
                        problems.add(f"handler {handler.name}: input {key} is not declared")
                for key in handler.outputs:
                    output = decls.get(key)
                    if output is None:
                        problems.add(f"handler {handler.name}: output {key} is not declared")
                    elif _computed_expr(output) is not None:
                        problems.add(
                            f"handler {handler.name}: output {key} is computed, so a sandbox "
                            "cannot write it"
                        )
            case AgentHandler():
                pass
            case _ as unreachable:
                assert_never(unreachable)
        return nodes

    # --- components ----------------------------------------------------------------------

    def _check_components(
        self,
        decls: Mapping[StateKey, StateDecl],
        handler_names: Mapping[HandlerName, Handler],
        problems: _Problems,
    ) -> None:
        for count, (component, path, depth) in enumerate(_walk(self.root), start=1):
            if count > MAX_COMPONENTS:
                problems.add(f"the component tree has more than {MAX_COMPONENTS} components")
                return
            if depth > MAX_COMPONENT_DEPTH:
                problems.add(f"{path} is nested {depth} deep, the limit is {MAX_COMPONENT_DEPTH}")
                return
            _check_component(
                component, f"{path} ({component.kind})", decls, handler_names, problems
            )


def _bind(
    where: str,
    key: StateKey,
    *,
    decls: Mapping[StateKey, StateDecl],
    problems: _Problems,
    kinds: tuple[str, ...],
    writes: bool = False,
    bounded: bool = False,
) -> None:
    decl = decls.get(key)
    if decl is None:
        problems.add(f"{where}: binds unknown state key {key}")
        return
    if decl.kind not in kinds:
        problems.add(
            f"{where}: binds {key}, whose kind is {decl.kind}; it needs {' or '.join(kinds)}"
        )
        return
    if writes and _computed_expr(decl) is not None:
        problems.add(f"{where}: binds {key} two-way, but it is computed and cannot be written")
    if bounded and not (
        isinstance(decl, IntState | FloatState)
        and decl.minimum is not None
        and decl.maximum is not None
    ):
        problems.add(f"{where}: {key} needs both a minimum and a maximum")


def _handler(
    where: str,
    name: HandlerName | None,
    handler_names: Mapping[HandlerName, Handler],
    problems: _Problems,
) -> None:
    if name is not None and name not in handler_names:
        problems.add(f"{where}: refers to unknown handler {name}")


def _check_component(
    component: Component,
    where: str,
    decls: Mapping[StateKey, StateDecl],
    handler_names: Mapping[HandlerName, Handler],
    problems: _Problems,
) -> None:
    def bind(key: StateKey, *kinds: str, writes: bool = False, bounded: bool = False) -> None:
        _bind(
            where, key, decls=decls, problems=problems, kinds=kinds, writes=writes, bounded=bounded
        )

    def handler(name: HandlerName | None) -> None:
        _handler(where, name, handler_names, problems)

    match component:
        case Box() | Grid() | Tabs() | Expander() | Separator():
            pass
        case Label() | Heading():
            if component.bind is not None:
                bind(component.bind, "string", "enum")
        case Progress():
            bind(component.bind, "int", "float", bounded=True)
        case Image():
            bind(component.bind, "image")
        case TableView():
            bind(component.bind, "table")
        case Button():
            handler(component.on_click)
        case Entry():
            bind(component.bind, "string", writes=True)
            handler(component.on_change)
            handler(component.on_activate)
        case Switch() | Check():
            bind(component.bind, "bool", writes=True)
            handler(component.on_change)
        case Slider() | Spin():
            bind(component.bind, "int", "float", writes=True, bounded=True)
            handler(component.on_change)
        case Dropdown():
            bind(component.bind, "enum", writes=True)
            handler(component.on_change)
        case TextView():
            bind(component.bind, "string", writes=component.editable)
            handler(component.on_change)
        case _ as unreachable:
            assert_never(unreachable)
