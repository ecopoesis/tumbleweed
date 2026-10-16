"""Tests for `tumbleweed.broker.task`: taint (I12) and task state."""

import pytest
from pydantic import ValidationError

from tumbleweed.broker import CapabilityKind, TaintKind, TaskState, merged_taint
from tumbleweed.broker.task import NO_TAINT, other_taint, taint_of
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import TaskId, ToolId

FS = frozenset({TaintKind.FS})
NET = frozenset({TaintKind.NET})
BOTH = FS | NET


class TestMergedTaint:
    @pytest.mark.parametrize(
        ("current", "incoming", "expected"),
        [
            (NO_TAINT, NO_TAINT, NO_TAINT),
            (NO_TAINT, FS, FS),
            (NO_TAINT, NET, NET),
            (FS, NO_TAINT, FS),
            (FS, FS, FS),
            (NET, NET, NET),
            (FS, NET, None),
            (NET, FS, None),
            (NO_TAINT, BOTH, None),
            (BOTH, NO_TAINT, None),
        ],
        ids=[
            "none",
            "fs-in",
            "net-in",
            "fs-stays",
            "fs-fs",
            "net-net",
            "fs-net",
            "net-fs",
            "both-in",
            "both-held",
        ],
    )
    def test_adds_unless_it_would_hold_both(
        self,
        current: frozenset[TaintKind],
        incoming: frozenset[TaintKind],
        expected: frozenset[TaintKind] | None,
    ) -> None:
        assert merged_taint(current, incoming) == expected

    def test_never_removes_taint(self) -> None:
        for held in (NO_TAINT, FS, NET):
            for incoming in (NO_TAINT, FS, NET):
                merged = merged_taint(held, incoming)
                if merged is not None:
                    assert held <= merged


class TestKinds:
    @pytest.mark.parametrize(
        ("capability", "taint"),
        [
            (CapabilityKind.FS_READ, TaintKind.FS),
            (CapabilityKind.FS_WRITE, TaintKind.FS),
            (CapabilityKind.NET_FULL, TaintKind.NET),
        ],
    )
    def test_each_capability_has_its_taint(
        self, capability: CapabilityKind, taint: TaintKind
    ) -> None:
        assert taint_of(capability) is taint

    def test_the_two_kinds_exclude_each_other(self) -> None:
        assert other_taint(TaintKind.FS) is TaintKind.NET
        assert other_taint(TaintKind.NET) is TaintKind.FS


class TestTaskState:
    def test_a_new_task_has_no_taint_and_has_not_ended(self) -> None:
        state = TaskState(task=TaskId.new())

        assert state.taint == NO_TAINT
        assert not state.ended

    def test_a_task_is_never_both(self) -> None:
        with pytest.raises(ValidationError, match="never both"):
            TaskState(task=TaskId.new(), taint=BOTH)
        with pytest.raises(ValidationError, match="never both"):
            TaskState.model_validate_json(f'{{"task": "{TaskId.new()}", "taint": ["fs", "net"]}}')

    def test_adding_taint_returns_a_new_state(self) -> None:
        state = TaskState(task=TaskId.new())

        tainted = state.with_taint(FS)

        assert tainted is not None
        assert tainted.taint == FS
        assert state.taint == NO_TAINT
        assert tainted.task == state.task

    def test_adding_the_other_taint_is_refused(self) -> None:
        state = TaskState(task=TaskId.new(), taint=FS)

        assert state.with_taint(NET) is None
        assert state.with_taint(FS) == state

    def test_ending_keeps_the_taint(self) -> None:
        state = TaskState(task=TaskId.new(), taint=NET).end()

        assert state.ended
        assert state.taint == NET

    def test_adding_taint_to_an_ended_task_keeps_it_ended(self) -> None:
        ended = TaskState(task=TaskId.new()).end()

        tainted = ended.with_taint(FS)

        assert tainted is not None
        assert tainted.ended

    def test_is_strict_closed_and_frozen(self) -> None:
        state = TaskState(task=TaskId.new())

        with pytest.raises(ValidationError):
            TaskState.model_validate({"task": ToolId.new()})
        with pytest.raises(ValidationError):
            TaskState.model_validate({"task": state.task, "extra": 1})
        with pytest.raises(ValidationError):
            TaskState.model_validate({"task": state.task, "ended": 1})
        with pytest.raises(ValidationError):
            TaskState.model_validate({"task": state.task, "taint": ["tainted"]})
        with pytest.raises(ValidationError):
            state.ended = True  # type: ignore[misc]  # the point of the test

    def test_round_trips_through_json(self) -> None:
        state = TaskState(task=TaskId.new(), taint=FS, ended=True)

        assert TaskState.model_validate_json(state.model_dump_json()) == state


class TestSuccessor:
    """D7: a task whose context inherits turns from an fs-tainted task starts fs-tainted, unless
    it was created with `fresh_context=true`."""

    @pytest.mark.parametrize("taint", [FS, NET, NO_TAINT])
    def test_inherits_the_taint_of_the_task_it_replaces(self, taint: frozenset[TaintKind]) -> None:
        ended = TaskState(task=TaskId.new(), taint=taint).end()

        successor = ended.successor(TaskId.new(), fresh_context=False)

        assert successor.taint == taint
        assert not successor.ended
        assert successor.task != ended.task

    @pytest.mark.parametrize("taint", [FS, NET, NO_TAINT])
    def test_a_fresh_context_starts_clean(self, taint: frozenset[TaintKind]) -> None:
        successor = TaskState(task=TaskId.new(), taint=taint).successor(
            TaskId.new(), fresh_context=True
        )

        assert successor.taint == NO_TAINT

    @pytest.mark.parametrize("flag", ["false", "true", "", 0, 1, None, "False"])
    def test_the_flag_must_be_a_real_bool(self, flag: object) -> None:
        """A truthy string must not be able to drop the taint."""
        state = TaskState(task=TaskId.new(), taint=FS)

        with pytest.raises(InvalidValueError, match="bool"):
            state.successor(TaskId.new(), fresh_context=flag)  # type: ignore[arg-type]

    def test_the_inherited_taint_is_the_same_set_not_a_view(self) -> None:
        original = TaskState(task=TaskId.new(), taint=FS)

        successor = original.successor(TaskId.new(), fresh_context=False)
        grown = successor.with_taint(FS)

        assert grown == successor
        assert original.taint == FS
