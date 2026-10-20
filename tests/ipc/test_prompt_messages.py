"""The prompt messages are strict and bounded (S18): what the schema refuses reaches no handler."""

import json

import pytest
from pydantic import ValidationError

from tumbleweed.core.ids import PromptId, TaskId
from tumbleweed.ipc.broker_messages import MAX_PATH_CHARS, MAX_REASON_CHARS
from tumbleweed.ipc.prompt_messages import AnswerPrompt, ShowPrompt


def show_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "kind": "show_prompt",
        "prompt": str(PromptId.new()),
        "task": str(TaskId.new()),
        "tool": None,
        "access": {"kind": "fs.read", "path": "/home/u/Photos"},
        "scopes": ["once", "task"],
        "reason": "to look",
        "expires_in_ms": 1000,
    }
    return {**body, **changes}


def answer_body(decision: dict[str, object]) -> dict[str, object]:
    return {"kind": "answer_prompt", "prompt": str(PromptId.new()), "decision": decision}


def test_a_well_formed_prompt_and_answers_are_accepted() -> None:
    assert ShowPrompt.model_validate_json(json.dumps(show_body()))
    for decision in (
        {"kind": "allow", "scope": "once", "picked": None},
        {"kind": "allow", "scope": "task", "picked": "/home/u/Photos"},
        {"kind": "deny", "reason": "user_denied"},
        {"kind": "deny", "reason": "prompt_timeout"},
    ):
        assert AnswerPrompt.model_validate_json(json.dumps(answer_body(decision)))


@pytest.mark.parametrize(
    "changes",
    [
        {"scopes": []},
        {"scopes": ["once", "task", "once"]},
        {"scopes": ["tool"]},
        {"reason": "x" * (MAX_REASON_CHARS + 1)},
        {"expires_in_ms": 0},
        {"expires_in_ms": 3_600_001},
        {"prompt": "tsk_" + "a" * 26},  # a TaskId is not a PromptId
        {"prompt": "prm_not-an-id"},
        {"access": {"kind": "fs.read", "path": ""}},
        {"access": {"kind": "fs.read", "path": "/" + "a" * MAX_PATH_CHARS}},
        {"access": {"kind": "fs.pick", "choose": "anything"}},
        {"access": {"kind": "shell.exec", "command": "rm"}},
        {"extra": 1},
    ],
)
def test_a_prompt_the_schema_refuses(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ShowPrompt.model_validate_json(json.dumps(show_body(**changes)))


@pytest.mark.parametrize(
    "decision",
    [
        {"kind": "approve", "scope": "task", "picked": None},
        {"kind": "allow", "scope": "tool", "picked": None},
        {"kind": "allow", "scope": "task"},  # picked is required, even if null
        {"kind": "allow", "scope": "task", "picked": ""},
        {"kind": "allow", "scope": "task", "picked": "/" + "a" * MAX_PATH_CHARS},
        {"kind": "allow", "scope": 1, "picked": None},
        {"kind": "deny", "reason": "because"},
        {"kind": "deny", "reason": "user_denied", "scope": "task"},
        {},
    ],
)
def test_an_answer_the_schema_refuses(decision: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AnswerPrompt.model_validate_json(json.dumps(answer_body(decision)))
