"""The trusted permission prompt as a state machine, with no GTK (S18, I5, I10).

`PromptController` is what decides, for the prompts the broker sends the shell, what is shown, when
it can be answered and when it is over. The widgets (`tumbleweed.shell.prompt_chrome`) only draw
its `PromptCard` and report a click; nothing on this side of the line knows about a window.

What it holds to, in the order of the invariant (I5):

- **One at a time.** At most one prompt is shown; the others wait behind it in order, at most
  `MAX_QUEUED` in all (a ninth is refused: the broker denies it). A prompt takes the place of the
  last one only when that one is answered, withdrawn or expired.
- **Not at once.** A prompt ignores every answer for its first `ARM_SECONDS`: a click or an Enter
  that was meant for whatever was under the pointer or the focus a moment ago cannot answer it.
  The clock is the controller's own and is read when the answer arrives, so a late timer cannot
  arm a prompt early. The delay starts again when the prompt is first drawn (`mark_drawn`), when
  the window is activated (`reactivated`) and on every press or key that reaches the window while
  the prompt is not yet armed (`disturbed`): a stream of clicks aimed at where a button will
  appear is never taken until there has been a quiet gap of the whole delay.
- **No answer is a denial.** Every prompt has the deadline the broker gave it (`expires_in`), and
  past it the shell answers `deny(prompt_timeout)` and takes the prompt down: shown or waiting.
- **What is shown is built from the structure.** `card_of` turns the wire message into a card of a
  closed `kind`, the path or none, and the model's reason made plain (no control, format or
  separator characters, one line, capped), so the widgets cannot be handed text that renders as
  something else. A path the shell does not like is shown with the offending characters spelled out.
- **Only a person answers.** `choose` is called by the widgets' click handlers and by nothing else:
  there is no path from a transcript, the voice input or the agent to it (I10).

The controller runs on the GTK main thread only (the broker's requests are marshalled there by
`tumbleweed.shell.broker_prompts`).
"""

import logging
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal, Protocol, assert_never, final

from tumbleweed.agent.canvas_events import Scheduler
from tumbleweed.core.ids import PromptId, TaskId, ToolId
from tumbleweed.ipc.broker_messages import (
    NetworkAccess,
    PickAccess,
    ReadAccess,
    Scope,
    WriteAccess,
)
from tumbleweed.ipc.prompt_messages import AllowDecision, DenyDecision, PromptDecision, ShowPrompt

_log = logging.getLogger(__name__)

ARM_SECONDS: Final = 0.75
"""How long a prompt ignores answers after it is shown (I5)."""
MAX_QUEUED: Final = 8
"""The most prompts the shell holds, the one shown included."""
MAX_REASON_SHOWN: Final = 300
"""The most characters of the model's reason that are shown (I5: capped)."""
_ELLIPSIS: Final = "…"

_SPELLED_OUT: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

type CardKind = Literal["read", "write", "network", "pick_file", "pick_folder"]


@final
@dataclass(frozen=True, slots=True)
class PromptCard:
    """What the widgets draw. `path` is for `read` and `write` only; `reason` is the model's text,
    plain and capped; `reason_cut` says it was cut."""

    prompt: PromptId
    task: TaskId
    tool: ToolId | None
    kind: CardKind
    path: str | None
    scopes: tuple[Scope, ...]
    reason: str
    reason_cut: bool

    @property
    def picks(self) -> bool:
        return self.kind in ("pick_file", "pick_folder")


def plain_reason(text: str, limit: int = MAX_REASON_SHOWN) -> tuple[str, bool]:
    """`text` as one plain line of at most `limit` characters, and whether it was cut.

    Control, format (bidi overrides, zero-width), separator, private-use and unassigned characters
    become a space, runs of spaces one space, so that nothing in it can start a new line, reorder
    what follows or hide a character.
    """
    pieces: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        pieces.append(" " if category[0] in "CZ" and char != " " else char)
    squeezed = " ".join("".join(pieces).split())
    if len(squeezed) <= limit:
        return squeezed, False
    return squeezed[: limit - 1].rstrip() + _ELLIPSIS, True


def visible(text: str) -> str:
    """`text` with every character that is not safe to show spelled out as `\\u{XXXX}`.

    For what must be shown whole and exactly (a path): nothing is dropped, so the user sees what
    would be granted, and nothing can hide. That is every control (Cc), format (Cf, bidi overrides
    and zero-width characters), surrogate, private-use and unassigned character and the line and
    paragraph separators (Zl, Zp), which would break the line: the categories of
    `core.types.WorkspaceRelPath`, and the rest of C.
    """
    return "".join(
        f"\\u{{{ord(char):04X}}}" if unicodedata.category(char) in _SPELLED_OUT else char
        for char in text
    )


