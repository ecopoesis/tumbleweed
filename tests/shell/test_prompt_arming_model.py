"""A model-based property test of the prompt's arming invariant (S18, I5), over random sequences.

The invariant (also in `viewmodel.prompts`, D25 (6) and ARCHITECTURE I5):

  (A) An Allow or a Choose (anything that can lead to a grant) is accepted only if the input that
      produced it (the pointer press whose release clicked it, or the key press that activated it)
      is recorded, began while the prompt was armed and after the last arming restart, and no
      input that began while the prompt was disarmed, or before a restart, is still held, as far
      as the window has seen (pointers are tracked until their release arrives, or until the next
      prompt comes up; keys until it arrives or the window loses the focus). A click that a
      release completes is refused if the input that is released is tainted, whichever input
      produced it (fail closed). A click with no producing input (a synthetic one), or whose
      producer is another button's, is refused, and the delay starts again.
  (B) A Deny is never refused because of an input that is held, or of how an input began. It is
      ignored whenever the prompt is disarmed, and otherwise taken from any click (it cannot
      grant).
  (C) Nothing makes the prompt refuse a deliberate Allow for good: an input that begins after the
      prompt is armed, with nothing tainted held, is accepted, of either kind and after any other
      input. The cases that fail closed instead are the documented ones: an input whose release
      was lost (a pointer is replaced by its next press of the same button of the same device, or
      dropped when the next prompt comes up), an input that is released with no record, and a
      click completed by a tainted input's release.

The reference model is written from that statement and from what GTK does (read in gtkbutton.c and
gtkwindow.c), and shares no code with the controller: its inputs are its own tuples (`Phys`), which
only the subject's adapter turns into the controller's `InputId`. It keeps what is physically down,
apart from what the window has been told, and for every Allow and Deny says whether the controller
must take it, may take it, or must refuse it: *safety* (accepted implies the conditions of (A)) and
*liveness* (the conditions imply accepted, except in the fail-closed cases above) are checked
separately, and the controller's own idea of when it is armed with them. What GTK does that
matters here:

  - a GtkButton clicks when the pointer press that began on it is released (the release is
    delivered to that button); a keyboard activation (Enter, Space, bare, with the button focused)
    starts at the key press and is completed by a 250 ms timer or by the release of any key,
    whichever is first, and then a new activation can start (auto-repeat); anything else going down
    does not cancel it;
  - a window that is deactivated does not cancel a pointer press (its release still arrives), but
    its keys' releases go to the window that has the focus: the window forgets the keys;
  - a drag can take a release (the button is up and the window was not told);
  - a mouse and a pen both have a button 1, as two devices; a touch sequence is new each time, and
    the end of one that began elsewhere arrives with no press seen.

The random steps are pointer presses and releases (buttons 1, 2, 3, a pen's, and touches, each a new
sequence), touch cancels, lost and stray releases (of a key too: it completes an activation under
way), key presses and releases (Enter, Space, a letter, Shift, with auto-repeat),
the timer of a keyboard activation, the window being activated and deactivated, the clock, the
first frame, a click with no input behind it, a prompt swap, a prompt that goes away and one that
comes (a plain one or a picker's), Deny clicks, a picker's click and its answer, and deliberate
Allow clicks and key activations. A few scripted sequences (`TestScripted`) pin the cases that the
random runs reach too rarely at the CI budget: Enter on Deny while a synthetic Allow is in flight,
an activation that expires or is ended by a press, an unrecorded release, a pen over a mouse.

Runs are deterministic (`derandomize`, no example database): what passes here passes in CI.

`SUBJECT` is the seam that lets the same test run against another controller (a scratch copy of an
older one, to see what it finds).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal, NamedTuple, Protocol

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from tests.shell.prompt_support import Answers, Clock, FakeView, Timers, pick_card, read_card
from tumbleweed.ipc.prompt_messages import AllowDecision, DenyDecision
from tumbleweed.shell.viewmodel.prompts import (
    ACTIVATION_SECONDS,
    ARM_SECONDS,
    DeviceNo,
    InputId,
    PromptController,
)

ALLOW = AllowDecision(kind="allow", scope="task", picked=None)
ALLOW_OTHER = AllowDecision(kind="allow", scope="once", picked=None)
PICKED = AllowDecision(kind="allow", scope="task", picked="/home/u/Documents")
DENY = DenyDecision(kind="deny", reason="user_denied")
BUTTON: Final = "task"  # the grant button the model's inputs go down on; "once" is the other one


class Phys(NamedTuple):
    """An input as the model knows it: its own tuple, not the controller's `InputId`."""

    kind: str  # "pointer" or "key"
    code: int  # the button, the touch sequence or the hardware key code
    device: int = 0


