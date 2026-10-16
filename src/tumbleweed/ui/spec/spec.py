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
- sliders, spin buttons and progress bars have a sane range and step (`_range_problem`);
- the component tree and the expressions are within their limits, and the text in the defaults of
  all the state is within `MAX_STATE_TEXT_BYTES`.

The problems found are all reported (up to `MAX_REPORTED_PROBLEMS`), one per line, each naming
the component (`root.children[2] (slider)`), state entry or handler it is about.
"""

import sys
from collections.abc import Collection, Iterator, Mapping
from typing import Annotated, Final, Literal, Self, assert_never, final

from pydantic import BeforeValidator, Field, model_validator

from tumbleweed.core.errors import InvalidValueError, excerpt
from tumbleweed.ui.spec.check import (
    ExprType,
    check_expr,
    computed_order,
    referenced_keys,
    string_literals,
)
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
from tumbleweed.ui.spec.expr import Expr, measure
from tumbleweed.ui.spec.handlers import AgentHandler, Handler, SandboxHandler, SetHandler
from tumbleweed.ui.spec.limits import (
    MAX_COMPONENT_DEPTH,
    MAX_COMPONENTS,
    MAX_HANDLERS,
    MAX_PATCH_TEXT_BYTES,
    MAX_SAFE_INT,
    MAX_STATE_ENTRIES,
    MAX_STATE_TEXT_BYTES,
    MAX_TOTAL_EXPR_NODES,
)
from tumbleweed.ui.spec.model import SpecModel
from tumbleweed.ui.spec.names import HandlerName, StateKey
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
    StateValue,
    StringState,
    TableState,
    check_value,
    value_text_bytes,
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
        case ImageState() | FileState() | TableState() | ListState() | JsonState():
            return False
        case _ as unreachable:
            assert_never(unreachable)


def _budget_problems(
    patch: Mapping[StateKey, StateValue], current: Mapping[StateKey, StateValue]
) -> list[str]:
    """The patch's text against `MAX_PATCH_TEXT_BYTES`; the text of the state after it against
    `MAX_STATE_TEXT_BYTES`, but only if the patch makes the state's text grow.

    A state that is over its budget already (a tool whose strings were allowed to grow until they
    filled it, or whose spec declares many large strings) must still be able to shrink and to
    change what does not hold text, or the tool would be wedged: so only a patch that raises the
    state's text, and ends over the budget, is refused.
    """
    size = sum(value_text_bytes(value) for value in patch.values())
    if size > MAX_PATCH_TEXT_BYTES:
        return [f"the patch holds {size} bytes of text, the limit is {MAX_PATCH_TEXT_BYTES}"]
    before = sum(value_text_bytes(value) for value in current.values())
    replaced = sum(value_text_bytes(current[key]) for key in patch if key in current)
    after = before - replaced + size
    if after > MAX_STATE_TEXT_BYTES and after > before:
        return [
            (
                f"the patch would grow the text of the state from {before} to {after} bytes, "
                f"over the limit of {MAX_STATE_TEXT_BYTES}"
            )
        ]
    return []


@final
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
            case (
                Label()
                | Heading()
                | Button()
                | Entry()
                | Switch()
                | Check()
                | Slider()
                | Spin()
                | Dropdown()
                | Progress()
                | Separator()
                | Image()
                | TableView()
                | TextView()
            ):
                pass
            case _ as unreachable:
                assert_never(unreachable)
        stack.extend((c, p, depth + 1) for c, p in reversed(children))


@final
class UiSpec(SpecModel):
    """A complete UI spec. Build it with `parse_ui_spec` (JSON text) or `validate_ui_spec`."""

    version: Literal["tumbleweed.ui/1"]
    title: Title
    state: Annotated[
        tuple[StateDecl, ...],
        Field(max_length=MAX_STATE_ENTRIES, fail_fast=True),
        BeforeValidator(as_tuple),
    ] = ()
    root: Component
    handlers: Annotated[
        tuple[Handler, ...],
        Field(max_length=MAX_HANDLERS, fail_fast=True),
        BeforeValidator(as_tuple),
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
        self,
        patch: Mapping[str, object],
        *,
        current: Mapping[StateKey, StateValue],
        only: Collection[StateKey] | None = None,
    ) -> dict[StateKey, StateValue]:
        """Check a patch of state values (from `patch_state` or a sandbox) against the spec.

        Every key must be a declared, non-computed entry (and in `only`, when given: a sandbox
        handler's declared outputs), and every value must pass `check_value` for its entry. The
        text in the patch is at most `MAX_PATCH_TEXT_BYTES` bytes, and the state after the patch
        may not grow its text beyond `MAX_STATE_TEXT_BYTES`: a tool cannot grow its state without
        limit by patching it again and again. `current` is the whole live state, computed
        entries included (their values are text in the state like any other, and the check of
        the defaults counts them), with the patch's values replacing theirs; pass `{}` for an
        empty state. A patch that does not raise the text of the state is never refused for the
        budget, even over it, so that a state that got there can still shrink. All or nothing:
        if anything is wrong, `SpecError` lists the problems and no value is returned, so a
        caller never applies half a patch.
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
        if not problems:
            problems.extend(_budget_problems(checked, current))
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

        text = sum(value_text_bytes(decl.default) for decl in self.state)
        if text > MAX_STATE_TEXT_BYTES:
            problems.add(
                f"the defaults of the state hold {text} bytes of text, the limit is "
                f"{MAX_STATE_TEXT_BYTES}"
            )

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
                    elif isinstance(target, EnumState):
                        # A literal or an enum entry the expression can produce, directly or
                        # through `if`: a literal must be an option, an enum entry must share
                        # one (as `eq` of two enums must), or the assignment never takes effect.
                        for literal in string_literals(assignment.value):
                            if literal not in target.options:
                                problems.add(
                                    f"{where}: {excerpt(literal)} is not one of its options"
                                )
                                break
                        for key in referenced_keys(assignment.value):
                            source = decls.get(key)
                            if isinstance(source, EnumState) and set(source.options).isdisjoint(
                                target.options
                            ):
                                problems.add(
                                    f"{where}: {key} is an enum with no option in common with "
                                    f"{assignment.target}"
                                )
                                break
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


def _range_problem(decl: IntState | FloatState, *, stepped: bool) -> str | None:
    """Why `decl` cannot be the range of a slider, spin button or progress bar, or None.

    These are rules of numeric safety, not of taste. The widget needs both bounds, finite and
    within +-`MAX_SAFE_INT`, with `minimum` below `maximum` (a zero-width range has no positions,
    and a progress bar divides by it) and a range at most `MAX_SAFE_INT` wide, so that nothing
    the widget computes overflows or loses its integers. A slider or spin button (`stepped`)
    also needs a `step` that is a normal float (not subnormal, whose arithmetic underflows) and
    no wider than the range. How many positions that makes is the author's business: a step of
    0.01 over a range of a million is a fine slider. A progress bar has no step.
    """
    minimum, maximum = decl.minimum, decl.maximum
    if minimum is None or maximum is None:
        return "needs both a minimum and a maximum"
    if abs(minimum) > MAX_SAFE_INT or abs(maximum) > MAX_SAFE_INT:
        return f"needs bounds within +-{MAX_SAFE_INT}"
    if not minimum < maximum:
        return "needs a minimum below its maximum"
    if maximum - minimum > MAX_SAFE_INT:
        return f"needs a range at most {MAX_SAFE_INT} wide"
    if stepped and decl.step is not None:
        if decl.step < sys.float_info.min:
            return "needs a step that is not subnormal"
        if decl.step > maximum - minimum:
            return "needs a step no wider than its range"
    return None


def _bind(
    where: str,
    key: StateKey,
    *,
    decls: Mapping[StateKey, StateDecl],
    problems: _Problems,
    kinds: tuple[str, ...],
    writes: bool = False,
    bounded: bool = False,
    stepped: bool = False,
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
    if bounded and isinstance(decl, IntState | FloatState):
        problem = _range_problem(decl, stepped=stepped)
        if problem is not None:
            problems.add(f"{where}: {key} {problem}")


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
    def bind(
        key: StateKey,
        *kinds: str,
        writes: bool = False,
        bounded: bool = False,
        stepped: bool = False,
    ) -> None:
        _bind(
            where,
            key,
            decls=decls,
            problems=problems,
            kinds=kinds,
            writes=writes,
            bounded=bounded,
            stepped=stepped,
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
            bind(component.bind, "int", "float", writes=True, bounded=True, stepped=True)
            handler(component.on_change)
        case Dropdown():
            bind(component.bind, "enum", writes=True)
            handler(component.on_change)
        case TextView():
            bind(component.bind, "string", writes=component.editable)
            handler(component.on_change)
            if component.on_change is not None and not component.editable:
                problems.add(f"{where}: on_change needs editable: a read-only view has no edits")
        case _ as unreachable:
            assert_never(unreachable)
