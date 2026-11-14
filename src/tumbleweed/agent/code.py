"""Running a tool's code and the model's one-off code in the sandbox (S20, D27).

The agent never compiles, imports or evaluates the code it is given: it is text, and it goes into
the payload file of a run the broker makes in a sandbox (I1). The command, the payload and the
reading of the shim's answer are `tumbleweed.sandbox.handler_protocol`, so that the broker can
build the same run from stored code (S24). This module owns

- the handler call as the canvas has it (`HandlerCall`) and how it becomes a run;
- `BrokerHandlerExecutor`, which starts a run on the broker link and reports on the main thread;
- the `run_code` tool, whose result is framed as untrusted data for the model.
"""

import base64
import binascii
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol, assert_never, final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from tumbleweed.agent.broker_link import BrokerLink, BrokerUnavailableError
from tumbleweed.agent.canvas import StoredBinding
from tumbleweed.agent.canvas_events import json_of
from tumbleweed.agent.runtime import MainThreadDispatcher
from tumbleweed.agent.tasks import TaskError, TaskSession
from tumbleweed.agent.tools import ToolError, ToolName, ToolRegistry
from tumbleweed.agent.untrusted import PROGRAM_INTRO, framed
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.config import RUN_SETUP_WAIT
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.ipc.broker_messages import RunOrigin, RunResult, WireBinding
from tumbleweed.sandbox.handler_protocol import (
    handler_command,
    handler_payload,
    read_shim_line,
    script_command,
)
from tumbleweed.tools.package import CodeModule
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import StateValue
from tumbleweed.ui.spec.text import text_bytes

logger = logging.getLogger(__name__)

MAX_SOURCE_BYTES: Final = 24 * 1024  # of `run_code`'s source
HANDLER_SECONDS: Final = 20
HANDLER_OUTPUT_BYTES: Final = 64 * 1024
RUN_CODE_OUTPUT_BYTES: Final = 64 * 1024
FRAME_BYTES: Final = 5 * 1024  # of each of stdout and stderr in a `run_code` result
_SETUP_SECONDS: Final = RUN_SETUP_WAIT.total_seconds() + 20.0
"""The broker's allowance on top of a run's wall time: the launcher's own, and the broker's setup,
which for a run that can write includes its snapshot and the user's answer to "run without undo?"
(S21, D28 H13: derived from the broker's constants, so the two cannot drift apart)."""

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
    "run_cap",
    "rejected",
    "not_granted",
    "grant_gone",
    "denied",
    "ended",
    "unavailable",
    "no_undo",
    "workspace_full",
]


def failure_text(failure: HandlerFailure) -> str:
    """A plain line of ours for the card. Never the sandbox's or the broker's own text."""
    match failure:
        case "input_too_large":
            return "The tool's code and inputs are too large to run."
        case "bad_payload":
            return "The tool's code could not be started."
        case "no_module":
            return "The tool has no such code module."
        case "no_function":
            return "The tool's code has no such function."
        case "raised":
            return "The tool's code failed."
        case "bad_result":
            return "The tool's code did not return a result it can use."
        case "too_large":
            return "The tool's code returned too much."
        case "timed_out":
            return "The tool's code took too long and was stopped."
        case "output_limit":
            return "The tool's code printed too much and was stopped."
        case "crashed":
            return "The tool's code stopped before it finished."
        case "busy":
            return "The system is busy running other code. Try again in a moment."
        case "run_cap":
            return "This task has used all the code runs it may. Start a new task to run more."
        case "rejected":
            return "The system would not run that code."
        case "not_granted":
            return (
                "The tool's code was not given access to what it needs. Access allowed for "
                "one reply only does not reach code: allow it for the task."
            )
        case "grant_gone":
            return "Access you gave to a file or folder has changed. Ask for it again."
        case "denied":
            return "The system does not allow that."
        case "ended":
            return "The task this tool belongs to has ended."
        case "unavailable":
            return "The sandbox is not available right now."
        case "no_undo":
            return (
                "That code could change your files and no copy for undo could be saved, so it "
                "did not run."
            )
        case "workspace_full":
            return "This task's workspace is full. Start a new task to run more."
        case _ as unreachable:
            assert_never(unreachable)