LEFT, MIDDLE, RIGHT = (Phys("pointer", n) for n in (1, 2, 3))
PEN = Phys("pointer", 1, 2)  # the same button number as LEFT, on another device
FIRST_FINGER = 1000  # a touch sequence is a new id each time
POINTERS = (LEFT, MIDDLE, RIGHT, PEN)
ENTER, SPACE, LETTER, SHIFT = (Phys("key", n) for n in (36, 65, 38, 50))
KEYS = (ENTER, SPACE, LETTER, SHIFT)
ACTIVATING = (ENTER, SPACE)  # what starts a keyboard activation of the focused button


def clicks(which: Phys) -> bool:
    """Whether its release makes a GtkButton click, if it began on it: the primary button of a
    device, or a touch."""
    return which.kind == "pointer" and (which.code == 1 or which.code >= FIRST_FINGER)


class Subject(Protocol):
    """What the model drives: a controller with a clock and at most one prompt shown."""

    clock: Clock

    def began(self, which: Phys, activates: bool) -> None: ...
    def ended(self, which: Phys, cancelled: bool = False) -> None: ...
    def released_on_button(self) -> None: ...
    def event_done(self) -> None: ...
    def deactivated(self) -> None: ...
    def reactivated(self) -> None: ...
    def drawn(self) -> None: ...
    def advance(self, seconds: float) -> None: ...
    def allow(self, other: bool = False) -> bool: ...
    def deny(self) -> bool: ...
    def picked(self) -> bool: ...
    def dismiss_picker(self) -> None: ...
    def swap(self) -> None: ...
    def dismiss(self) -> None: ...
    def show(self, picks: bool) -> None: ...
    def present(self) -> bool: ...
    def picks(self) -> bool: ...
    def armed(self) -> bool: ...


def _input(which: Phys) -> InputId:
    kind: Literal["pointer", "key"] = "key" if which.kind == "key" else "pointer"
    return InputId(kind, which.code, DeviceNo(which.device))


class RealSubject:
    def __init__(self) -> None:
        self.clock = Clock()
        self._timers = Timers(self.clock)
        self.controller = PromptController(FakeView(), Answers(), self._timers, self.clock)
        self.card = read_card()
        self.show(False)

    def began(self, which: Phys, activates: bool) -> None:
        self.controller.input_began(_input(which), activates=BUTTON if activates else None)

    def ended(self, which: Phys, cancelled: bool = False) -> None:
        self.controller.input_ended(_input(which), cancelled=cancelled)

    def released_on_button(self) -> None:
        self.controller.released_on(BUTTON)

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

    def allow(self, other: bool = False) -> bool:
        """The click on a button that can lead to a grant: Allow, or a picker's Choose (`other`:
        the one that no input of the model went down on)."""
        decision = ALLOW_OTHER if other else ALLOW
        if self.card.picks:
            return self.controller.can_act(self.card.prompt, decision.scope)
        return self.controller.choose(self.card.prompt, decision)

    def deny(self) -> bool:
        return self.controller.choose(self.card.prompt, DENY)

    def picked(self) -> bool:
        return self.controller.choose(self.card.prompt, PICKED)

    def dismiss_picker(self) -> None:
        self.controller.picker_dismissed(self.card.prompt)

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
    on_button: bool  # it went down on the button that its click would answer (or, for a key,
    # the focus was on it)