def card_of(show: ShowPrompt) -> PromptCard:
    """The card for a prompt from the broker. The capability is read from its structure."""
    access = show.access
    kind: CardKind
    path: str | None = None
    match access:
        case ReadAccess():
            kind, path = "read", visible(access.path)
        case WriteAccess():
            kind, path = "write", visible(access.path)
        case NetworkAccess():
            kind = "network"
        case PickAccess():
            kind = "pick_file" if access.choose == "file" else "pick_folder"
        case _:
            assert_never(access)
    reason, cut = plain_reason(show.reason)
    return PromptCard(
        prompt=show.prompt,
        task=show.task,
        tool=show.tool,
        kind=kind,
        path=path,
        scopes=show.scopes,
        reason=reason,
        reason_cut=cut,
    )


class PromptView(Protocol):
    """What draws a prompt (`tumbleweed.shell.prompt_chrome`; a fake in the unit tests)."""

    def present(self, card: PromptCard) -> None:
        """Show `card`, not yet armed."""
        ...

    def set_armed(self, armed: bool) -> None:
        """The prompt now takes answers (or, with False, does not yet)."""
        ...

    def clear(self) -> None:
        """No prompt is pending any more."""
        ...


class PromptSink(Protocol):
    """Where an answer goes: back to the broker, on the connection the prompt arrived on."""

    def __call__(self, prompt: PromptId, decision: PromptDecision, /) -> None: ...


@final
class _Entry:
    def __init__(self, card: PromptCard) -> None:
        self.card = card
        self.cancel_deadline: Callable[[], None] = _nothing
        self.cancel_arming: Callable[[], None] = _nothing
        self.shown_at: float | None = None
        # A press (or a key) that began while the prompt was not armed ends in no click, however
        # long it is held (a click comes on the release). A pointer and a key have a life of their
        # own each: a key pressed during a held press, or a second button, does not end it. A flag
        # is cleared only by a refused click, or by a new press that begins on a prompt that takes
        # answers with nothing of that kind held.
        self.pointer_disarmed = False
        self.key_disarmed = False
        self.pointers_down = 0
        self.key_down = False

    @property
    def held_from_disarmed(self) -> bool:
        return self.pointer_disarmed or self.key_disarmed

    def forget_held(self) -> None:
        self.pointer_disarmed = False
        self.key_disarmed = False


def _nothing() -> None:
    return None


