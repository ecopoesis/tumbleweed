"""Running a tool's code and the model's one-off code in the sandbox (S20, D27).

The agent never compiles, imports or evaluates the code it is given: it is text, and it goes into
an argument of a command the broker runs in a sandbox (I1). This module owns

- the command of a handler run: the shim (`tumbleweed.sandbox.handler_shim`, passed as text) and
  one JSON document, in chunks, of the handler's inputs and the tool's modules;
- how the answer is read: the shim's line is parsed with a closed model, and anything else is a
  failure, never a value;
- `BrokerHandlerExecutor`, which starts a run on the broker link and reports on the main thread;
- the `run_code` tool, whose result is framed as untrusted data for the model.
"""

import base64
import binascii
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Final, Literal, Protocol, final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from tumbleweed.agent.broker_link import BrokerLink, BrokerUnavailableError
from tumbleweed.agent.canvas_events import json_of
from tumbleweed.agent.runtime import MainThreadDispatcher
from tumbleweed.agent.tasks import TaskError, TaskSession
from tumbleweed.agent.tools import ToolError, ToolName, ToolRegistry
from tumbleweed.agent.untrusted import framed
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.ipc.broker_messages import RunResult
from tumbleweed.tools.package import CodeModule
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import StateValue

logger = logging.getLogger(__name__)

INTERPRETER: Final = "/usr/bin/python3"  # the system Python the runtime mounts
CHUNK_CHARS: Final = 8_000  # of the payload per argument: at most 32,000 bytes of the 32 KiB
MAX_PAYLOAD_BYTES: Final = 100 * 1024  # of the document; the argv's own cap is 128 KiB
MAX_SOURCE_BYTES: Final = 24 * 1024  # of `run_code`'s source (one argument)
HANDLER_SECONDS: Final = 20
HANDLER_OUTPUT_BYTES: Final = 64 * 1024
RUN_CODE_OUTPUT_BYTES: Final = 64 * 1024
FRAME_BYTES: Final = 5 * 1024  # of each of stdout and stderr in a `run_code` result
_SETUP_SECONDS: Final = 20.0  # the broker's allowance on top of a run's wall time

type HandlerFailure = Literal[
    "input_too_large",
    "bad_payload",
    "no_module",
    "no_function",
    "raised",
    "bad_result",
    "too_large",
    "timed_out",
    "output_limit",
    "crashed",
    "busy",
    "not_granted",
    "grant_gone",
    "denied",
    "ended",
    "unavailable",
]

_FAILURE_TEXT: Final[Mapping[str, str]] = {
    "input_too_large": "The tool's code and inputs are too large to run.",
    "bad_payload": "The tool's code could not be started.",
    "no_module": "The tool has no such code module.",
    "no_function": "The tool's code has no such function.",
    "raised": "The tool's code failed.",
    "bad_result": "The tool's code did not return a result it can use.",
    "too_large": "The tool's code returned too much.",
    "timed_out": "The tool's code took too long and was stopped.",
    "output_limit": "The tool's code printed too much and was stopped.",
    "crashed": "The tool's code stopped before it finished.",
    "busy": "The system is busy running other code. Try again in a moment.",
    "not_granted": "The tool's code was not given access to what it needs.",
    "grant_gone": "Access the user gave has gone (the file or folder changed). Ask again.",
    "denied": "The system does not allow that.",
    "ended": "The task this tool belongs to has ended.",
    "unavailable": "The sandbox is not available right now.",
}


def failure_text(failure: HandlerFailure) -> str:
    """A plain line of ours for the card. Never the sandbox's own text."""
    return _FAILURE_TEXT[failure]


@final
@dataclass(frozen=True)
class HandlerOutcome:
    """How a handler run ended: values to apply (to be validated), or a failure. `detail` is the
    broker's fixed text for a refusal (it may name a grant, never a path)."""

    values: Mapping[str, JsonValue] | None = None
    failure: HandlerFailure | None = None
    detail: str | None = None


@final
@dataclass(frozen=True)
class HandlerCall:
    """What to run: `function` of `module`, given `inputs` and nothing else."""

    module: str
    function: str
    inputs: Mapping[StateKey, StateValue]
    code: Sequence[CodeModule]