UNSEEN = Down(armed=False, epoch=-1, on_button=True)  # an input the window has no record of


class ArmingMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.subject = SUBJECT()
        self.epoch = 0
        self.restart_at = self.subject.clock.now
        self.shown = True
        self.down: dict[Phys, Down] = {}  # physically down, and the window has been told
        self.forgotten: dict[Phys, bool] = {}  # physically down, no record: was it on the button
        self.stale: dict[Phys, Down] = {}  # physically up, the window was not told (lost)
        self.inflight: Down | None = None  # a keyboard activation under way: its activating press
        self.inflight_at = 0.0  # when it began
        self.activating_at: float | None = None  # the last activating key press on the button
        self.fingers = 0  # touch sequences so far
        self.pick_open = False
        self.restart()

    # -- the model -----------------------------------------------------------------------------

    def restart(self) -> None:
        self.epoch += 1
        self.pick_open = False  # a dialog that outlives a restart is not the user's choice
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

    def drop_pointers(self) -> None:
        """A prompt comes up: the window drops the pointers it holds, whose release (if it comes) is
        then one with no record."""
        for which in [i for i in self.down if i.kind == "pointer"]:
            self.forgotten[which] = self.down.pop(which).on_button
        self.stale.clear()

    def start_activation(self, record: Down) -> None:
        self.activating_at = self.subject.clock.now
        if self.inflight is None:
            self.inflight, self.inflight_at = record, self.activating_at

    def check_click(
        self,
        producer: Down,
        releaser: Down | None,
        unrecorded: bool,
        producer_ok: bool = True,
    ) -> None:
        """The button clicked. Safe: accepted only if armed, its producer clean (and, for an
        activation, not over), nothing tainted held, and the release that completed it (if any)
        clean. Live: all of those, and nothing stale that the window still believes held, and it
        is taken. A click that is refused starts the delay again."""
        if not self.shown:
            return
        safe = (
            self.armed()
            and producer_ok
            and not self.tainted(producer)
            and not self.tainted_held()
            and not (releaser is not None and self.tainted(releaser))
            and not unrecorded
        )
        live = safe and not self.tainted_stale()
        got = self.subject.allow()
        assert not (got and not safe), (
            f"UNSAFE: an Allow was accepted that (A) refuses (armed={self.armed()}, producer "
            f"tainted={self.tainted(producer)}, producer ok={producer_ok}, held tainted="
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
        else:
            self.restart()

    def refused(self, other: bool = False) -> None:
        """A click that nothing made: it must be refused, and the delay starts again."""
        if not self.shown:
            return
        assert not self.subject.allow(other), "UNSAFE: an Allow with no producer was accepted"
        self.restart()

    def next_prompt(self) -> None:
        self.subject.show(False)
        self.restart()  # a new prompt: everything held is of an earlier epoch
        self.shown = True
        self.inflight = None
        self.pick_open = False
        self.drop_pointers()

    # -- inputs -----------------------------------------------------------------------------------

    def record_press(self, which: Phys, on_button: bool) -> None:
        armed = self.armed()
        if self.shown and not armed:
            self.restart()
        self.down[which] = Down(armed, self.epoch, on_button)
        self.subject.began(which, which in ACTIVATING and on_button)

    @rule(which=st.sampled_from(POINTERS), on_button=st.booleans())
    def pointer_down(self, which: Phys, on_button: bool) -> None:
        if which in self.down or which in self.forgotten:
            return  # (a device's button is down once: a second press means its release was lost)
        self.stale.pop(which, None)  # a press of a button whose release was lost replaces it
        self.record_press(which, on_button)
        self.done()

    @rule(on_button=st.booleans())
    def touch_down(self, on_button: bool) -> None:
        """A finger: its sequence is new, never an id used before."""
        which = Phys("pointer", FIRST_FINGER + self.fingers)
        self.fingers += 1
        self.record_press(which, on_button)
        self.done()

    @rule(which=st.sampled_from(KEYS), on_button=st.booleans())
    def key_down(self, which: Phys, on_button: bool) -> None:
        if which in self.forgotten:
            return  # (it is physically down in another window)
        activates = which in ACTIVATING and on_button
        if which in self.down:  # auto-repeat: not a new press, but it restarts a disarmed prompt
            if self.shown and not self.armed():
                self.restart()
            self.subject.began(which, activates)
            if activates:
                self.start_activation(self.down[which])  # the keyboard repeats an activation
            self.done()
            return
        self.record_press(which, on_button)
        if activates:
            self.start_activation(self.down[which])
        self.done()

    def release_input(self, which: Phys) -> None:
        if which in self.down:
            record: Down | None = self.down.pop(which)
        else:
            on_button = self.forgotten.pop(which)
            record = None  # the window never sees it come up with a record
        self.subject.ended(which)
        if which.kind == "pointer":
            on_button = record.on_button if record is not None else on_button
            if on_button:
                self.subject.released_on_button()  # the release is delivered to the button
            if on_button and clicks(which):
                # The button clicks on the release; with no record it is a fail-closed refusal.
                self.check_click(record or UNSEEN, None, record is None)
            elif not on_button:
                self.refused()  # a release elsewhere, and a click on the button in its event
        elif self.inflight is not None:
            # Any key's release completes the activation under way (GTK): the click is the
            # activator's, and fail closed if the key that comes up began tainted.
            activator, self.inflight = self.inflight, None
            self.check_click(activator, record, record is None, producer_ok=self.over_ok())
        self.done()

    def over_ok(self) -> bool:
        """The activation that is completed is not over: the controller's half second, after which
        the producer is no longer taken for the user's (GTK's own timer is 250 ms)."""
        return self.subject.clock.now - self.inflight_at <= ACTIVATION_SECONDS

    @precondition(lambda self: bool(self.down) or bool(self.forgotten))
    @rule(data=st.data())
    def release(self, data: st.DataObject) -> None:
        choices = sorted([*self.down, *self.forgotten])
        self.release_input(data.draw(st.sampled_from(choices)))

    @precondition(lambda self: self.inflight is not None)
    @rule()
    def activation_timer(self) -> None:
        """The 250 ms of a keyboard activation pass with its key still down."""
        assert self.inflight is not None
        activator, self.inflight = self.inflight, None
        self.check_click(activator, None, False, producer_ok=self.over_ok())
        self.done()

    @precondition(lambda self: any(i.kind == "pointer" for i in self.down))
    @rule(data=st.data())
    def release_lost(self, data: st.DataObject) -> None:
        """A drag takes the release: the button is up and the window is not told."""
        which = data.draw(st.sampled_from(sorted(i for i in self.down if i.kind == "pointer")))
        self.stale[which] = self.down.pop(which)

    @precondition(lambda self: any(i.code >= FIRST_FINGER for i in self.down))
    @rule(data=st.data())
    def touch_cancelled(self, data: st.DataObject) -> None:
        touches = sorted(i.code for i in self.down if i.code >= FIRST_FINGER)
        which = Phys("pointer", data.draw(st.sampled_from(touches)))
        del self.down[which]
        self.subject.ended(which, cancelled=True)  # no click
        self.done()

    @rule(which=st.sampled_from([*POINTERS, *KEYS, None]))
    def release_stray(self, which: Phys | None) -> None:
        """A release comes with no press that was seen (it went down in another window; a touch
        that began elsewhere is a sequence not seen before: None). A key's completes an
        activation under way, like any key's, and the click is refused (fail closed)."""
        if which is None:
            which = Phys("pointer", FIRST_FINGER + self.fingers)
            self.fingers += 1
        if which in self.down or which in self.stale or which in self.forgotten:
            return
        self.subject.ended(which)
        if which.kind == "key" and self.inflight is not None:
            activator, self.inflight = self.inflight, None
            self.check_click(activator, None, True)
        self.done()  # (no button began on a pointer: nothing clicks)

    @rule(other=st.booleans())
    def synthetic_click(self, other: bool) -> None:
        """A click with no input behind it (an accessibility `DoAction`) is never taken, armed or
        not, whatever came before, unless it is on the button the user is activating with Enter or
        Space, and within half a second of it (the documented limit: it looks like their own). On
        the other button it is never taken."""
        recent = (
            self.activating_at is not None
            and self.subject.clock.now - self.activating_at <= ACTIVATION_SECONDS
        )
        if not self.shown or (not other and (self.inflight is not None or recent)):
            return
        self.refused(other)
        self.done()

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

    @precondition(lambda self: self.shown and self.pick_open)
    @rule()
    def picker_dialog_dismissed(self) -> None:
        """The dialog answers nothing: a later answer needs a new granted Choose."""
        self.subject.dismiss_picker()
        self.pick_open = False
        self.done()

    @precondition(lambda self: self.shown and not self.pick_open)
    @rule()
    def picker_answer_without_a_choose(self) -> None:
        """An answer with no dialog opened by a granted Choose behind it is never taken."""
        if self.subject.picks():
            assert not self.subject.picked(), "a picker's answer was taken with no Choose"
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
            self.forgotten[which] = False  # physically down; the window will not see it come up
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
        self.drop_pointers()
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
        self.drop_pointers()
        self.done()

    @precondition(lambda self: self.shown)
    @rule(device=st.sampled_from(["enter", "mouse", "pen", "touch", "elsewhere"]))
    def deliberate_input(self, device: str) -> None:
        """(C): the user waits for the prompt, then clicks (a mouse, a pen, a finger) or presses
        Enter on the button. If nothing tainted is held and nothing is stale it must be taken,
        whatever came before; if something tainted is held (a mouse button from the first 750 ms,
        say, that a pen's tap must not stand in for) it must not."""
        if self.subject.armed() != self.armed():
            return
        self.subject.advance(ARM_SECONDS)
        if device == "touch":
            which = Phys("pointer", FIRST_FINGER + self.fingers)
            self.fingers += 1
        else:
            which = {"enter": ENTER, "mouse": LEFT, "pen": PEN, "elsewhere": PEN}[device]
        if which in self.down or which in self.forgotten or self.inflight is not None:
            return
        self.record_press(which, device != "elsewhere")
        if device == "elsewhere":
            # A deliberate click, but on the prompt's text: a click on the button in its event
            # (a synthetic one) has no producer there.
            self.down.pop(which)
            self.subject.ended(which)
            self.refused()
        elif device == "enter":
            self.inflight = None
            self.activating_at = self.subject.clock.now
            self.check_click(self.down[which], None, False)  # the activation, by its timer
            self.subject.ended(which)
            del self.down[which]
        else:
            record = self.down.pop(which)
            self.subject.ended(which)
            self.subject.released_on_button()
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


class Draw:
    """Stands in for hypothesis' `data`: the script says what is drawn."""

    def __init__(self, choice: Phys) -> None:
        self._choice = choice

    def draw(self, _strategy: object) -> Phys:
        return self._choice


class TestScripted:
    """Cases that the random runs reach too rarely at the CI budget."""

    @staticmethod
    def machine() -> ArmingMachine:
        machine = ArmingMachine()
        machine.wait(0.75)  # armed
        return machine

    def test_enter_on_deny_while_a_synthetic_allow_is_in_flight(self) -> None:
        """Round 7: the focus is on Deny (the press is not on the button): Allow's own activation
        timer fires inside the producer window of a key that activates nothing that grants."""
        machine = self.machine()
        machine.key_down(ENTER, on_button=False)
        machine.wait(0.25)
        machine.refused()

    def test_a_voice_key_held_makes_no_grant(self) -> None:
        machine = self.machine()
        for _ in range(5):
            machine.key_down(SPACE, on_button=False)  # auto-repeat of a key the focus does not take
            machine.wait(0.25)
        machine.refused()

    def test_a_synthetic_click_on_the_other_button_during_an_activation_is_refused(self) -> None:
        machine = self.machine()
        machine.key_down(ENTER, on_button=True)
        machine.refused(other=True)

    def test_an_activation_over_makes_no_grant(self) -> None:
        machine = self.machine()
        machine.key_down(ENTER, on_button=True)
        machine.wait(1.0)
        machine.activation_timer()

    def test_a_press_does_not_end_an_activation(self) -> None:
        """GTK does not cancel it, so the user's Allow is taken: (C)."""
        machine = self.machine()
        machine.key_down(ENTER, on_button=True)
        machine.pointer_down(MIDDLE, on_button=False)
        machine.activation_timer()

    def test_a_key_that_activates_nothing_does_not_end_it(self) -> None:
        machine = self.machine()
        machine.key_down(ENTER, on_button=True)
        machine.key_down(LETTER, on_button=False)
        machine.activation_timer()  # must be taken: (C), and GTK does not cancel it

    def test_a_release_with_no_record_completes_no_activation(self) -> None:
        machine = self.machine()
        machine.key_down(ENTER, on_button=True)
        machine.release_stray(SHIFT)

    def test_a_pen_tap_while_a_mouse_press_from_the_first_750_ms_is_held(self) -> None:
        machine = ArmingMachine()
        machine.wait(0.25)
        machine.pointer_down(LEFT, on_button=True)
        machine.wait(0.75)
        machine.deliberate_input("pen")

    def test_the_release_of_a_dropped_pointer_completes_no_click(self) -> None:
        machine = self.machine()
        machine.pointer_down(LEFT, on_button=True)
        machine.prompt_swapped()
        machine.release_input(LEFT)

    def test_a_release_elsewhere_makes_no_grant(self) -> None:
        """A release off the button, and a click in its event: no grant."""
        machine = self.machine()
        machine.pointer_down(LEFT, on_button=False)
        machine.release_input(LEFT)

    def test_a_release_on_the_other_button_makes_no_grant(self) -> None:
        machine = self.machine()
        machine.pointer_down(LEFT, on_button=True)
        machine.subject.ended(LEFT)
        machine.down.pop(LEFT)
        machine.subject.released_on_button()
        machine.refused(other=True)

    def test_an_expired_activation_is_not_completed_by_another_keys_release(self) -> None:
        """T1: the half second is checked when the release comes, not only at the click."""
        machine = self.machine()
        machine.key_down(ENTER, on_button=True)
        machine.key_down(LETTER, on_button=False)
        machine.wait(1.0)
        machine.release_input(LETTER)

    def test_a_pointer_release_is_not_on_the_button_of_an_activation_under_way(self) -> None:
        """T2: only a key's release takes the activation's target."""
        machine = self.machine()
        machine.pointer_down(LEFT, on_button=False)  # clean, held, not on the button
        machine.key_down(ENTER, on_button=True)
        machine.release_input(LEFT)

    def test_a_press_during_an_activation_keeps_it_for_its_own_button_only(self) -> None:
        """T5: positive in `test_a_press_does_not_end_an_activation`; here the negatives."""
        machine = self.machine()
        machine.key_down(ENTER, on_button=True)
        machine.pointer_down(MIDDLE, on_button=False)
        machine.refused(other=True)
        machine.wait(0.75)
        machine.key_down(SPACE, on_button=True)
        machine.pointer_down(RIGHT, on_button=False)
        machine.wait(1.0)
        machine.activation_timer()

    def test_a_dismissed_dialog_takes_no_answer(self) -> None:
        machine = ArmingMachine()
        machine.prompt_goes()
        machine.prompt_comes(picks=True)
        machine.wait(0.75)
        machine.deliberate_input("mouse")
        assert machine.pick_open
        machine.picker_dialog_dismissed()
        machine.picker_answer_without_a_choose()

    def test_a_deliberate_click_after_all_that_is_taken(self) -> None:
        machine = self.machine()
        machine.key_down(ENTER, on_button=False)
        machine.refused()
        machine.wait(0.75)
        machine.deliberate_input("mouse")
