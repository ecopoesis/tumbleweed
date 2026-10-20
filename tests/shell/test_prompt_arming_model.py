"""A model-based property test of the prompt's arming invariant (S18, I5), over random sequences.

The invariant (also in `viewmodel.prompts`, D25 (6) and ARCHITECTURE I5):

  (A) An Allow or a Choose (anything that can lead to a grant) is accepted only if the input that
      produced it (the pointer press whose release clicked it, or the key press that activated it)
      began while the prompt was armed and after the last arming restart, and no input that began
      while the prompt was disarmed, or before a restart, is still held, as far as the window has
      seen (pointers are tracked until their release arrives, keys until it arrives or the window
      loses the focus). A click that a release completes is refused if the input that is released
      is tainted, whichever input produced it (fail closed).
  (B) A Deny is never refused because of an input that is held, or of how an input began. It is
      ignored whenever the prompt is disarmed.
  (C) Nothing makes the prompt refuse a deliberate Allow for good: an input that begins after the
      prompt is armed, with nothing tainted held, is accepted, of either kind and after any other
      input. The cases that fail closed instead are the documented ones: an input whose release was
      lost (a pointer is replaced by its next press of the same button) and a click completed by
      a tainted input's release.

The reference model is written from that statement and from what GTK does (read in gtkbutton.c and
gtkwindow.c), and shares no code with the controller. It keeps what is physically down, apart from
what the window has been told, and for every Allow and Deny says whether the controller must take
it, may take it, or must refuse it: *safety* (accepted implies the conditions of (A)) and *liveness*
(the conditions imply accepted, except in the fail-closed cases above) are checked separately, and
the controller's own idea of when it is armed with them. What GTK does that matters here:

  - a GtkButton clicks when the pointer press that began on it is released; a keyboard
    activation (Enter, Space) starts at the key press and is completed by a 250 ms timer or by the
    release of any key, whichever is first, and then a new activation can start (auto-repeat);
  - a window that is deactivated does not cancel a pointer press (its release still arrives), but
    its keys' releases go to the window that has the focus: the window forgets the keys;
  - a drag can take a release (the button is up and the window was not told).

The random steps are pointer presses and releases (buttons 1, 2, 3 and two touches), touch cancels,
lost and stray releases, key presses and releases (Enter, Space, a letter, Shift, with auto-repeat),
the timer of a keyboard activation, the window being activated and deactivated, the clock, the
first frame, a prompt swap, a prompt that goes away and one that comes (a plain one or a picker's),
Deny clicks, a picker's click and its answer, and deliberate Allow clicks and key activations.

Runs are deterministic (`derandomize`, no example database): what passes here passes in CI.

`SUBJECT` is the seam that lets the same test run against another controller (a scratch copy of an
older one, to see what it finds).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from tests.shell.prompt_support import Answers, Clock, FakeView, Timers, pick_card, read_card
from tumbleweed.ipc.prompt_messages import AllowDecision, DenyDecision
from tumbleweed.shell.viewmodel.prompts import ARM_SECONDS, InputId, PromptController

ALLOW = AllowDecision(kind="allow", scope="task", picked=None)
PICKED = AllowDecision(kind="allow", scope="task", picked="/home/u/Documents")
DENY = DenyDecision(kind="deny", reason="user_denied")

LEFT, MIDDLE, RIGHT = (InputId("pointer", n) for n in (1, 2, 3))
FINGER_A, FINGER_B = InputId("pointer", 1000), InputId("pointer", 1001)
ENTER, SPACE, LETTER, SHIFT = (InputId("key", n) for n in (36, 65, 38, 50))
POINTERS = (LEFT, MIDDLE, RIGHT, FINGER_A, FINGER_B)
KEYS = (ENTER, SPACE, LETTER, SHIFT)
CLICKS = (LEFT, FINGER_A, FINGER_B)  # what makes a GtkButton click, if it began on it
ACTIVATING = (ENTER, SPACE)  # what starts a keyboard activation of the focused button


class Subject(Protocol):
    """What the model drives: a controller with a clock and at most one prompt shown."""

    clock: Clock

    def began(self, which: InputId) -> None: ...
    def ended(self, which: InputId, cancelled: bool = False) -> None: ...
    def event_done(self) -> None: ...
    def deactivated(self) -> None: ...
    def reactivated(self) -> None: ...
    def drawn(self) -> None: ...
    def advance(self, seconds: float) -> None: ...
    def allow(self) -> bool: ...
    def deny(self) -> bool: ...
    def picked(self) -> bool: ...
    def swap(self) -> None: ...
    def dismiss(self) -> None: ...
    def show(self, picks: bool) -> None: ...
    def present(self) -> bool: ...
    def picks(self) -> bool: ...
    def armed(self) -> bool: ...


class RealSubject:
    def __init__(self) -> None:
        self.clock = Clock()
        self._timers = Timers(self.clock)
        self.controller = PromptController(FakeView(), Answers(), self._timers, self.clock)
        self.card = read_card()
        self.show(False)

    def began(self, which: InputId) -> None:
        self.controller.input_began(which)

    def ended(self, which: InputId, cancelled: bool = False) -> None:
        self.controller.input_ended(which, cancelled=cancelled)

    def event_done(self) -> None:
        self.controller.event_done()

    def deactivated(self) -> None:
        self.controller.deactivated()

    def reactivated(self) -> None:
        self.controller.reactivated()

    def drawn(self) -> None:
        self.controller.mark_drawn(self.card.prompt)

    def advance(self, seconds: float) -> None:
        self._timers.advance(seconds)

    def allow(self) -> bool:
        """The click on a button that can lead to a grant: Allow, or a picker's Choose."""
        if self.card.picks:
            return self.controller.can_act(self.card.prompt)
        return self.controller.choose(self.card.prompt, ALLOW)

    def deny(self) -> bool:
        return self.controller.choose(self.card.prompt, DENY)

    def picked(self) -> bool:
        return self.controller.choose(self.card.prompt, PICKED)

    def swap(self) -> None:
        self.controller.withdraw(self.card.prompt)
        self.show(self.card.picks)

    def dismiss(self) -> None:
        self.controller.withdraw(self.card.prompt)

    def show(self, picks: bool) -> None:
        if not self.controller.pending:
            self.card = pick_card() if picks else read_card()
            self.controller.offer(self.card, 100_000.0)
        else:
            self.card = self.controller.current or self.card

    def present(self) -> bool:
        return self.controller.pending

    def picks(self) -> bool:
        return self.card.picks

    def armed(self) -> bool:
        return self.controller.armed()


