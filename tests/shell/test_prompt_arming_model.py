"""A model-based property test of the prompt's arming invariant (S18, I5), over random sequences.

The invariant (also in `viewmodel.prompts` and D25 (6)):

  (A) An Allow is accepted only if the input that produced it (the pointer press whose release
      clicked it, or the key press that activated it) began while the prompt was armed and after
      the last restart of the delay, and no input that began while it was disarmed (or before a
      restart) is still held.
  (B) A Deny is never refused for how an input began or for what is held: it is ignored only in
      the first 750 ms.
  (C) Nothing refuses a deliberate Allow: an input that begins after arming, with nothing tainted
      held, is accepted, of either kind and after any other input.

The reference model below is written from that statement and shares no code with the controller:
it keeps what is physically down (with when and in which state it went down), and says, at every
Allow and Deny, exactly whether the controller must take it. The test then compares, so that (A)
(accepted implies conditions) and (C) (conditions imply accepted) are both checked, together with
the controller's own idea of when it is armed. The random steps are pointer presses and releases
(button 1, 2, 3 and a touch), key presses and releases (Enter, Space, a letter, Shift, with
auto-repeat), the window being activated and deactivated, the clock, the first frame, a prompt
swap, Deny clicks, and deliberate Allow clicks and key activations.

`SUBJECT` is the seam that lets the same test run against another controller (a scratch copy of an
older one, to see what it finds): it builds an object with the few methods used below.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from tests.shell.prompt_support import Answers, Clock, FakeView, Timers, read_card
from tumbleweed.ipc.prompt_messages import AllowDecision, DenyDecision
from tumbleweed.shell.viewmodel.prompts import ARM_SECONDS, InputId, PromptController

ALLOW = AllowDecision(kind="allow", scope="task", picked=None)
DENY = DenyDecision(kind="deny", reason="user_denied")

LEFT, MIDDLE, RIGHT, TOUCH = (InputId("pointer", n) for n in (1, 2, 3, 1000))
ENTER, SPACE, LETTER, SHIFT = (InputId("key", n) for n in (36, 65, 38, 50))
POINTERS = (LEFT, MIDDLE, RIGHT, TOUCH)
KEYS = (ENTER, SPACE, LETTER, SHIFT)
CLICKS_ON_RELEASE = (LEFT, TOUCH, SPACE)  # what makes a GtkButton click, when it began on it


class Subject(Protocol):
    """What the model drives: a controller with a clock, and one prompt shown at a time."""

    clock: Clock

    def began(self, which: InputId) -> None: ...
    def ended(self, which: InputId) -> None: ...
    def deactivated(self) -> None: ...
    def reactivated(self) -> None: ...
    def drawn(self) -> None: ...
    def advance(self, seconds: float) -> None: ...
    def allow(self) -> bool: ...
    def deny(self) -> bool: ...
    def swap(self) -> None: ...
    def next_prompt(self) -> None: ...
    def armed(self) -> bool: ...


class RealSubject:
    def __init__(self) -> None:
        self.clock = Clock()
        self._timers = Timers(self.clock)
        self.controller = PromptController(FakeView(), Answers(), self._timers, self.clock)
        self.card = read_card()
        self.controller.offer(self.card, 100_000.0)

    def began(self, which: InputId) -> None:
        self.controller.input_began(which)

    def ended(self, which: InputId) -> None:
        self.controller.input_ended(which)

    def deactivated(self) -> None:
        self.controller.deactivated()

    def reactivated(self) -> None:
        self.controller.reactivated()

    def drawn(self) -> None:
        self.controller.mark_drawn(self.card.prompt)

    def advance(self, seconds: float) -> None:
        self._timers.advance(seconds)

    def allow(self) -> bool:
        return self.controller.choose(self.card.prompt, ALLOW)

    def deny(self) -> bool:
        return self.controller.choose(self.card.prompt, DENY)

    def swap(self) -> None:
        self.controller.withdraw(self.card.prompt)
        self.next_prompt()

    def next_prompt(self) -> None:
        if not self.controller.pending:
            self.card = read_card()
            self.controller.offer(self.card, 100_000.0)
        else:
            self.card = self.controller.current or self.card

    def armed(self) -> bool:
        return self.controller.armed()


SUBJECT: Callable[[], Subject] = RealSubject


@dataclass
class Down:
    """What the model knows of an input that is physically down."""

    armed: bool  # the prompt took answers when it went down
    epoch: int  # the model's own count of restarts of the delay when it did
    on_button: bool  # it went down on the button that its click would answer


class ArmingMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.subject = SUBJECT()
        self.epoch = 0
        self.restart_at = self.subject.clock.now
        self.down: dict[InputId, Down] = {}
        self.restart()  # the prompt that is shown

    # -- the model ----------------------------------------------------------------------------

    def restart(self) -> None:
        self.epoch += 1
        self.restart_at = self.subject.clock.now

    def armed(self) -> bool:
        return self.subject.clock.now - self.restart_at >= ARM_SECONDS

    def tainted(self, down: Down) -> bool:
        return not down.armed or down.epoch != self.epoch

    def expect_allow(self, producer: Down | None) -> bool:
        """(A) and (C) in one: accepted exactly when the prompt is armed, the input that made
        this click began armed in this epoch, and nothing tainted is held."""
        return (
            self.armed()
            and (producer is None or not self.tainted(producer))
            and not any(self.tainted(d) for d in self.down.values())
        )

    def check_allow(self, producer: Down | None) -> None:
        expected = self.expect_allow(producer)
        got = self.subject.allow()
        assert got == expected, (
            f"Allow was {'accepted' if got else 'refused'}, the invariant says "
            f"{'accept' if expected else 'refuse'} (armed={self.armed()}, "
            f"producer tainted={None if producer is None else self.tainted(producer)}, "
            f"held={ {k: self.tainted(v) for k, v in self.down.items()} })"
        )
        if got:
            self.next_prompt()

    def next_prompt(self) -> None:
        self.subject.next_prompt()
        self.restart()  # a new prompt: everything held is of an earlier epoch

    # -- the user, and the machine ----------------------------------------------------------------

    @rule(which=st.sampled_from(POINTERS), on_button=st.booleans())
    def pointer_down(self, which: InputId, on_button: bool) -> None:
        if which in self.down:
            return  # the hardware never presses a held button twice
        armed = self.armed()
        if not armed:
            self.restart()
        self.down[which] = Down(armed, self.epoch, on_button)
        self.subject.began(which)

    @rule(which=st.sampled_from(KEYS), on_button=st.booleans())
    def key_down(self, which: InputId, on_button: bool) -> None:
        if which in self.down:  # auto-repeat: not a new press, but it restarts a disarmed prompt
            if not self.armed():
                self.restart()
            self.subject.began(which)
            return
        armed = self.armed()
        if not armed:
            self.restart()
        self.down[which] = Down(armed, self.epoch, on_button)
        self.subject.began(which)

    @precondition(lambda self: bool(self.down))
    @rule(data=st.data())
    def release(self, data: st.DataObject) -> None:
        which = data.draw(st.sampled_from(sorted(self.down, key=lambda i: (i.kind, i.code))))
        down = self.down.pop(which)
        self.subject.ended(which)
        if down.on_button and which in CLICKS_ON_RELEASE:
            self.check_allow(down)  # the button clicks as it is released

    @precondition(lambda self: ENTER in self.down and self.down[ENTER].on_button)
    @rule()
    def enter_activates(self) -> None:
        self.check_allow(self.down[ENTER])  # a held Enter activates the focused button

    @rule()
    def deny_clicked(self) -> None:
        expected = self.armed()
        got = self.subject.deny()
        assert got == expected, f"Deny was {'taken' if got else 'ignored'}, expected {expected}"
        if got:
            self.next_prompt()

    @rule(seconds=st.sampled_from([0.125, 0.25, 0.5, 0.75, 1.0]))
    def wait(self, seconds: float) -> None:
        self.subject.advance(seconds)

    @rule()
    def window_activated(self) -> None:
        self.subject.reactivated()
        self.restart()

    @rule()
    def window_deactivated(self) -> None:
        self.subject.deactivated()
        self.down.clear()  # the focus is gone: what was held is cancelled

    @rule()
    def first_frame(self) -> None:
        self.subject.drawn()
        self.restart()

    @rule()
    def prompt_swapped(self) -> None:
        self.subject.swap()
        self.restart()

    @rule(use_key=st.booleans())
    def deliberate_input(self, use_key: bool) -> None:
        """(C): the user waits for the prompt, then clicks (or presses Enter) on the button. If
        nothing tainted is held that must be taken, whatever came before."""
        if any(self.tainted(d) for d in self.down.values()):
            return
        self.subject.advance(ARM_SECONDS)
        if use_key:
            which = ENTER if ENTER not in self.down else SPACE
        else:
            which = LEFT if LEFT not in self.down else TOUCH
        if which in self.down:
            return
        if use_key:
            self.key_down_for(which)
            self.check_allow(self.down[which])
            self.release_of(which)
        else:
            self.pointer_down_for(which)
            self.release_of(which)  # the click on release is checked there

    def pointer_down_for(self, which: InputId) -> None:
        armed = self.armed()
        assert armed, "a deliberate input is made on an armed prompt"
        self.down[which] = Down(armed, self.epoch, True)
        self.subject.began(which)

    def key_down_for(self, which: InputId) -> None:
        armed = self.armed()
        assert armed
        self.down[which] = Down(armed, self.epoch, True)
        self.subject.began(which)

    def release_of(self, which: InputId) -> None:
        down = self.down.pop(which)
        self.subject.ended(which)
        if down.on_button and which in CLICKS_ON_RELEASE:
            self.check_allow(down)

    @invariant()
    def the_controller_knows_when_it_is_armed(self) -> None:
        assert self.subject.armed() == self.armed()


TestArmingModel = ArmingMachine.TestCase
TestArmingModel.settings = settings(
    max_examples=1500,
    stateful_step_count=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
