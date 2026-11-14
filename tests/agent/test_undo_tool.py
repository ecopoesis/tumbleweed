"""The model's write request and `undo_last_change`, against a real broker (S21, D28)."""
# ruff: noqa: PT018

import asyncio
from pathlib import Path

from pydantic import JsonValue

from tests.agent.test_tasks import World, world
from tumbleweed.broker.client import BrokerClient
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.sandbox.policy import SandboxPolicy


def tool(w: World, name: str, **arguments: JsonValue) -> tuple[str, bool]:
    return w.run_tool(name, **arguments)


def run_code_on(w: World, task: TaskId) -> None:
    async def go(client: BrokerClient) -> None:
        await client.run_in_sandbox(task, RunId.new(), ("/usr/bin/python3", "-c", "pass"))

    asyncio.run(w.link.call(go, deadline=20.0))


def project(w: World) -> Path:
    directory = w.home / "Projects" / "app"
    (directory / "a.txt").write_text("alpha")
    return directory


class TestTheModelsTools:
    def test_write_access_is_asked_and_then_undo_puts_the_run_back(self) -> None:
        with world() as w:
            directory = project(w)
            w.allow("task")
            text, error = tool(w, "request_access", kind="write", path=str(directory), reason="r")
            assert not error and "for this task" in text
            task = w.tasks.current
            assert task is not None

            def change(policy: SandboxPolicy) -> None:
                (directory / "a.txt").write_text("CHANGED")

            w.broker.rig.sandbox.on_run = change
            run_code_on(w, task)
            assert (directory / "a.txt").read_text() == "CHANGED"

            text, error = tool(w, "undo_last_change")

            assert not error
            assert text.startswith("Undone: 1 place")
            assert (directory / "a.txt").read_text() == "alpha"

    def test_there_is_nothing_to_undo_in_a_task_that_changed_nothing(self) -> None:
        with world() as w:
            tool(w, "start_task", title="t")

            text, error = tool(w, "undo_last_change")

            assert error
            assert "no change to undo" in text

    def test_a_write_needs_a_path_and_a_pick_takes_none(self) -> None:
        with world() as w:
            text, error = tool(w, "request_access", kind="write", reason="r")
            assert error and "needs a path" in text
            text, error = tool(w, "request_access", kind="network", path="/x", reason="r")
            assert error

    def test_a_denied_write_changes_nothing_and_says_so(self) -> None:
        with world() as w:
            directory = project(w)

            text, error = tool(w, "request_access", kind="write", path=str(directory), reason="r")

            assert not error and text.startswith("Not granted")