SUBJECT: Callable[[], Subject] = RealSubject


@dataclass
class Down:
    """What the model knows of an input the window has been told is down."""

    armed: bool  # the prompt took answers when it went down
    epoch: int  # the model's own count of restarts of the delay when it did
    on_button: bool  # it went down on the button that its click would answer


class ArmingMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.subject = SUBJECT()
        self.epoch = 0
        self.restart_at = self.subject.clock.now
        self.shown = True
        self.down: dict[InputId, Down] = {}  # physically down, and the window has been told
        self.forgotten: set[InputId] = set()  # keys physically down that the window forgot
        self.stale: dict[InputId, Down] = {}  # physically up, the window was not told (lost)
        self.inflight: Down | None = None  # a keyboard activation under way: its activating press
        self.pick_open = False
        self.restart()

    # -- the model -----------------------------------------------------------------------------

    def restart(self) -> None:
        self.epoch += 1
        self.restart_at = self.subject.clock.now

    def armed(self) -> bool:
        return self.shown and self.subject.clock.now - self.restart_at >= ARM_SECONDS

    def tainted(self, down: Down) -> bool:
        return not down.armed or down.epoch != self.epoch

    def tainted_held(self) -> bool:
        return any(self.tainted(d) for d in self.down.values())

    def tainted_stale(self) -> bool:
        return any(self.tainted(d) for d in self.stale.values())

    def done(self) -> None:
        self.subject.event_done()

    def check_click(self, producer: Down | None, releaser: Down | None, unrecorded: bool) -> None:
        """A button clicked. Safe: accepted only if armed, its producer clean, nothing tainted
        held, and the release that completed it (if any) clean. Live: all of those, and nothing
        stale that the window still believes held, and it is taken."""
        if not self.shown:
            return
        safe = (
            self.armed()
            and (producer is None or not self.tainted(producer))
            and not self.tainted_held()
            and not (releaser is not None and self.tainted(releaser))
            and not unrecorded
        )
        live = safe and not self.tainted_stale()
        got = self.subject.allow()
        assert not (got and not safe), (
            f"UNSAFE: an Allow was accepted that (A) refuses (armed={self.armed()}, producer "
            f"tainted={None if producer is None else self.tainted(producer)}, held tainted="
            f"{self.tainted_held()}, releaser tainted="
            f"{None if releaser is None else self.tainted(releaser)}, unrecorded={unrecorded})"
        )
        assert not (live and not got), (
            f"STUCK: a deliberate Allow was refused that (C) requires (armed={self.armed()}, "
            f"held={ {k: self.tainted(v) for k, v in self.down.items()} })"
        )
        if got:
            if self.subject.picks():
                self.pick_open = True  # the dialog is open; the prompt goes on
            else:
                self.next_prompt()

    def next_prompt(self) -> None:
        self.subject.show(False)
        self.restart()  # a new prompt: everything held is of an earlier epoch
        self.shown = True
        self.inflight = None
        self.pick_open = False

    # -- inputs -----------------------------------------------------------------------------------

    def record_press(self, which: InputId, on_button: bool) -> None:
        armed = self.armed()
        if self.shown and not armed:
            self.restart()
        self.down[which] = Down(armed, self.epoch, on_button)
        self.subject.began(which)

    @rule(which=st.sampled_from(POINTERS), on_button=st.booleans())
    def pointer_down(self, which: InputId, on_button: bool) -> None:
        if which in self.down:
            return  # the hardware never presses a held button twice
        self.stale.pop(which, None)  # a press of a button whose release was lost replaces it
        self.record_press(which, on_button)
        self.done()

    @rule(which=st.sampled_from(KEYS), on_button=st.booleans())
    def key_down(self, which: InputId, on_button: bool) -> None:
        if which in self.forgotten:
            return  # (it is physically down in another window)
        if which in self.down:  # auto-repeat: not a new press, but it restarts a disarmed prompt
            if self.shown and not self.armed():
                self.restart()
            self.subject.began(which)
            if which in ACTIVATING and self.down[which].on_button and self.inflight is None:
                self.inflight = self.down[which]  # the keyboard repeats an activation
            self.done()
            return
        self.record_press(which, on_button)
        if which in ACTIVATING and on_button and self.inflight is None:
            self.inflight = self.down[which]
        self.done()

    @precondition(lambda self: bool(self.down) or bool(self.forgotten))
    @rule(data=st.data())
    def release(self, data: st.DataObject) -> None:
        choices = sorted([*self.down, *self.forgotten], key=lambda i: (i.kind, i.code))
        which = data.draw(st.sampled_from(choices))
        if which in self.down:
            record: Down | None = self.down.pop(which)
        else:
            self.forgotten.discard(which)
            record = None  # the window never sees it come up with a record
        self.subject.ended(which)
        unrecorded = record is None
        if which.kind == "pointer":
            if record is not None and record.on_button and which in CLICKS:
                self.check_click(record, None, unrecorded)  # the button clicks on the release
        elif self.inflight is not None:
            # Any key's release completes the activation under way (GTK): the click is the
            # activator's, and fail closed if the key that comes up began tainted.
            activator, self.inflight = self.inflight, None
            self.check_click(activator, record, unrecorded)
        self.done()

    @precondition(lambda self: self.inflight is not None)
    @rule()
    def activation_timer(self) -> None:
        """The 250 ms of a keyboard activation pass with its key still down."""
        assert self.inflight is not None
        activator, self.inflight = self.inflight, None
        self.check_click(activator, None, False)
        self.done()

    @precondition(lambda self: any(i.kind == "pointer" for i in self.down))
    @rule(data=st.data())
    def release_lost(self, data: st.DataObject) -> None:
        """A drag takes the release: the button is up and the window is not told."""
        which = data.draw(
            st.sampled_from(
                sorted((i for i in self.down if i.kind == "pointer"), key=lambda i: i.code)
            )
        )
        self.stale[which] = self.down.pop(which)

    @precondition(lambda self: any(i in (FINGER_A, FINGER_B) for i in self.down))
    @rule(data=st.data())
    def touch_cancelled(self, data: st.DataObject) -> None:
        which = data.draw(st.sampled_from([i for i in (FINGER_A, FINGER_B) if i in self.down]))
        del self.down[which]
        self.subject.ended(which, cancelled=True)  # no click
        self.done()

    @rule(which=st.sampled_from(POINTERS))
    def release_stray(self, which: InputId) -> None:
        """A release comes with no press that was seen (it went down in another window)."""
        if which in self.down or which in self.stale:
            return
        self.subject.ended(which)
        self.done()  # (no button began on it: nothing clicks)

    # -- the window and the prompt --------------------------------------------------------------

    @precondition(lambda self: self.shown)
    @rule()
    def deny_clicked(self) -> None:
        expected = self.armed()
        got = self.subject.deny()
        assert got == expected, f"Deny was {'taken' if got else 'ignored'}, expected {expected}"
        if got:
            self.next_prompt()
        self.done()

    @precondition(lambda self: self.shown and self.pick_open)
    @rule()
    def picker_answered(self) -> None:
        """The dialog's answer: the user's choice, which no input of this window can refuse."""
        assert self.subject.picked(), "a picker's answer was refused"
        self.next_prompt()
        self.done()

    @rule(seconds=st.sampled_from([0.125, 0.25, 0.5, 0.75, 1.0]))
    def wait(self, seconds: float) -> None:
        self.subject.advance(seconds)

    @rule()
    def window_activated(self) -> None:
        self.subject.reactivated()
        if self.shown:
            self.restart()
        self.done()

    @rule()
    def window_deactivated(self) -> None:
        self.subject.deactivated()
        for which in [i for i in self.down if i.kind == "key"]:
            del self.down[which]
            self.forgotten.add(which)  # physically down; the window will not see it come up
        self.inflight = None
        self.done()

    @precondition(lambda self: self.shown)
    @rule()
    def first_frame(self) -> None:
        self.subject.drawn()
        self.restart()
        self.done()

    @precondition(lambda self: self.shown)
    @rule()
    def prompt_swapped(self) -> None:
        self.subject.swap()
        self.restart()
        self.inflight = None
        self.pick_open = False
        self.done()

    @precondition(lambda self: self.shown)
    @rule()
    def prompt_goes(self) -> None:
        """The broker takes the prompt down, and nothing is shown until the next one."""
        self.subject.dismiss()
        self.shown = False
        self.inflight = None
        self.pick_open = False
        self.done()

    @precondition(lambda self: not self.shown)
    @rule(picks=st.booleans())
    def prompt_comes(self, picks: bool) -> None:
        self.subject.show(picks)
        self.shown = True
        self.restart()
        self.done()

    @precondition(lambda self: self.shown)
    @rule(use_key=st.booleans())
    def deliberate_input(self, use_key: bool) -> None:
        """(C): the user waits for the prompt, then clicks (or presses Enter) on the button. If
        nothing tainted is held and nothing is stale it must be taken, whatever came before."""
        if self.tainted_held() or self.tainted_stale() or self.subject.armed() != self.armed():
            return
        self.subject.advance(ARM_SECONDS)
        which = ENTER if use_key else LEFT
        if which in self.down or which in self.forgotten or self.inflight is not None:
            return
        self.record_press(which, True)
        if use_key:
            self.inflight = None
            self.check_click(self.down[which], None, False)  # the activation, by its timer
            self.subject.ended(which)
            del self.down[which]
        else:
            record = self.down.pop(which)
            self.subject.ended(which)
            self.check_click(record, None, False)
        self.done()

    @invariant()
    def the_controller_knows_when_it_is_armed(self) -> None:
        assert self.subject.armed() == self.armed()


TestArmingModel = ArmingMachine.TestCase
TestArmingModel.settings = settings(
    max_examples=1500,
    stateful_step_count=60,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)
