"""Fakes and builders for the trusted-prompt tests (S18): a clock and timers a test moves by hand, a
view that records, a picker that waits for the user, and the wire message of a prompt.

No `gi` here: the controller's tests need none, and the GTK tests import this too.
"""

from collections.abc import Callable
from typing import Literal, final

from tumbleweed.core.ids import PromptId, TaskId, ToolId
from tumbleweed.ipc.broker_messages import (
    NetworkAccess,
    PickAccess,
    ReadAccess,
    RequestedAccess,
    Scope,
)
from tumbleweed.ipc.prompt_messages import PromptDecision, ShowPrompt
from tumbleweed.shell.viewmodel.prompts import PromptCard, card_of


@final
class Clock:
    """A monotonic clock a test moves by hand (and the timers that go with it, see `Timers`)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@final
class Timers:
    """A `Scheduler` driven by the test: `advance` moves the shared clock and fires what is due."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._pending: list[tuple[float, int, Callable[[], None]]] = []
        self._next = 0

    def call_later(self, seconds: float, callback: Callable[[], None]) -> Callable[[], None]:
        self._next += 1
        entry = (self._clock.now + seconds, self._next, callback)
        self._pending.append(entry)

        def cancel() -> None:
            if entry in self._pending:
                self._pending.remove(entry)

        return cancel

    @property
    def waiting(self) -> int:
        return len(self._pending)

    def advance(self, seconds: float) -> None:
        target = self._clock.now + seconds
        while True:
            due = sorted(entry for entry in self._pending if entry[0] <= target)
            if not due:
                break
            entry = due[0]
            self._pending.remove(entry)
            self._clock.now = max(self._clock.now, entry[0])
            entry[2]()
        self._clock.now = target


@final
class FakeView:
    """A `PromptView` that records what it was told."""

    def __init__(self) -> None:
        self.shown: list[PromptCard] = []
        self.armed: list[bool] = []
        self.cleared = 0

    def present(self, card: PromptCard) -> None:
        self.shown.append(card)

    def set_armed(self, armed: bool) -> None:
        self.armed.append(armed)

    def clear(self) -> None:
        self.cleared += 1


@final
class Answers:
    """A `PromptSink` that keeps what the user decided."""

    def __init__(self) -> None:
        self.given: list[tuple[PromptId, PromptDecision]] = []
        self.explode = False

    def __call__(self, prompt: PromptId, decision: PromptDecision, /) -> None:
        if self.explode:
            raise RuntimeError("the connection is gone")
        self.given.append((prompt, decision))


@final
class FakePicker:
    """The trusted picker with nobody at it: `finish` is the user choosing (or not choosing)."""

    def __init__(self) -> None:
        self.asked: list[Literal["file", "folder"]] = []
        self._done: list[Callable[[str | None], None]] = []

    def pick(
        self, parent: object, choose: Literal["file", "folder"], done: Callable[[str | None], None]
    ) -> None:
        self.asked.append(choose)
        self._done.append(done)

    def finish(self, path: str | None) -> None:
        self._done.pop(0)(path)


def show_message(
    *,
    access: RequestedAccess | None = None,
    reason: str = "to look",
    scopes: tuple[Scope, ...] = ("once", "task"),
    task: TaskId | None = None,
    tool: ToolId | None = None,
    prompt: PromptId | None = None,
    expires_in_ms: int = 60_000,
) -> ShowPrompt:
    return ShowPrompt(
        kind="show_prompt",
        prompt=PromptId.new() if prompt is None else prompt,
        task=TaskId.new() if task is None else task,
        tool=tool,
        access=ReadAccess(kind="fs.read", path="/home/u/Photos") if access is None else access,
        scopes=scopes,
        reason=reason,
        expires_in_ms=expires_in_ms,
    )


def read_card(**changes: object) -> PromptCard:
    return card_of(show_message(**changes))  # type: ignore[arg-type]  # a test passes what it means


def pick_card(choose: Literal["file", "folder"] = "folder") -> PromptCard:
    return card_of(show_message(access=PickAccess(kind="fs.pick", choose=choose)))


def network_card() -> PromptCard:
    return card_of(show_message(access=NetworkAccess(kind="net.full")))