@final
@dataclass(frozen=True)
class HandlerOutcome:
    """How a handler run ended: values to apply (to be validated), or a failure."""

    values: Mapping[str, JsonValue] | None = None
    failure: HandlerFailure | None = None


@final
@dataclass(frozen=True)
class HandlerCall:
    """What to run: `function` of `module`, given `inputs` and nothing else."""

    module: str
    function: str
    inputs: Mapping[StateKey, StateValue]
    code: Sequence[CodeModule]
    stored: StoredBinding | None = None
    """For a tool opened from the broker's store (S24): the run carries no code, the broker puts in
    the stored code, and the run is a user's (a handler runs only on a direct input event)."""


def handler_run(call: HandlerCall) -> tuple[tuple[str, ...], str] | None:
    """The argv and the payload of a handler run, or None when they do not fit the limits."""
    payload = handler_payload(
        call.module,
        call.function,
        {str(key): json_of(value) for key, value in call.inputs.items()},
        {}
        if call.stored is not None
        else {str(module.name): module.source for module in call.code},
    )
    return None if payload is None else (handler_command(), payload)


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
    line = read_shim_line(data, result.exit_code)
    if line is None:
        return HandlerOutcome(failure="crashed" if result.exit_code != 0 else "bad_result")
    if line.error is not None:
        return HandlerOutcome(failure=line.error)
    return HandlerOutcome(values=line.outputs)


def refusal_outcome(refusal: BrokerRefused) -> HandlerOutcome:
    failure: HandlerFailure
    match refusal.reason:
        case "busy":
            failure = "busy"
        case "exhausted":
            failure = "run_cap"
        case "invalid_request" | "too_large":
            failure = "rejected"
        case "not_found" | "unsafe":
            failure = "grant_gone"
        case "denied":
            failure = "denied"
        case "not_granted":
            failure = "not_granted"
        case "unknown_task":
            failure = "ended"
        case "no_undo":
            failure = "no_undo"
        case "workspace_full":
            failure = "workspace_full"
        case _:
            failure = "unavailable"
    return HandlerOutcome(failure=failure)


class HandlerExecutor(Protocol):
    """Starts a handler run and later calls `done` once on the main thread (unless the run is
    cancelled first). Returns the function that cancels it."""

    def start(
        self, task: TaskId, call: HandlerCall, done: Callable[[HandlerOutcome], None]
    ) -> Callable[[], None]: ...


@final
class BrokerHandlerExecutor:
    """Runs handler calls through the broker link and reports on the main thread.

    Every run has a fresh `RunId` and the limits of D27 (5). Whatever happens, `done` is called
    once on the main thread with an outcome (a failure of the link is `unavailable`), unless the
    run was cancelled: the answer of a cancelled run is not waited for. The broker has no way to
    cancel a run yet, so a cancelled run keeps its slot until it ends (at most its wall time).
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
    ) -> Callable[[], None]:
        prepared = handler_run(call)
        if prepared is None:
            self._dispatcher.post(lambda: done(HandlerOutcome(failure="input_too_large")))
            return _nothing
        command, payload = prepared
        run = RunId.new()
        origin = _origin_of(call.stored)

        async def work(client: BrokerClient) -> HandlerOutcome:
            try:
                result = await client.run_in_sandbox(
                    task,
                    run,
                    command,
                    wall_seconds=self._wall_seconds,
                    cpu_seconds=self._wall_seconds,
                    max_output_bytes=HANDLER_OUTPUT_BYTES,
                    payload=payload,
                    origin=origin,
                )
            except BrokerRefused as refusal:
                return refusal_outcome(refusal)
            return outcome_of(result)

        try:
            future = self._link.submit(work, deadline=self._wall_seconds + _SETUP_SECONDS)
        except BrokerUnavailableError:
            self._dispatcher.post(lambda: done(HandlerOutcome(failure="unavailable")))
            return _nothing

        def finished() -> None:
            if future.cancelled():
                return
            try:
                outcome = future.result()
            except Exception:  # noqa: BLE001  (the link's failure: the line is ours)
                logger.warning("a handler run did not complete")
                outcome = HandlerOutcome(failure="unavailable")
            done(outcome)

        future.add_done_callback(lambda _: self._dispatcher.post(finished))

        def cancel() -> None:
            future.cancel()

        return cancel


def _nothing() -> None:
    return


def _origin_of(stored: StoredBinding | None) -> RunOrigin:
    """What started a handler run. A sandbox handler runs only on a direct input event of the
    user (a patch fires none, D24), so the run of a stored tool is the user's, and the broker
    checks the binding (D30 (2)). A tool that was not opened from the store has none: its run is
    the model's code and gets no tool grant."""
    if stored is None:
        return RunOrigin()
    return RunOrigin(
        trigger="user_event",
        binding=WireBinding(
            instance=stored.instance, tool=stored.tool, content_hash=stored.content_hash
        ),
    )


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