@final
class PromptController:
    """Queues the broker's prompts, shows one, and reports what the user chose."""

    def __init__(
        self,
        view: PromptView,
        sink: PromptSink,
        scheduler: Scheduler,
        clock: Callable[[], float],
    ) -> None:
        self._view = view
        self._sink = sink
        self._scheduler = scheduler
        self._clock = clock
        self._current: _Entry | None = None
        self._queue: deque[_Entry] = deque()

    @property
    def current(self) -> PromptCard | None:
        """The prompt that is shown, if any."""
        return None if self._current is None else self._current.card

    @property
    def queued(self) -> int:
        """How many prompts wait behind the one that is shown."""
        return len(self._queue)

    @property
    def pending(self) -> bool:
        return self._current is not None

    def armed(self) -> bool:
        """Whether the prompt that is shown takes answers now."""
        entry = self._current
        return (
            entry is not None
            and entry.shown_at is not None
            and self._clock() - entry.shown_at >= ARM_SECONDS
        )

    def offer(self, card: PromptCard, expires_in: float) -> bool:
        """Take a prompt from the broker. False if it cannot be taken (the queue is full, or the
        PromptId is already here): the broker is told, and denies it."""
        known = {entry.card.prompt for entry in self._entries()}
        if card.prompt in known or len(known) >= MAX_QUEUED:
            return False
        entry = _Entry(card)
        entry.cancel_deadline = self._scheduler.call_later(expires_in, lambda: self._expired(entry))
        self._queue.append(entry)
        if self._current is None:
            self._next()
        return True

    def pointer_pressed(self) -> None:
        """A pointer button went down in the window. If the prompt is not armed yet the delay starts
        again, so that clicks aimed at where its buttons appear (a stream of them, a model's tool
        can make one) are never taken until the user has stopped for the whole delay, and the click
        that such a press ends in, however long it is held, is not taken either. A press that
        begins on a prompt that takes answers, with no button already down, is the user's own."""
        entry = self._current
        if entry is None:
            return
        fresh = entry.pointers_down == 0
        entry.pointers_down += 1
        if not self.armed():
            entry.pointer_disarmed = True
            self._restart_arming(entry)
        elif fresh:
            entry.pointer_disarmed = False

    def pointer_released(self) -> None:
        """A button came up (or the press was cancelled). The click, if there is one, follows."""
        if self._current is not None:
            self._current.pointers_down = max(0, self._current.pointers_down - 1)

    def key_pressed(self) -> None:
        """The same for a key (a held Enter or Space activates the button with the focus). A key
        never ends a press that is held from before the prompt was armed."""
        entry = self._current
        if entry is None:
            return
        fresh = not entry.key_down
        entry.key_down = True
        if not self.armed():
            entry.key_disarmed = True
            self._restart_arming(entry)
        elif fresh:
            entry.key_disarmed = False

    def key_released(self) -> None:
        if self._current is not None:
            self._current.key_down = False

    def reactivated(self) -> None:
        """The window became the active one: a click that brought it to the front may be about to
        land on a button of the prompt, which has not been seen since (and the press that did it
        may have been delivered before this, so it counts as begun disarmed). The delay starts
        again."""
        entry = self._current
        if entry is not None:
            entry.pointer_disarmed = True
            entry.pointers_down = 0  # (releases can be lost with the focus)
            entry.key_down = False
            self._restart_arming(entry)

    def mark_drawn(self, prompt: PromptId) -> None:
        """The prompt's first frame is on screen (or about to be): the delay counts from here, not
        from when it was queued, so a slow first paint does not eat it."""
        if self._current is not None and self._current.card.prompt == prompt:
            self._restart_arming(self._current)

    def withdraw(self, prompt: PromptId) -> None:
        """The broker took the prompt down (its own deadline, or the request is gone): take it
        down without answering."""
        entry = self._find(prompt)
        if entry is None:
            return
        entry.cancel_deadline()
        if entry is self._current:
            self._finish_current()
        else:
            self._queue.remove(entry)

    def can_act(self, prompt: PromptId) -> bool:
        """Whether a click on `prompt` is to be taken now: it is the one shown, and armed."""
        entry = self._current
        if entry is None or entry.card.prompt != prompt:
            return False
        if entry.held_from_disarmed:  # a press held from before it took answers ends in this click
            entry.forget_held()
            return False
        return self.armed()

    def choose(self, prompt: PromptId, decision: PromptDecision) -> bool:
        """The user's decision on the prompt that is shown. Ignored (False) unless that is
        `prompt`, it is armed and the decision is one the prompt offers."""
        entry = self._current
        if entry is None or entry.card.prompt != prompt:
            return False
        if entry.held_from_disarmed:  # a press that began before the prompt took answers ends here
            entry.forget_held()
            return False
        if not self.armed():
            return False
        match decision:
            case AllowDecision():
                if decision.scope not in entry.card.scopes:
                    return False
                if (decision.picked is not None) != entry.card.picks:
                    return False
            case DenyDecision():
                pass
            case _:
                assert_never(decision)
        entry.cancel_deadline()
        self._report(prompt, decision)
        self._finish_current()
        return True

    def close(self) -> None:
        """Take everything down without answering (the window is closing)."""
        for entry in self._entries():
            entry.cancel_deadline()
            entry.cancel_arming()
        self._current = None
        self._queue.clear()
        self._view.clear()

    # --- inside ----------------------------------------------------------------------------

    def _entries(self) -> list[_Entry]:
        return [*([] if self._current is None else [self._current]), *self._queue]

    def _find(self, prompt: PromptId) -> _Entry | None:
        return next((entry for entry in self._entries() if entry.card.prompt == prompt), None)

    def _report(self, prompt: PromptId, decision: PromptDecision) -> None:
        try:
            self._sink(prompt, decision)
        except Exception:  # noqa: BLE001  (whatever the sink does, the prompt must come down)
            _log.exception("an answer to a prompt could not be sent")

    def _expired(self, entry: _Entry) -> None:
        """The deadline passed with no answer: a denial (I5), and the prompt comes down."""
        denial = DenyDecision(kind="deny", reason="prompt_timeout")
        if entry is self._current:
            self._report(entry.card.prompt, denial)
            self._finish_current()
        elif entry in self._queue:
            self._queue.remove(entry)
            self._report(entry.card.prompt, denial)

    def _finish_current(self) -> None:
        entry = self._current
        if entry is not None:
            entry.cancel_arming()
        self._current = None
        self._next()

    def _next(self) -> None:
        if not self._queue:
            self._view.clear()
            return
        entry = self._queue.popleft()
        self._current = entry
        self._start_arming(entry)
        self._view.present(entry.card)

    def _start_arming(self, entry: _Entry) -> None:
        entry.cancel_arming()
        entry.shown_at = self._clock()
        entry.cancel_arming = self._scheduler.call_later(ARM_SECONDS, lambda: self._arm(entry))

    def _restart_arming(self, entry: _Entry) -> None:
        self._start_arming(entry)
        self._view.set_armed(False)

    def _arm(self, entry: _Entry) -> None:
        if entry is not self._current:
            return
        if self.armed():
            self._view.set_armed(True)
            return
        # The timer ran before the clock got there (they are not the same clock): look again then.
        started = self._clock() if entry.shown_at is None else entry.shown_at
        remaining = max(ARM_SECONDS - (self._clock() - started), 0.01)
        entry.cancel_arming = self._scheduler.call_later(remaining, lambda: self._arm(entry))
