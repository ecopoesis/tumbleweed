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
      produced it is recorded, produced a click on that very button, began while the prompt was
      armed and after the last arming restart, and no input that began while the prompt was
      disarmed, or before a restart, is still held, as far as the window has seen (pointers are
      tracked until their release arrives or the next prompt comes up, keys until it arrives or the
      window loses the focus). The producer is the pointer press whose release came on that button,
      or the press of Enter or Space, bare, that started a keyboard activation of that button while
      it had the focus, in the last half second; a key's release in that half second is taken for
      the completion of an activation under way, and the release of the activating key itself ends
      it. A click that a release completes is refused if the input that is released is tainted,
      whichever input produced it (fail closed). A click with no producing input (a synthetic one,
      such as an accessibility DoAction), or whose producer belongs to another button, is refused,
      and the delay starts again. The answer of the picker's dialog is taken only after a Choose
      that was granted at that scope.
  (B) A Deny is never refused because of an input that is held, or of how an input began: a Deny
      cannot become a grant. It is ignored whenever the prompt is disarmed (the 750 ms after it
      appears or after any restart; the plan: early clicks and Enter presses are ignored), and
      otherwise taken from any click, a synthetic one included, because it cannot grant (a hostile
      client of the accessibility bus can deny a user's prompts, delay their grants by restarting
      the delay, and make the user's own Deny be ignored until they retry, the deadline still
      denying: that costs liveness, not safety; and with no attack at all, a Deny pressed within
      0.75 s of the user's own refused Allow is ignored, and the note is shown).
  (C) Nothing makes the prompt refuse a deliberate Allow for good: an input that begins after the
      prompt is armed, with nothing tainted held, is accepted, of either kind and after any other
      input (typing, a window activation, a press during a keyboard activation, which only the press
      of another activating key replaces, and its own key's release, the half second, a focus loss
      or a restart end). A refusal of an Allow is shown to the user, by the view. The cases that
      fail closed instead are the documented ones: an input whose release was lost (a pointer is
      replaced by its next press of the same button of the same device, or dropped when the next
      prompt comes up, so a lost release costs one prompt at most; a key is forgotten when the
      window loses the focus), an input that comes up with no record (it went down before the prompt
      or a focus loss, was dropped, or was one too many to record: at most 64 are held), a click
      completed by a tainted input's release, and two mice that the system merges into one device
      (their same-numbered buttons are one input to the window, so a press of the second replaces
      the record of the first). Exactly, for Enter on Allow followed by a press on Deny during GTK's
      activation: if Enter is still held when GTK's 250 ms timer fires, GTK clicks Allow and it is
      taken (the user's own Enter; GTK does the same without the shell); if Enter comes up before
      that timer, after the press moved the focus to Deny, the activation ends at that release,
      GTK's timer click on Allow is refused and the delay restarts, so a Deny that clicks after that
      is ignored too: no answer, the note is shown, and a retry works (fail closed). What remains,
      outside the threat model (a same-user process, ARCHITECTURE section 8): a synthetic click on
      the very button the user is activating, in the same event as their clean release on it or
      within half a second of their clean Enter or Space on it, is taken as theirs. The picker's
      dialog is GTK's or the portal's own window and can be driven over the accessibility bus like
      any window of the session; what protects it is that it opens only after a granted Choose, that
      its answer is taken only then, only once, and not after the dialog was dismissed or the delay
      restarted, and that the broker still applies the deny rules, the taint rule and the cap to
      what it picks. Assistive-technology users grant with Enter or Space on the focused button; a
      DoAction is refused. A bus client cannot move the keyboard focus onto Allow: GTK's AT-SPI
      Component.GrabFocus answers NotSupported (gtk/a11y/gtkatspicomponent.c, GTK 4.14.5; the same
      in 4.16, 4.18 and main), and the Action interface's DoAction only calls gtk_widget_activate,
      which takes no focus. A GTK that implemented GrabFocus would let the user's Enter meant for
      Deny activate Allow, from a same-user process, outside the threat model: re-check it on a GTK
      upgrade.

  How: every pointer button (told apart by its device: a mouse and a pen both have a button 1),
  touch (one identity per sequence) and key that goes down is recorded with whether the prompt was
  armed and in which *epoch* (every restart of the delay starts a new one: the prompt shown, first
  drawn, the window activated, any input that begins while disarmed, a refused grant). An input is
  *tainted* if it began disarmed or in an earlier epoch, and so is one that comes up with no record
  (it went down before the prompt, or before a focus loss, or was dropped, or was one too many: at
  most `_MAX_HELD` are recorded). Held inputs are tracked exactly by identity, not by flags: a
  second button, a key or Enter on the focused Deny cannot end the hold of another input, and
  auto-repeat is not a new press. Pointer records survive a focus loss (GTK does not cancel a press
  when the window is deactivated: the grab goes on and its release arrives) but not the next prompt
  (a release that never comes, because a drag took it, must not refuse Allows for good: it is
  dropped, and its release, if it comes, is unrecorded and so tainted); keys are forgotten on the
  focus loss (their release goes to the other window). A click needs a producing input of its own
  button (`input_ended` and `released_on`, or `input_began(activates=...)`): the release that came
  on that button, remembered for the click that follows it in the same event (forgotten with the
  next input or `event_done`, which the view runs at high priority so that nothing at the default
  one sees it), or a clean press of Enter or Space, bare, while that button has the focus, in the
  last `ACTIVATION_SECONDS` (GTK completes a keyboard activation with a 250 ms timer or the next key
  release, and does not cancel it when anything else goes down: only the press of a key that
  activates replaces it here). One with neither, as an accessibility `DoAction` is, or
  on another button than its producer's, is refused and starts the delay again. The delay itself
  restarts at the same events, so the 750 ms are of quiet.
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
from typing import Final, Literal, NewType, Protocol, assert_never, final

from tumbleweed.agent.canvas_events import Scheduler
from tumbleweed.core.ids import PromptId, TaskId, ToolId
from tumbleweed.ipc.broker_messages import (
    NetworkAccess,
    NoUndoAccess,
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

type CardKind = Literal["read", "write", "network", "pick_file", "pick_folder", "no_undo"]


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
    task_title: str | None = None
    """The title the assistant gave the task (plain, capped), or None: set apart as its words."""
    paths: tuple[str, ...] = ()
    """For `no_undo`: the places the run may change, spelled out (`visible`), and `cause` why they
    could not be saved: the broker's closed code, shown as our own sentence."""
    cause: str | None = None
    purpose: str = "run"

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


NETWORK_WARNING: Final = (
    "WARNING. Code the assistant runs would be able to reach any server your computer can "
    "reach, send anything it holds to it and fetch anything from it, including other "
    "machines on your network and the programs on this computer that accept network "
    "connections. Nothing it sends or fetches is checked. While the network is "
    "allowed the assistant gets no access to your files, and a task that has seen your files "
    "cannot be given the network. Allow it only if you trust what it is about to do."
)
"""What a network prompt says, in our words (D29 (10)). Shown as plain text, with the card."""

MAX_TITLE_SHOWN: Final = 80
"""The most characters of a task's title that are shown (I5: capped)."""


def card_of(show: ShowPrompt, task_title: str | None = None) -> PromptCard:
    """The card for a prompt from the broker. The capability is read from its structure.

    `task_title` is the assistant's name for the task, looked up by the shell (#142); it is made a
    plain line and capped here as the reason is, whoever gave it."""
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
        case NoUndoAccess():
            kind = "no_undo"
        case _:
            assert_never(access)
    reason, cut = plain_reason(show.reason)
    title = None if not task_title else plain_reason(task_title, MAX_TITLE_SHOWN)[0] or None
    return PromptCard(
        prompt=show.prompt,
        task=show.task,
        tool=show.tool,
        kind=kind,
        path=path,
        scopes=show.scopes,
        reason=reason,
        reason_cut=cut,
        task_title=title,
        paths=tuple(visible(each) for each in access.paths)
        if isinstance(access, NoUndoAccess)
        else (),
        cause=access.cause if isinstance(access, NoUndoAccess) else None,
        purpose=access.purpose if isinstance(access, NoUndoAccess) else "run",
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
        self.picking: Scope | None = None  # a granted Choose opened the dialog, at this scope


type InputKind = Literal["pointer", "key"]

DeviceNo = NewType("DeviceNo", int)
"""An opaque number the view gives each input device (never reused for another one)."""
NO_DEVICE: Final = DeviceNo(0)


@final
@dataclass(frozen=True, slots=True)
class InputId:
    """One input that can be held: a pointer button or touch (`code` told apart by the view, and
    `device` the input device it came from: a mouse and a pen both have a button 1), or a key (its
    hardware code, as an opaque number: no key is ever read as a character)."""

    kind: InputKind
    code: int
    device: DeviceNo = NO_DEVICE


@final
@dataclass(frozen=True, slots=True)
class _Held:
    armed: bool  # the prompt took answers when the input began
    epoch: int  # the epoch it began in


_UNRECORDED: Final = _Held(armed=False, epoch=-1)
_MAX_HELD: Final = 64
ACTIVATION_SECONDS: Final = 0.5
"""How long a key press that starts an activation can be the producer of a click. GTK completes
an activation 250 ms after the press (a timer) or at the next key release; the rest is headroom
for a loaded host, and no more, because a synthetic click on the same button inside this window
is taken for the user's."""


@final
@dataclass(frozen=True, slots=True)
class _Released:
    """An input that came up in this event, and the grant button the click it makes would be on
    (None: not one)."""

    held: _Held
    target: Scope | None


@final
@dataclass(frozen=True, slots=True)
class _Activation:
    """A clean press of a key that starts a keyboard activation of the grant button `target`."""

    key: InputId
    target: Scope
    at: float


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
        # What a click that comes now was made by, and on which grant button: the input that came
        # up in this event, or a clean key press that began an activation of that button, which
        # the click follows with GTK's timer or at the next key release.
        self._released: _Released | None = None
        self._activation: _Activation | None = None

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

    def input_began(self, which: InputId, *, activates: Scope | None = None) -> None:
        """A pointer button, a touch or a key went down in the window. `activates` is the grant
        button that the key starts a keyboard activation of: the view says it only for Enter or
        Space, with no modifier, pressed while that button has the focus.

        If the prompt is not armed the delay starts again (a stream of clicks aimed at where a
        button will appear is never taken, and neither is one that is held), and the input is
        recorded as having begun disarmed. A key that is already down is auto-repeat, not a new
        press (it activates again if it is one that activates). A pointer that is already down
        cannot go down again from the same device: its release was lost, and this press replaces
        the record (the next deliberate press heals a lost release). At most `_MAX_HELD` are
        recorded; one more is not, and so is tainted when it comes up. An activation under way
        is not ended by any other press (GTK does not cancel one either): only by the press of a
        key that activates, which replaces it.
        """
        self._released = None  # a new input: whatever was released has had its click
        if activates is not None:
            self._activation = None  # (replaced below; a press makes no click by itself)
        entry = self._current
        armed = self.armed()
        before = self._held.get(which)
        if entry is not None and not armed:
            self._restart_arming(entry)
        if before is not None and which.kind == "key":
            held = before
        elif before is None and len(self._held) >= _MAX_HELD:
            held = _UNRECORDED
        else:
            held = _Held(armed=armed, epoch=self._epoch)
            self._held[which] = held
        if activates is not None and which.kind == "key" and not self._tainted(held):
            self._activation = _Activation(which, activates, self._clock())

    def input_ended(self, which: InputId, *, cancelled: bool = False) -> None:
        """The input came up. The click it makes, if any, follows in the same event, and is taken
        only if this input was not tainted: one that comes up with no record is (it went down
        before the prompt, or before the window lost the focus). A cancelled one (a touch
        cancelled) makes no click. The release of a key completes the keyboard activation under
        way (GTK), so its click is on that activation's button; a pointer's is on the button it
        was released on, which the view says next (`released_on`)."""
        held = self._held.pop(which, None)
        activation = self._activation
        if activation is not None and self._clock() - activation.at > ACTIVATION_SECONDS:
            activation = None
        target = activation.target if activation is not None and which.kind == "key" else None
        self._released = (
            None if cancelled else _Released(_UNRECORDED if held is None else held, target)
        )
        if activation is not None and activation.key == which:
            self._activation = None  # (the key that activated is up: its release is the producer)

    def released_on(self, target: Scope) -> None:
        """The pointer input that just came up was released on the grant button `target`: the
        click it makes, if any, is on that button, and no other."""
        released = self._released
        if released is not None:
            self._released = _Released(released.held, target)

    def event_done(self) -> None:
        """The event is over (the view calls it when the main loop is idle again): a release has
        had its click, if it was to have one. (A key press that starts an activation stays the
        producer: the click comes with GTK's timer, or the next key release.)"""
        self._released = None

    def deactivated(self) -> None:
        """The window lost the focus. The keys that are down are forgotten (their release goes to
        the window that has the focus now, and a key never released here must not refuse Allows
        for good); a pointer press is not (GTK does not cancel it: the grab goes on and its
        release arrives here)."""
        for which in [which for which in self._held if which.kind == "key"]:
            del self._held[which]
        self._released = None
        self._activation = None

    def reactivated(self) -> None:
        """The window became the active one: a click that brought it to the front may be about to
        land on a button of the prompt, which has not been seen since. The delay starts again, and
        a press that is already down (delivered before this) is of the epoch before: tainted."""
        self._activation = None
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

    def can_act(self, prompt: PromptId, scope: Scope) -> bool:
        """Whether a click on the picker's button at `scope` is to be taken now: `prompt` is the
        one shown and the arming invariant (A) holds. If so the dialog may be opened, and its
        answer (and no other) is taken."""
        entry = self._current
        if entry is None or entry.card.prompt != prompt:
            return False
        if not entry.card.picks or scope not in entry.card.scopes:
            return False
        if not self._grant_input_clean(scope):
            return False
        entry.picking = scope
        return True

    def picker_dismissed(self, prompt: PromptId) -> None:
        """The dialog that a granted Choose opened gave no answer (it was dismissed, or the name
        cannot be sent): it is over, and no answer is taken until another Choose is granted."""
        entry = self._current
        if entry is not None and entry.card.prompt == prompt:
            entry.picking = None

    def choose(self, prompt: PromptId, decision: PromptDecision) -> bool:
        """The user's decision on the prompt that is shown. Ignored (False) unless that is
        `prompt`, the decision is one it offers and the arming invariant holds: an Allow needs (A),
        a Deny only that the prompt is armed (B), from any click, a synthetic one included: it
        cannot grant. The answer of the picker is the user's choice in the trusted dialog, which a
        click that passed `can_act` at that scope opened, and is taken only then; it needs no input
        of this window (a stray release from the dialog must not refuse it)."""
        entry = self._current
        if entry is None or entry.card.prompt != prompt:
            return False
        if isinstance(decision, AllowDecision):
            if decision.picked is None:
                if entry.card.picks or decision.scope not in entry.card.scopes:
                    return False  # (before the producer is used up: a malformed call costs none)
                if not self._grant_input_clean(decision.scope):
                    return False
            elif entry.picking != decision.scope:
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
        # A release that was lost (a drag took it, a touch end never came) must not refuse every
        # Allow of the prompts to come: what the pointers held is dropped, so that their release,
        # if it comes, is unrecorded and so tainted (fail closed), and no longer blocks.
        for which in [which for which in self._held if which.kind == "pointer"]:
            del self._held[which]
        self._released = None
        self._start_arming(entry)
        self._view.present(entry.card)

    def _tainted(self, held: _Held) -> bool:
        """An input is tainted if it began while the prompt was not armed, or before the last
        restart of the delay (so it has been held, or is about to be released, across one)."""
        return not held.armed or held.epoch != self._epoch

    def _grant_input_clean(self, scope: Scope) -> bool:
        """(A) for a click on the grant button `scope`: armed, a producing input of that button
        recorded and not tainted, and no tainted input held. The producer is the input that came
        up in this event, or else a clean press of a key that began an activation of that button
        in the last `ACTIVATION_SECONDS`; they are used up either way. A click with neither (a
        synthetic one, from the accessibility bus), or on another button than the producer's, is
        refused, and the delay starts again, so that a stream of them is never armed."""
        released, self._released = self._released, None
        activation, self._activation = self._activation, None
        if released is not None:
            produced = released.target == scope and not self._tainted(released.held)
        else:
            produced = (
                activation is not None
                and activation.target == scope
                and self._clock() - activation.at <= ACTIVATION_SECONDS
            )
        clean = (
            self.armed()
            and produced
            and not any(self._tainted(held) for held in self._held.values())
        )
        if not clean and self._current is not None:
            self._restart_arming(self._current)
        return clean

    def _start_arming(self, entry: _Entry) -> None:
        self._epoch += 1
        entry.picking = None  # (a dialog that outlives a restart is not the user's choice any more)
        self._activation = None  # (it began in the epoch before)
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