def check_source(source: str) -> None:
    """Refuse source that cannot be a payload (`ToolError`, for the model). The field's own limit
    counts characters; this one counts bytes."""
    try:
        source.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolError("The source is not valid text.") from None
    if text_bytes(source) > MAX_SOURCE_BYTES:
        raise ToolError(f"The source is over {MAX_SOURCE_BYTES // 1024} KiB.")
    if "\x00" in source:
        raise ToolError("The source holds a NUL character.")


def _how(termination: Literal["exited", "timed_out", "output_limit"], exit_code: int | None) -> str:
    match termination:
        case "exited":
            return f"The program exited with code {exit_code}"
        case "timed_out":
            return "The program ran out of time and was stopped"
        case "output_limit":
            return "The program printed too much and was stopped"
        case _ as unreachable:
            assert_never(unreachable)


def run_code_text(result: RunResult) -> str:
    """The model's view of a finished run: how it ended, then stdout and stderr, each framed as
    data the program wrote (its output is never an instruction)."""
    parts = [f"{_how(result.termination, result.exit_code)} after {result.wall_milliseconds} ms."]
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
                    intro=PROGRAM_INTRO,
                )
            )
        else:
            parts.append(f"The program wrote nothing to {name}.")
    return "\n".join(parts)


def _run_code_failure(refusal: BrokerRefused) -> ToolError:
    match refusal.reason:
        case "exhausted":
            return ToolError(
                "This task has used all the code runs it may. Start a new task to run more."
            )
        case "busy":
            return ToolError("The system is busy running other code. Try again in a moment.")
        case _:
            return ToolError(f"The broker said no ({refusal.reason}): {refusal.message}.")


def register_code_tools(registry: ToolRegistry, tasks: TaskSession, link: BrokerLink) -> None:
    """Add `run_code` to `registry`."""

    async def run_code(arguments: RunCodeInput) -> str:
        check_source(arguments.source)
        seconds = arguments.timeout_seconds
        try:
            task = await tasks.ensure()
            # A fresh run: a run ID is used once, and a reply may run code more than once.
            run = RunId.new()

            async def work(client: BrokerClient) -> RunResult:
                return await client.run_in_sandbox(
                    task,
                    run,
                    script_command(),
                    wall_seconds=seconds,
                    cpu_seconds=seconds,
                    max_output_bytes=RUN_CODE_OUTPUT_BYTES,
                    payload=arguments.source,
                )

            result = await link.call(work, deadline=seconds + _SETUP_SECONDS)
        except BrokerRefused as refusal:
            raise _run_code_failure(refusal) from None
        except (BrokerUnavailableError, TaskError) as error:
            raise ToolError(str(error)) from None
        return run_code_text(result)

    registry.register(
        ToolName("run_code"),
        "Run Python 3 once in the sandbox, in the task's workspace (the working directory), with "
        "Pillow and numpy installed. It has no home directory and no network unless the user "
        "granted the network for this task (request_access kind network); it sees only "
        "the files the user granted for this task (access allowed for one reply does not reach "
        "it) and the workspace, and a task with the network sees no granted files. "
        "Its output is returned as data. Use print for what you want back.",
        RunCodeInput,
        run_code,
        timeout_seconds=60.0 + _SETUP_SECONDS + 5.0,
    )
