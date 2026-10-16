"""Handlers: what happens when the user acts on a component.

A component names a handler (`on_click`, `on_change`, `on_activate`) and the handler is one of
three kinds, discriminated by `kind`:

- `set` assigns the results of expressions to state entries, in the renderer, immediately. The
  expressions are all evaluated against the state as it was when the event fired, and then the
  assignments are applied together, so no assignment sees another's result; two assignments to
  one target are refused because their order would matter.
- `sandbox` calls `function` in the tool's code `module`, inside a sandbox (S20). The call gets
  the `inputs` (state entries, as JSON) and nothing else. Its result may write only the
  `outputs`, and is validated against their declarations before any is applied. `debounce_ms`
  waits for that long after the last event before calling; 0 calls at once. At most one call
  per tool instance runs at a time (I9).
- `agent` sends the event and a snapshot of the state to the agent as untrusted input (S12).
  `note` is the author's hint to the agent about what the event means; it is data too.

Handlers run only when a user acts on a widget. A change of state from anywhere else (a
computed entry updating, a `set` or sandbox result, `patch_state`) never fires a handler, so
handlers cannot trigger one another.
"""

from typing import Annotated, Literal, Self, final

from pydantic import BeforeValidator, Field, model_validator

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.ui.spec.expr import Expr
from tumbleweed.ui.spec.limits import MAX_DEBOUNCE_MS, MAX_HANDLER_ITEMS
from tumbleweed.ui.spec.model import SpecModel
from tumbleweed.ui.spec.names import FunctionName, HandlerName, ModuleName, StateKey
from tumbleweed.ui.spec.text import Block, as_tuple


def _distinct(keys: tuple[StateKey, ...], what: str) -> None:
    if len(set(keys)) != len(keys):
        raise InvalidValueError(f"{what} must not repeat a state key")


@final
class SetAssignment(SpecModel):
    """`target` becomes the value of `value`."""

    target: StateKey
    value: Expr


@final
class SetHandler(SpecModel):
    """Assign expression results to state entries."""

    kind: Literal["set"]
    name: HandlerName
    assignments: Annotated[
        tuple[SetAssignment, ...],
        Field(min_length=1, max_length=MAX_HANDLER_ITEMS, fail_fast=True),
        BeforeValidator(as_tuple),
    ]

    @model_validator(mode="after")
    def _targets_are_distinct(self) -> Self:
        _distinct(tuple(a.target for a in self.assignments), "a set handler")
        return self


@final
class SandboxHandler(SpecModel):
    """Call a function of one of the tool's code modules in a sandbox."""

    kind: Literal["sandbox"]
    name: HandlerName
    module: ModuleName
    function: FunctionName
    inputs: Annotated[
        tuple[StateKey, ...],
        Field(max_length=MAX_HANDLER_ITEMS, fail_fast=True),
        BeforeValidator(as_tuple),
    ] = ()
    outputs: Annotated[
        tuple[StateKey, ...],
        Field(min_length=1, max_length=MAX_HANDLER_ITEMS, fail_fast=True),
        BeforeValidator(as_tuple),
    ]
    debounce_ms: Annotated[int, Field(ge=0, le=MAX_DEBOUNCE_MS)] = 0

    @model_validator(mode="after")
    def _keys_are_distinct(self) -> Self:
        _distinct(self.inputs, "the inputs of a sandbox handler")
        _distinct(self.outputs, "the outputs of a sandbox handler")
        return self


@final
class AgentHandler(SpecModel):
    """Send the event and the state to the agent."""

    kind: Literal["agent"]
    name: HandlerName
    note: Block | None = None


type Handler = Annotated[SetHandler | SandboxHandler | AgentHandler, Field(discriminator="kind")]
