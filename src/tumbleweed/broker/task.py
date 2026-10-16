"""Task state: the part of a task that policy looks at, chiefly its taint (I12).

A task's **taint** is a sticky set. It becomes `fs`-tainted the moment it is issued any fs grant
(any scope, used or not, picker included) and `net`-tainted the moment it is issued `net.full`;
applying a tool grant to a run counts as issuing it (`policy.select_grants`). No task is ever both
(`TaskState` refuses to exist like that), and the taint of a task only grows: nothing here
removes any, so a grant that expires or is revoked leaves the task tainted.

A new task copies the taint of the one it replaces unless it is created with
`fresh_context=true` (D7, ARCHITECTURE sections 2 and 7), because without it the model's
context carries the earlier task's assistant turns, which may hold what the earlier task saw.
"""

from enum import StrEnum
from typing import Final, Self, assert_never, final

from pydantic import model_validator

from tumbleweed.broker.capabilities import CapabilityKind
from tumbleweed.broker.model import PolicyModel
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import TaskId


class TaintKind(StrEnum):
    """What a task has been exposed to."""

    FS = "fs"  # it was given access to files outside its workspace
    NET = "net"  # it was given the network


NO_TAINT: Final[frozenset[TaintKind]] = frozenset()
_BOTH: Final = frozenset({TaintKind.FS, TaintKind.NET})


def taint_of(capability: CapabilityKind) -> TaintKind:
    """The taint that being issued `capability` brings."""
    match capability:
        case CapabilityKind.FS_READ | CapabilityKind.FS_WRITE:
            return TaintKind.FS
        case CapabilityKind.NET_FULL:
            return TaintKind.NET
        case _ as unreachable:
            assert_never(unreachable)


def other_taint(kind: TaintKind) -> TaintKind:
    """The kind that cannot be held together with `kind`."""
    match kind:
        case TaintKind.FS:
            return TaintKind.NET
        case TaintKind.NET:
            return TaintKind.FS
        case _ as unreachable:
            assert_never(unreachable)


def merged_taint(
    current: frozenset[TaintKind], incoming: frozenset[TaintKind]
) -> frozenset[TaintKind] | None:
    """`current` with `incoming` added, or None if that would make a task both fs- and
    net-tainted (I12)."""
    merged = current | incoming
    return None if merged >= _BOTH else merged


@final
class TaskState(PolicyModel):
    """A task as policy sees it: its ID, its taint, and whether it has ended.

    An ended task is refused everything (ending revokes its `once` and `task` grants; this is the
    same rule held by the decision function as well as by the store).
    """

    task: TaskId
    taint: frozenset[TaintKind] = NO_TAINT
    ended: bool = False

    @model_validator(mode="after")
    def _not_both(self) -> Self:
        if self.taint >= _BOTH:
            raise InvalidValueError("a task is never both fs-tainted and net-tainted (I12)")
        return self

    def with_taint(self, incoming: frozenset[TaintKind]) -> Self | None:
        """The task with `incoming` added to its taint, or None if that would conflict."""
        merged = merged_taint(self.taint, incoming)
        if merged is None:
            return None
        return type(self)(task=self.task, taint=merged, ended=self.ended)

    def successor(self, task: TaskId, *, fresh_context: bool) -> Self:
        """The task that replaces this one: tainted as this one is, unless `fresh_context`.

        `fresh_context` must be a real `bool`: a truthy string must not drop the taint.
        """
        flag: object = fresh_context  # the annotation is for the checker; callers can pass anything
        if type(flag) is not bool:
            raise InvalidValueError("fresh_context must be a bool")
        return type(self)(task=task, taint=NO_TAINT if fresh_context else self.taint)

    def end(self) -> Self:
        """The same task, ended."""
        return type(self)(task=self.task, taint=self.taint, ended=True)
