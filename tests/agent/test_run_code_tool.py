"""The `run_code` tool's own checks and the run it asks for, with a fake broker link (S20, D27).

Nothing is run: the fake link records what the tool would have asked the broker for."""

import asyncio
import base64
from collections.abc import Awaitable, Callable

import pytest

from tumbleweed.agent.code import MAX_SOURCE_BYTES, check_source, register_code_tools
from tumbleweed.agent.tools import ToolError, ToolRegistry
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.ipc.broker_messages import RunResult
from tumbleweed.sandbox.policy import PAYLOAD_TARGET

TASK = TaskId.new()


class FakeClient:
    def __init__(self, owner: "Fakes") -> None:
        self._owner = owner

    async def run_in_sandbox(
        self,
        task: TaskId,
        run: RunId,
        command: tuple[str, ...],
        *,
        wall_seconds: int,
        cpu_seconds: int,
        max_output_bytes: int,
        payload: str | None,
    ) -> RunResult:
        self._owner.runs.append((command, payload, wall_seconds))
        if self._owner.refuse is not None:
            raise self._owner.refuse
        return RunResult.model_validate(
            {
                "kind": "run_result",
                "termination": "exited",
                "exit_code": 0,
                "stdout": base64.b64encode(b"ok").decode(),
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "wall_milliseconds": 1,
                "unenforced": (),
                "landlock": "applied",
                "mounted_grants": 0,
                "network": False,
            }
        )


class Fakes:
    """A link and a task session of two methods each: what the tool calls."""

    def __init__(self) -> None:
        self.runs: list[tuple[tuple[str, ...], str | None, int]] = []
        self.refuse: BrokerRefused | None = None

    async def call[R](self, work: Callable[[BrokerClient], Awaitable[R]], *, deadline: float) -> R:
        return await work(FakeClient(self))  # type: ignore[arg-type]  # a fake of one method

    async def ensure(self) -> TaskId:
        return TASK


def registry() -> tuple[ToolRegistry, Fakes]:
    fakes = Fakes()
    tools = ToolRegistry()
    register_code_tools(tools, fakes, fakes)  # type: ignore[arg-type]  # fakes of the used methods
    return tools, fakes


def call(source: str, timeout: int = 10) -> tuple[str, bool, Fakes]:
    tools, fakes = registry()
    outcome = asyncio.run(tools.call("run_code", {"source": source, "timeout_seconds": timeout}))
    return outcome.text, outcome.is_error, fakes


def test_the_source_is_the_payload_and_never_an_argument_and_the_interpreter_is_isolated() -> None:
    text, is_error, fakes = call("print('hi')  # SECRET")

    assert not is_error
    ((command, payload, wall),) = fakes.runs
    assert command == ("python3", "-I", PAYLOAD_TARGET)
    assert payload == "print('hi')  # SECRET"
    assert wall == 10
    assert "ok" in text


def test_source_that_is_too_long_in_bytes_but_not_in_characters_is_refused_unrun() -> None:
    source = "#" + "\U0001f600" * (MAX_SOURCE_BYTES // 4 + 1)  # 4 bytes each, under 24K characters
    assert len(source) < MAX_SOURCE_BYTES

    text, is_error, fakes = call(source)

    assert is_error
    assert "over 24 KiB" in text
    assert fakes.runs == []


def test_source_with_a_nul_is_refused_unrun() -> None:
    text, is_error, fakes = call("print(1)\x00")

    assert is_error
    assert "NUL" in text
    assert fakes.runs == []


def test_source_that_is_not_text_is_refused_unrun() -> None:
    _, is_error, fakes = call("x = '\ud800'")

    assert is_error
    assert fakes.runs == []
    with pytest.raises(ToolError, match="not valid text"):
        check_source("x = '\ud800'")  # the tool's own check, behind the registry's


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("exhausted", "all the code runs"),
        ("busy", "busy running other code"),
        ("denied", "broker said no"),
    ],
)
def test_a_refusal_is_told_for_what_it_is(reason: str, words: str) -> None:
    tools, fakes = registry()
    fakes.refuse = BrokerRefused(reason, "fixed")  # type: ignore[arg-type]  # parametrised

    outcome = asyncio.run(tools.call("run_code", {"source": "x", "timeout_seconds": 5}))

    assert outcome.is_error
    assert words in outcome.text
    assert "try again" not in outcome.text.lower() or reason == "busy"
