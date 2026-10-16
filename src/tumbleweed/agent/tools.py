"""The typed tool registry: what the model may ask the agent to run, and how a call is checked.

A tool is a name, a description, a pydantic input model and an async handler that takes an
instance of that model. The model's JSON Schema is what the provider shows the LLM, and it is also
what a call is validated against, so a handler never sees an input the schema would refuse
(ARCHITECTURE section 3: invalid calls go back to the model as errors).

`ToolRegistry.call` does not raise for anything the model can cause: an unknown tool, an input
that does not validate, a handler that fails or takes too long all come back as an error
`ToolOutcome`, which the loop returns to the model as an error `tool_result`. Results are bounded.
Only cancellation passes through.

S03 ships no real tools: the registry exists, and is tested, for S12 and S19 to fill. Handlers run
on the agent's worker thread, not the GTK main thread: a handler that needs the UI marshals to it
itself (the shell's dispatcher), and never touches a widget directly.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import final

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from tumbleweed.agent.errors import describe_unexpected
from tumbleweed.agent.limits import MAX_TOOL_INPUT_BYTES, MAX_TOOL_RESULT_BYTES, TOOL_TIMEOUT
from tumbleweed.agent.llm import ToolSpec, sanitize_text
from tumbleweed.core.errors import InvalidValueError, TumbleweedError, excerpt
from tumbleweed.core.types import PatternStr

logger = logging.getLogger(__name__)

MAX_TOOLS = 64
_MAX_PROBLEM_LINES = 5
_MAX_PROBLEM_CHARS = 300
_SCHEMA = TypeAdapter(dict[str, JsonValue])


@final
class ToolName(
    PatternStr,
    pattern=r"[A-Za-z][A-Za-z0-9_-]{0,63}",
    expects="a letter then up to 63 of A-Z a-z 0-9 _ -",
):
    """The name of a tool, in the character set the providers accept."""

    __slots__ = ()


@final
class ToolOutcome(BaseModel):
    """What a call to the registry produced: text for the model, and whether it is an error."""

    text: str
    is_error: bool = False


def _bounded(text: str) -> str:
    text = sanitize_text(text)
    data = text.encode("utf-8")
    if len(data) <= MAX_TOOL_RESULT_BYTES:
        return text
    return data[:MAX_TOOL_RESULT_BYTES].decode("utf-8", errors="ignore") + "\n[truncated]"


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _problems(error: ValidationError) -> str:
    # Without the input values: they are the model's, and they can be large.
    lines = [
        f"{'.'.join(str(part) for part in item['loc']) or 'input'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False, include_context=False)[
            :_MAX_PROBLEM_LINES
        ]
    ]
    return _one_line("; ".join(lines), _MAX_PROBLEM_CHARS)


@final
class _Entry:
    def __init__(self, spec: ToolSpec, invoke: Callable[[str], Awaitable[str]]) -> None:
        self.spec = spec
        self.invoke = invoke


@final
class ToolRegistry:
    """The tools on offer, by name."""

    def __init__(self, *, timeout_seconds: float = TOOL_TIMEOUT.total_seconds()) -> None:
        self._entries: dict[str, _Entry] = {}
        self._timeout = timeout_seconds

    def register[I: BaseModel](
        self,
        name: ToolName,
        description: str,
        input_model: type[I],
        handler: Callable[[I], Awaitable[str]],
    ) -> None:
        """Add a tool. The name must be new, and `input_model` must forbid extra fields."""
        if name in self._entries:
            raise InvalidValueError(f"tool {excerpt(str(name))} is already registered")
        if len(self._entries) >= MAX_TOOLS:
            raise InvalidValueError(f"at most {MAX_TOOLS} tools can be registered")
        if input_model.model_config.get("extra") != "forbid":
            raise InvalidValueError(f"the input model of {name} must set extra='forbid'")
        schema = _SCHEMA.validate_python(input_model.model_json_schema())

        async def invoke(raw: str) -> str:
            return await handler(input_model.model_validate_json(raw))

        spec = ToolSpec(name=str(name), description=description, input_schema=schema)
        self._entries[str(name)] = _Entry(spec, invoke)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(entry.spec for entry in self._entries.values())

    async def call(self, name: str, tool_input: dict[str, JsonValue]) -> ToolOutcome:
        """Run the tool `name` on `tool_input`; see the module docstring for what is returned."""
        entry = self._entries.get(name)
        if entry is None:
            return ToolOutcome(text=f"Unknown tool {excerpt(name)}.", is_error=True)
        raw = json.dumps(tool_input, ensure_ascii=False)
        if len(raw.encode("utf-8", errors="replace")) > MAX_TOOL_INPUT_BYTES:
            return ToolOutcome(text="The tool input is too large.", is_error=True)
        try:
            async with asyncio.timeout(self._timeout):
                return ToolOutcome(text=_bounded(await entry.invoke(raw)))
        except ValidationError as exc:
            return ToolOutcome(text=f"Invalid input for {name}: {_problems(exc)}", is_error=True)
        except TimeoutError:
            return ToolOutcome(
                text=f"The tool {name} took too long and was stopped.", is_error=True
            )
        except TumbleweedError as exc:
            # The tool's own, expected failure: its message is ours to show the model, bounded.
            return ToolOutcome(
                text=_one_line(str(exc), _MAX_PROBLEM_CHARS) or "failed", is_error=True
            )
        except Exception as exc:  # noqa: BLE001  (a handler's bug comes back as an error)
            logger.error("tool %s failed: %s", name, describe_unexpected(exc))
            return ToolOutcome(text=f"The tool {name} failed unexpectedly.", is_error=True)