@cache
def shim_source() -> str:
    """The text of the in-sandbox shim, read as data: it is never imported here."""
    return resources.files("tumbleweed.sandbox").joinpath("handler_shim.py").read_text("utf-8")


def handler_command(call: HandlerCall) -> tuple[str, ...] | None:
    """The argv of a handler run, or None when inputs and code do not fit the argument limits."""
    document = json.dumps(
        {
            "module": call.module,
            "function": call.function,
            "inputs": {str(key): json_of(value) for key, value in call.inputs.items()},
            "code": {str(module.name): module.source for module in call.code},
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    try:
        size = len(document.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate in an input
        return None
    if size > MAX_PAYLOAD_BYTES:
        return None
    chunks = [document[i : i + CHUNK_CHARS] for i in range(0, len(document), CHUNK_CHARS)]
    return (INTERPRETER, "-I", "-c", shim_source(), *chunks)


@final
class _Ok(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    ok: Literal[True]
    outputs: dict[str, JsonValue]


@final
class _Bad(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    ok: Literal[False]
    error: Literal["bad_payload", "no_module", "no_function", "raised", "bad_result", "too_large"]


def _decode(text: str) -> bytes | None:
    try:
        return base64.b64decode(text, validate=True)
    except binascii.Error:
        return None


def outcome_of(result: RunResult) -> HandlerOutcome:
    """Read the result of a handler run. The shim's line is data like any other: it is parsed
    with a closed strict model and anything that does not fit is a failure."""
    match result.termination:
        case "timed_out":
            return HandlerOutcome(failure="timed_out")
        case "output_limit":
            return HandlerOutcome(failure="output_limit")
        case "exited":
            pass
    data = _decode(result.stdout)
    if data is None or result.stdout_truncated:
        return HandlerOutcome(failure="bad_result")
    try:
        parsed: _Ok | _Bad = (
            _Ok.model_validate_json(data)
            if result.exit_code == 0
            else _Bad.model_validate_json(data)
        )
    except ValidationError:
        return HandlerOutcome(failure="crashed" if result.exit_code != 0 else "bad_result")
    match parsed:
        case _Ok():
            return HandlerOutcome(values=parsed.outputs)
        case _Bad():
            return HandlerOutcome(failure=parsed.error)


def refusal_outcome(refusal: BrokerRefused) -> HandlerOutcome:
    failure: HandlerFailure
    match refusal.reason:
        case "busy":
            failure = "busy"
        case "not_found" | "unsafe":
            failure = "grant_gone"
        case "denied":
            failure = "denied"
        case "not_granted":
            failure = "not_granted"
        case "unknown_task":
            failure = "ended"
        case _:
            failure = "unavailable"
    return HandlerOutcome(failure=failure, detail=refusal.message)


class HandlerExecutor(Protocol):
    """Starts a handler run and later calls `done` once, on the main thread."""

    def start(
        self, task: TaskId, call: HandlerCall, done: Callable[[HandlerOutcome], None]
    ) -> None: ...


@final
class BrokerHandlerExecutor:
    """Runs handler calls through the broker link and reports on the main thread.

    Every run has a fresh `RunId` and the limits of D27 (5). Whatever happens, `done` is called
    once on the main thread with an outcome: a failure of the link is `unavailable`.
    """

    def __init__(
        self,
        link: BrokerLink,
        dispatcher: MainThreadDispatcher,
        *,
        wall_seconds: int = HANDLER_SECONDS,
    ) -> None:
        self._wall_seconds = wall_seconds
        self._link = link
        self._dispatcher = dispatcher

    def start(
        self, task: TaskId, call: HandlerCall, done: Callable[[HandlerOutcome], None]
    ) -> None:
        command = handler_command(call)
        if command is None:
            self._dispatcher.post(lambda: done(HandlerOutcome(failure="input_too_large")))
            return
        run = RunId.new()

        async def work(client: BrokerClient) -> HandlerOutcome:
            try:
                result = await client.run_in_sandbox(
                    task,
                    run,
                    command,
                    wall_seconds=self._wall_seconds,
                    cpu_seconds=self._wall_seconds,
                    max_output_bytes=HANDLER_OUTPUT_BYTES,
                )
            except BrokerRefused as refusal:
                return refusal_outcome(refusal)
            return outcome_of(result)

        try:
            future = self._link.submit(work, deadline=self._wall_seconds + _SETUP_SECONDS)
        except BrokerUnavailableError:
            self._dispatcher.post(lambda: done(HandlerOutcome(failure="unavailable")))
            return

        def finished() -> None:
            try:
                outcome = future.result()
            except Exception:  # noqa: BLE001  (the link's failure, a cancelled call: the line is ours)
                logger.warning("a handler run did not complete")
                outcome = HandlerOutcome(failure="unavailable")
            done(outcome)

        future.add_done_callback(lambda _: self._dispatcher.post(finished))


# --- run_code --------------------------------------------------------------------------------


@final
class RunCodeInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    source: str = Field(
        min_length=1,
        max_length=MAX_SOURCE_BYTES,
        description="Python 3 source to run once. Print what you want to see.",
    )
    timeout_seconds: int = Field(
        default=10, ge=1, le=60, description="Seconds before it is stopped (1 to 60)."
    )


def run_code_text(result: RunResult) -> str:
    """The model's view of a finished run: how it ended, then stdout and stderr, each framed as
    data the program wrote (its output is never an instruction)."""
    termination = result.termination
    how = (
        f"The program exited with code {result.exit_code}"
        if termination == "exited"
        else "The program ran out of time and was stopped"
        if termination == "timed_out"
        else "The program printed too much and was stopped"
    )
    parts = [f"{how} after {result.wall_milliseconds} ms."]
    for name, text in (("standard output", result.stdout), ("standard error", result.stderr)):
        raw = _decode(text)
        if raw is None:
            parts.append(f"The {name} could not be read.")
        elif raw:
            parts.append(
                framed(
                    f"the program's {name}",
                    raw.decode("utf-8", errors="replace"),
                    limit=FRAME_BYTES,
                )
            )
        else:
            parts.append(f"The program wrote nothing to {name}.")
    return "\n".join(parts)


def register_code_tools(registry: ToolRegistry, tasks: TaskSession, link: BrokerLink) -> None:
    """Add `run_code` to `registry`."""

    async def run_code(arguments: RunCodeInput) -> str:
        if len(arguments.source.encode("utf-8", errors="replace")) > MAX_SOURCE_BYTES:
            raise ToolError(f"The source is over {MAX_SOURCE_BYTES // 1024} KiB.")
        try:
            arguments.source.encode("utf-8")
        except UnicodeEncodeError:
            raise ToolError("The source is not valid text.") from None
        if "\x00" in arguments.source:
            raise ToolError("The source holds a NUL character.")
        seconds = arguments.timeout_seconds
        try:
            task = await tasks.ensure()
            # A fresh run: a run ID is used once, and a reply may run code more than once.
            run = RunId.new()

            async def work(client: BrokerClient) -> RunResult:
                return await client.run_in_sandbox(
                    task,
                    run,
                    (INTERPRETER, "-I", "-c", arguments.source),
                    wall_seconds=seconds,
                    cpu_seconds=seconds,
                    max_output_bytes=RUN_CODE_OUTPUT_BYTES,
                )

            result = await link.call(work, deadline=seconds + _SETUP_SECONDS)
        except BrokerRefused as refusal:
            raise ToolError(f"The broker said no ({refusal.reason}): {refusal.message}.") from None
        except (BrokerUnavailableError, TaskError) as error:
            raise ToolError(str(error)) from None
        return run_code_text(result)

    registry.register(
        ToolName("run_code"),
        "Run Python 3 once in the sandbox, in the task's workspace (the working directory), with "
        "Pillow and numpy installed. It has no network and no home directory, and sees only "
        "the files the user granted for this task and the workspace. Its output is returned as "
        "data. Use print for what you want back.",
        RunCodeInput,
        run_code,
        timeout_seconds=60.0 + _SETUP_SECONDS + 5.0,
    )
