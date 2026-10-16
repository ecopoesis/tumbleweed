"""Every model call goes through the invocation recorder, structurally and not by convention."""

import ast
from pathlib import Path

import pytest

from tests.agent.support import MODEL
from tumbleweed.agent.fake import FakeProvider
from tumbleweed.agent.limits import AgentLimits
from tumbleweed.agent.loop import AgentLoop
from tumbleweed.agent.tools import ToolRegistry

SRC = Path(__file__).resolve().parents[2] / "src" / "tumbleweed"
# The three places a `.stream(` of a model call may be: the recorder (calls the provider), the
# loop (calls the recorded provider) and the provider itself (calls the SDK).
ALLOWED = {"agent/recorder.py", "agent/loop.py", "agent/anthropic_provider.py"}


def stream_calls() -> set[str]:
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "stream"
            for node in ast.walk(tree)
        ):
            found.add(str(path.relative_to(SRC)))
    return found


def test_only_the_recorder_the_loop_and_the_sdk_adapter_call_stream() -> None:
    assert stream_calls() == ALLOWED


def test_the_loop_does_not_import_a_provider() -> None:
    imported = {
        node.module
        for node in ast.walk(ast.parse((SRC / "agent/loop.py").read_text()))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "tumbleweed.agent.anthropic_provider" not in imported
    assert "tumbleweed.agent.fake" not in imported


def test_the_loop_refuses_a_provider_that_is_not_recorded() -> None:
    with pytest.raises(TypeError, match="RecordedProvider"):
        AgentLoop(
            provider=FakeProvider([]),  # type: ignore[arg-type]  # the point: the checker refuses too
            registry=ToolRegistry(),
            model=MODEL,
            system_prompt="s",
            limits=AgentLimits(),
        )
