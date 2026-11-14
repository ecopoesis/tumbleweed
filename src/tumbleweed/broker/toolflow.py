"""The rules of opening a stored tool and of running one from the store (S24, D30).

Pure: no I/O, no clock, no transport. The service calls these where it holds the task and the
store; they are here so that each rule can be tested on its own.

- `open_refusal`: whether a stored version may be opened into a task, by taint (I12). The taint
  is `ToolRecord.taint`, the index's, never the package's own claim.
- `bound_payload`: the payload of a stored tool's handler run, made from the **stored** code (I8).
  The shell sends the handler and its inputs and nothing else; code it sends is a refusal.
"""

from typing import Final, Literal, final

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from tumbleweed.broker.task import TaintKind, merged_taint
from tumbleweed.sandbox.handler_protocol import handler_payload
from tumbleweed.tools.package import ToolPackage
from tumbleweed.ui.spec.handlers import SandboxHandler

type OpenRefusal = Literal["both_taints", "fs_in_net_task", "net_in_fs_task"]

# What the model is told: fixed text, no name, no hash.
OPEN_REFUSAL_TEXT: Final[dict[OpenRefusal, str]] = {
    "both_taints": (
        "That saved version carries both file and network taint (it came from a bundle that "
        "claims the network), so it cannot be opened in any task. Make a new tool instead."
    ),
    "fs_in_net_task": (
        "That saved version was made by a task that saw the user's files, and this task has the "
        "network, so it cannot be opened here. Call start_task with fresh_context=true, then "
        "open it again."
    ),
    "net_in_fs_task": (
        "That saved version was made by a task that used the network, and this task has seen the "
        "user's files, so it cannot be opened here. Call start_task with fresh_context=true, "
        "then open it again."
    ),
}


def open_refusal(
    task_taint: frozenset[TaintKind], version_taint: frozenset[TaintKind]
) -> OpenRefusal | None:
    """Why a version tainted `version_taint` cannot be opened into a task tainted `task_taint`
    (I12), or None. Checked before anything is recorded and with no prompt."""
    if len(version_taint) > 1:
        return "both_taints"
    if merged_taint(task_taint, version_taint) is not None:
        return None
    if TaintKind.FS in version_taint:
        return "fs_in_net_task"
    return "net_in_fs_task"


BoundFault = Literal["not_a_handler_call", "code_supplied", "no_such_handler", "too_large"]


@final
class BoundRunError(Exception):
    """A stored tool's run was not acceptable. `fault` is a closed code; the text never repeats
    what the request said."""

    def __init__(self, fault: BoundFault) -> None:
        super().__init__(fault)
        self.fault = fault


@final
class _Call(BaseModel):
    """What the shell may send for a stored tool: the handler, its inputs, and no code."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    module: str
    function: str
    inputs: dict[str, JsonValue]
    code: dict[str, str]


def bound_payload(package: ToolPackage, sent: str | None) -> str:
    """The payload of a run of `package`'s handler, whose `module`, `function` and inputs are in
    `sent` (the handler document with an empty `code`): the same document with the package's own
    modules in `code`.

    `BoundRunError` for a document that is not one, one that brings code of its own, a handler the
    package's spec does not declare, or one over the payload limit. The code that reaches the
    sandbox is only ever the package's.
    """
    if sent is None:
        raise BoundRunError("not_a_handler_call")
    try:
        call = _Call.model_validate_json(sent)
    except ValidationError:
        raise BoundRunError("not_a_handler_call") from None
    if call.code:
        raise BoundRunError("code_supplied")
    declared = any(
        isinstance(handler, SandboxHandler)
        and str(handler.module) == call.module
        and str(handler.function) == call.function
        for handler in package.ui.handlers
    )
    if not declared:
        raise BoundRunError("no_such_handler")
    document = handler_payload(
        call.module,
        call.function,
        call.inputs,
        {str(module.name): module.source for module in package.modules},
    )
    if document is None:
        raise BoundRunError("too_large")
    return document
