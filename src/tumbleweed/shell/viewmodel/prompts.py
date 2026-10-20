"""The trusted permission prompt as a state machine, with no GTK (S18, I5, I10).

`PromptController` is what decides, for the prompts the broker sends the shell, what is shown, when
it can be answered and when it is over. The widgets (`tumbleweed.shell.prompt_chrome`) only draw
its `PromptCard` and report a click; nothing on this side of the line knows about a window.

What it holds to, in the order of the invariant (I5):

- **One at a time.** At most one prompt is shown; the others wait behind it in order, at most
  `MAX_QUEUED` in all (a ninth is refused: the broker denies it). A prompt takes the place of the
  last one only when that one is answered, withdrawn or expired.
- **Not at once, and not from an input that was already under way.** The arming invariant:

  (A) An Allow or a Choose (anything that can lead to a grant) is accepted only if the input that
  produced it (the pointer press whose release clicked it, or the key press that activated it)
  began while the prompt was armed and after the last arming restart, and no input that began while
  the prompt was disarmed, or before a restart, is still held, as far as the window has seen
  (pointers are tracked until their release arrives, keys until it arrives or the window loses the
  focus). A click that a release completes is refused if the input that is released is tainted,
  whichever input produced it (fail closed).
  (B) A Deny is never refused because of an input that is held, or of how an input began: a Deny
  cannot become a grant. It is ignored whenever the prompt is disarmed (the 750 ms after it
  appears or after any restart; the plan: early clicks and Enter presses are ignored), and it
  still needs a real click or key (I10).
  (C) Nothing makes the prompt refuse a deliberate Allow for good: an input that begins after the
  prompt is armed, with nothing tainted held, is accepted, of either kind and after any other input
  (typing, a window activation). A refusal of an Allow is shown to the user, by the view. The
  cases that fail closed instead are the documented ones: an input whose release was lost (a
  pointer is replaced by its next press of the same button; a key is forgotten when the window
  loses the focus), and a click completed by a tainted input's release.

  How: every pointer button, touch (one identity per sequence) and key that goes down is recorded
  with whether the prompt was armed and in which *epoch* (every restart of the delay starts a new
  one: the prompt shown, first drawn, the window activated, any input that begins while
  disarmed). An input is *tainted* if it began disarmed or in an earlier epoch, and so is one that
  comes up with no record (it went down before the prompt, or before a focus loss). Held inputs
  are tracked exactly by identity, not by flags: a second button, a key or Enter on the focused
  Deny cannot end the hold of another input, and auto-repeat is not a new press. Pointer records
  survive a focus loss (GTK does not cancel a press when the window is deactivated: the grab goes
  on and its release arrives); keys are forgotten (their release goes to the other window). A
  release is remembered for the click that follows it in the same event, and forgotten with the
  next input or `event_done`. When too many inputs are held to record, Allows are refused until
  they are not (fail closed). The delay itself restarts at the same events, so the 750 ms are of
  quiet.
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


type InputKind = Literal["pointer", "key"]


@final
@dataclass(frozen=True, slots=True)
class InputId:
    """One input that can be held: a pointer button or touch (`code` told apart by the view), or a
    key (its hardware code, as an opaque number: no key is ever read as a character)."""

    kind: InputKind
    code: int


@final
@dataclass(frozen=True, slots=True)
class _Held:
    armed: bool  # the prompt took answers when the input began
    epoch: int  # the epoch it began in


_MAX_HELD: Final = 64


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
        self._epoch = 0  # advances at every restart of the delay
        self._held: dict[InputId, _Held] = {}
        self._overflowed = False  # more inputs went down than are recorded
        self._release = False  # the input that came up in this event began tainted

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

    def input_began(self, which: InputId) -> None:
        """A pointer button, a touch or a key went down in the window.

        If the prompt is not armed the delay starts again (a stream of clicks aimed at where a
        button will appear is never taken, and neither is one that is held), and the input is
        recorded as having begun disarmed. A key that is already down is auto-repeat, not a new
        press. A pointer that is already down cannot go down again: its release was lost, and this
        press replaces the record (the next deliberate press heals a lost release).
        """
        self._release = False  # a new input: whatever was released has had its click
        entry = self._current
        armed = self.armed()
        before = self._held.get(which)
        if entry is not None and not armed:
            self._restart_arming(entry)
        if before is not None and which.kind == "key":
            return
        if before is None and len(self._held) >= _MAX_HELD:
            self._overflowed = True  # not recorded: fail closed until fewer are held
            return
        self._held[which] = _Held(armed=armed, epoch=self._epoch)

    def input_ended(self, which: InputId, *, cancelled: bool = False) -> None:
        """The input came up. The click it makes, if any, follows in the same event, and is taken
        only if this input was not tainted: one that comes up with no record is (it went down
        before the prompt, or before the window lost the focus). A cancelled one (a touch
        cancelled) makes no click."""
        held = self._held.pop(which, None)
        if not self._held:
            self._overflowed = False
        self._release = not cancelled and (held is None or self._tainted(held))

    def event_done(self) -> None:
        """The event is over (the view calls it when the main loop is idle again): a release has
        had its click, if it was to have one."""
        self._release = False

    def deactivated(self) -> None:
        """The window lost the focus. The keys that are down are forgotten (their release goes to
        the window that has the focus now, and a key never released here must not refuse Allows
        for good); a pointer press is not (GTK does not cancel it: the grab goes on and its
        release arrives here)."""
        for which in [which for which in self._held if which.kind == "key"]:
            del self._held[which]
        self._release = False

    def reactivated(self) -> None:
        """The window became the active one: a click that brought it to the front may be about to
        land on a button of the prompt, which has not been seen since. The delay starts again, and
        a press that is already down (delivered before this) is of the epoch before: tainted."""
        if self._current is not None:
            self._restart_arming(self._current)

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
        """Whether a click on a button that can lead to a grant (the picker's) is to be taken now:
        `prompt` is the one shown and the arming invariant (A) holds."""
        entry = self._current
        if entry is None or entry.card.prompt != prompt:
            return False
        return self._grant_input_clean()

    def choose(self, prompt: PromptId, decision: PromptDecision) -> bool:
        """The user's decision on the prompt that is shown. Ignored (False) unless that is
        `prompt`, the decision is one it offers and the arming invariant holds: an Allow needs (A),
        a Deny only that the prompt is armed (B). The answer of the picker is the user's choice in
        the trusted dialog, which a click that passed `can_act` opened: it needs no input of this
        window (a stray release from the dialog must not refuse it)."""
        entry = self._current
        if entry is None or entry.card.prompt != prompt:
            return False
        if isinstance(decision, AllowDecision):
            if decision.picked is None and not self._grant_input_clean():
                return False
        elif not self.armed():
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
        self._held.clear()
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

    def _tainted(self, held: _Held) -> bool:
        """An input is tainted if it began while the prompt was not armed, or before the last
        restart of the delay (so it has been held, or is about to be released, across one)."""
        return not held.armed or held.epoch != self._epoch

    def _grant_input_clean(self) -> bool:
        """(A): armed, no tainted input held (or too many to record), and the release whose click
        this is, if it is one, not tainted. The release is used up either way."""
        released_tainted, self._release = self._release, False
        return (
            self.armed()
            and not released_tainted
            and not self._overflowed
            and not any(self._tainted(held) for held in self._held.values())
        )

    def _start_arming(self, entry: _Entry) -> None:
        self._epoch += 1
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
