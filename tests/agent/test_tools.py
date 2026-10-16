"""The typed tool registry: schemas out, validated calls in, nothing the model causes can raise."""

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from tumbleweed.agent.limits import MAX_TOOL_INPUT_BYTES, MAX_TOOL_RESULT_BYTES
from tumbleweed.agent.tools import MAX_TOOLS, ToolName, ToolRegistry
from tumbleweed.core.errors import InvalidValueError, TumbleweedError


class AddInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    a: int
    b: int


async def add(args: AddInput) -> str:
    return str(args.a + args.b)


def registry_with_add(**kwargs: float) -> ToolRegistry:
    registry = ToolRegistry(**kwargs)
    registry.register(ToolName("add"), "Add two integers.", AddInput, add)
    return registry


def call(registry: ToolRegistry, name: str, tool_input: dict[str, JsonValue]) -> tuple[str, bool]:
    outcome = asyncio.run(registry.call(name, tool_input))
    return outcome.text, outcome.is_error


class TestSpecs:
    def test_the_spec_carries_the_name_description_and_the_models_schema(self) -> None:
        (spec,) = registry_with_add().specs()

        assert (spec.name, spec.description) == ("add", "Add two integers.")
        assert spec.input_schema["type"] == "object"
        assert spec.input_schema["required"] == ["a", "b"]
        assert spec.input_schema["additionalProperties"] is False

    def test_an_empty_registry_offers_nothing(self) -> None:
        assert ToolRegistry().specs() == ()

    @pytest.mark.parametrize("bad", ["", "1add", "has space", "x" * 65, "add!", "naïve"])
    def test_a_tool_name_is_in_the_providers_character_set(self, bad: str) -> None:
        with pytest.raises(InvalidValueError):
            ToolName(bad)


class TestRegistration:
    def test_a_name_is_registered_once(self) -> None:
        with pytest.raises(InvalidValueError, match="already registered"):
            registry_with_add().register(ToolName("add"), "again", AddInput, add)

    def test_an_input_model_that_allows_extra_fields_is_refused(self) -> None:
        class Loose(BaseModel):
            a: int

        async def handler(args: Loose) -> str:
            return ""

        with pytest.raises(InvalidValueError, match="extra='forbid'"):
            ToolRegistry().register(ToolName("loose"), "", Loose, handler)

    def test_the_number_of_tools_is_bounded(self) -> None:
        registry = ToolRegistry()
        for index in range(MAX_TOOLS):
            registry.register(ToolName(f"t{index}"), "", AddInput, add)

        with pytest.raises(InvalidValueError, match="at most"):
            registry.register(ToolName("one-too-many"), "", AddInput, add)


class TestCalls:
    def test_a_valid_call_runs_the_handler_with_the_validated_model(self) -> None:
        assert call(registry_with_add(), "add", {"a": 1, "b": 2}) == ("3", False)

    @pytest.mark.parametrize(
        "tool_input",
        [{"a": 1}, {"a": 1, "b": "2"}, {"a": 1, "b": 2, "c": 3}, {"a": True, "b": 1}, {}],
    )
    def test_an_input_the_schema_refuses_comes_back_as_an_error_not_an_exception(
        self, tool_input: dict[str, JsonValue]
    ) -> None:
        text, is_error = call(registry_with_add(), "add", tool_input)

        assert is_error
        assert text.startswith("Invalid input for add:")

    def test_the_error_does_not_echo_the_input_values(self) -> None:
        text, _ = call(registry_with_add(), "add", {"a": "SECRET-" + "x" * 5000, "b": 1})

        assert "SECRET" not in text
        assert len(text) < 500

    def test_an_unknown_tool_is_an_error_naming_it_safely(self) -> None:
        text, is_error = call(registry_with_add(), "rm\n-rf", {})

        assert is_error
        assert "\n" not in text
        assert "Unknown tool" in text

    def test_an_input_over_the_size_limit_is_refused_before_validation(self) -> None:
        text, is_error = call(registry_with_add(), "add", {"a": "x" * (MAX_TOOL_INPUT_BYTES + 1)})

        assert (text, is_error) == ("The tool input is too large.", True)

    def test_a_result_over_the_limit_is_cut(self) -> None:
        async def big(args: AddInput) -> str:
            return "é" * MAX_TOOL_RESULT_BYTES  # two bytes each

        registry = ToolRegistry()
        registry.register(ToolName("big"), "", AddInput, big)

        text, is_error = call(registry, "big", {"a": 1, "b": 1})

        assert not is_error
        assert len(text.encode()) <= MAX_TOOL_RESULT_BYTES + len("\n[truncated]")
        assert text.endswith("[truncated]")

    def test_a_domain_error_of_the_handler_is_reported_to_the_model(self) -> None:
        async def refuses(args: AddInput) -> str:
            raise TumbleweedError("no access\nto that")

        registry = ToolRegistry()
        registry.register(ToolName("refuses"), "", AddInput, refuses)

        assert call(registry, "refuses", {"a": 1, "b": 1}) == ("no access to that", True)

    def test_a_bug_in_the_handler_is_an_error_without_its_text(self) -> None:
        async def buggy(args: AddInput) -> str:
            raise RuntimeError("internal detail /home/user/secret")

        registry = ToolRegistry()
        registry.register(ToolName("buggy"), "", AddInput, buggy)

        text, is_error = call(registry, "buggy", {"a": 1, "b": 1})

        assert is_error
        assert "secret" not in text

    def test_a_handler_that_hangs_is_stopped(self) -> None:
        async def hangs(args: AddInput) -> str:
            await asyncio.Event().wait()
            return ""

        registry = ToolRegistry(timeout_seconds=0.05)
        registry.register(ToolName("hangs"), "", AddInput, hangs)

        text, is_error = call(registry, "hangs", {"a": 1, "b": 1})

        assert is_error
        assert "took too long" in text

    def test_cancellation_passes_through_a_call(self) -> None:
        async def scenario() -> bool:
            async def waits(args: AddInput) -> str:
                await asyncio.Event().wait()
                return ""

            registry = ToolRegistry()
            registry.register(ToolName("waits"), "", AddInput, waits)
            task = asyncio.ensure_future(registry.call("waits", {"a": 1, "b": 1}))
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return True
            return False

        assert asyncio.run(scenario())
