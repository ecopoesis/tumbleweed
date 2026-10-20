"""The prompt types of the broker (S18): what a prompt asks, and the interface that never asks."""

import asyncio

import pytest
from pydantic import ValidationError

from tumbleweed.broker.capabilities import NetFull
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.owner import Owner
from tumbleweed.broker.prompts import Allowed, AutoDeny, Denied, GrantPrompt
from tumbleweed.core.ids import PromptId, RunId, TaskId

SCOPES = frozenset({GrantScope.ONCE, GrantScope.TASK})


def prompt(**fields: object) -> GrantPrompt:
    base: dict[str, object] = {
        "task": TaskId.new(),
        "run": RunId.new(),
        "scopes": SCOPES,
        "reason": "x",
    }
    return GrantPrompt.model_validate({**base, **fields})


class TestWhatAPromptAsks:
    def test_it_asks_for_a_capability(self) -> None:
        assert prompt(capability=NetFull()).pick is None

    def test_or_it_asks_for_a_pick(self) -> None:
        asked = prompt(pick="folder")

        assert asked.capability is None
        assert asked.pick == "folder"

    @pytest.mark.parametrize("fields", [{}, {"capability": NetFull(), "pick": "file"}])
    def test_but_for_exactly_one_of_them(self, fields: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            prompt(**fields)

    def test_the_reason_and_the_choice_are_bounded(self) -> None:
        with pytest.raises(ValidationError):
            prompt(pick="file", reason="x" * 501)
        with pytest.raises(ValidationError):
            Allowed(scope=GrantScope.TASK, picked="/" + "a" * 4096)
        with pytest.raises(ValidationError):
            Allowed(scope=GrantScope.TASK, picked="")


class TestAutoDeny:
    def test_it_denies_every_prompt_and_takes_no_answer(self) -> None:
        asked = prompt(pick="file")

        answer = asyncio.run(AutoDeny().ask(asked, Owner(1)))

        assert answer == Denied(reason="no_prompt_ui")
        assert not AutoDeny().answer(Owner(1), PromptId.new(), Allowed(scope=GrantScope.TASK))
