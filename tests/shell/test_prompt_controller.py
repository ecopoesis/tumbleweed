"""The prompt's state machine, without a window (S18, I5, I10): one at a time, ignoring answers
for 750 ms, a deadline that is a denial, and what is shown made from the structure."""

from collections.abc import Callable

import pytest

from tests.shell.prompt_support import (
    Answers,
    Clock,
    FakeView,
    Timers,
    network_card,
    pick_card,
    read_card,
    show_message,
)
from tumbleweed.core.ids import PromptId
from tumbleweed.ipc.broker_messages import NetworkAccess, PickAccess, ReadAccess, WriteAccess
from tumbleweed.ipc.prompt_messages import AllowDecision, DenyDecision
from tumbleweed.shell.viewmodel.prompts import (
    ARM_SECONDS,
    MAX_QUEUED,
    MAX_REASON_SHOWN,
    InputId,
    PromptCard,
    PromptController,
    card_of,
    plain_reason,
)

ALLOW_TASK = AllowDecision(kind="allow", scope="task", picked=None)
DENY = DenyDecision(kind="deny", reason="user_denied")


class Rig:
    def __init__(self) -> None:
        self.clock = Clock()
        self.timers = Timers(self.clock)
        self.view = FakeView()
        self.answers = Answers()
        self.controller = PromptController(self.view, self.answers, self.timers, self.clock)

    def offer(self, card: PromptCard | None = None, expires_in: float = 60.0) -> PromptCard:
        shown = read_card() if card is None else card
        assert self.controller.offer(shown, expires_in)
        return shown

    def arm(self) -> None:
        self.timers.advance(ARM_SECONDS)


@pytest.fixture
def rig() -> Rig:
    return Rig()


class TestOneAtATime:
    def test_the_first_prompt_is_shown_and_the_next_waits_behind_it(self, rig: Rig) -> None:
        first, second = rig.offer(), rig.offer()

        assert rig.controller.current == first
        assert rig.view.shown == [first]
        assert rig.controller.queued == 1

        rig.arm()
        assert rig.controller.choose(first.prompt, DENY)

        assert rig.view.shown == [first, second]  # shown only now
        assert rig.controller.current == second
        assert rig.controller.queued == 0

    def test_the_view_is_cleared_when_the_last_prompt_is_gone(self, rig: Rig) -> None:
        only = rig.offer()
        rig.arm()
        assert rig.view.cleared == 0

        rig.controller.choose(only.prompt, DENY)

        assert rig.view.cleared == 1
        assert not rig.controller.pending

    def test_the_queue_is_bounded_and_a_ninth_prompt_is_refused(self, rig: Rig) -> None:
        for _ in range(MAX_QUEUED):
            rig.offer()

        assert not rig.controller.offer(read_card(), 60)
        assert rig.controller.queued == MAX_QUEUED - 1

    def test_a_prompt_id_already_here_is_refused(self, rig: Rig) -> None:
        card = rig.offer()

        assert not rig.controller.offer(card, 60)

    def test_an_answer_for_a_prompt_that_waits_is_not_taken(self, rig: Rig) -> None:
        first, waiting = rig.offer(), rig.offer()
        rig.arm()

        assert not rig.controller.choose(waiting.prompt, DENY)
        assert rig.answers.given == []
        assert rig.controller.current == first


class TestNotAtOnce:
    @pytest.mark.parametrize("decision", [DENY, ALLOW_TASK])
    def test_an_answer_in_the_first_750_ms_is_ignored_and_the_prompt_stays(
        self, rig: Rig, decision: AllowDecision | DenyDecision
    ) -> None:
        card = rig.offer()

        rig.clock.now += ARM_SECONDS - 0.01
        taken = rig.controller.choose(card.prompt, decision)

        assert not taken
        assert rig.answers.given == []
        assert rig.controller.current == card
        assert not rig.controller.armed()

    def test_it_takes_answers_from_750_ms_on(self, rig: Rig) -> None:
        card = rig.offer()
        rig.clock.now += ARM_SECONDS

        assert rig.controller.choose(card.prompt, ALLOW_TASK)

        assert rig.answers.given == [(card.prompt, ALLOW_TASK)]

    def test_the_clock_decides_not_the_timer(self, rig: Rig) -> None:
        """A timer that is late must not arm a prompt early, nor one that is lost keep it shut."""
        card = rig.offer()
        rig.clock.now += 0.5  # the timer has not run, the clock has not reached 750 ms

        assert not rig.controller.choose(card.prompt, DENY)

        rig.clock.now += 0.5  # the clock has passed 750 ms though the timer never fired
        assert rig.controller.choose(card.prompt, DENY)

    def test_each_prompt_has_its_own_750_ms(self, rig: Rig) -> None:
        first, second = rig.offer(), rig.offer()
        rig.arm()
        rig.controller.choose(first.prompt, DENY)

        # The user's second click lands on the prompt that has just replaced the first.
        assert not rig.controller.choose(second.prompt, DENY)
        assert rig.controller.current == second

        rig.arm()
        assert rig.controller.choose(second.prompt, DENY)

    def test_a_choice_made_after_its_prompt_was_taken_down_is_no_answer(self, rig: Rig) -> None:
        """The dialog of a picker may outlive its prompt by a moment: its late choice is nothing."""
        card = rig.offer(pick_card())
        rig.arm()

        rig.controller.withdraw(card.prompt)
        late = AllowDecision(kind="allow", scope="task", picked="/home/u/Documents")

        assert not rig.controller.choose(card.prompt, late)
        assert rig.answers.given == []

    def test_the_view_is_told_when_the_prompt_is_armed(self, rig: Rig) -> None:
        rig.offer()
        assert rig.view.armed == []

        rig.arm()

        assert rig.view.armed == [True]


LEFT = InputId("pointer", 1)
RIGHT = InputId("pointer", 3)
TOUCH = InputId("pointer", 1000)
ENTER = InputId("key", 36)
SPACE = InputId("key", 65)
LETTER = InputId("key", 38)


def click(rig: Rig, card: PromptCard, decision: AllowDecision | DenyDecision = DENY) -> bool:
    """A whole click as the window sees it: the press, the release, and the button's click."""
    rig.controller.input_began(LEFT)
    rig.controller.input_ended(LEFT)
    return rig.controller.choose(card.prompt, decision)


def tap(rig: Rig, key: InputId) -> None:
    rig.controller.input_began(key)
    rig.controller.input_ended(key)


class TestTheArmingInvariant:
    """(A) an Allow is accepted only if the input that produced it began while the prompt was armed
    and after the last restart of the delay, and no input that began disarmed is still held;
    (B) a Deny is never refused for how an input began or what is held; (C) nothing refuses a
    deliberate Allow for good. `test_prompt_arming_model` checks all three over random sequences;
    these are the cases that reviewers found, one by one."""

    # -- a stream, a hold -------------------------------------------------------------------

    def test_a_stream_of_clicks_is_never_taken_until_there_has_been_a_quiet_gap(
        self, rig: Rig
    ) -> None:
        """A model's tool can put a button where "Allow" will appear and click it, over and over."""
        card = rig.offer()

        for _ in range(12):  # 2.4 s of clicks, 200 ms apart
            rig.timers.advance(0.2)
            assert not click(rig, card, ALLOW_TASK)

        assert rig.answers.given == []
        rig.timers.advance(ARM_SECONDS - 0.01)
        assert not click(rig, card, ALLOW_TASK)  # not yet a whole delay of quiet
        rig.timers.advance(ARM_SECONDS)
        assert click(rig, card, ALLOW_TASK)

    def test_a_press_that_began_before_the_prompt_took_answers_is_no_click_however_long_it_is_held(
        self, rig: Rig
    ) -> None:
        """Press and hold ("hold to confirm", a model's tool): the click comes on the release."""
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(LEFT)  # lands on a button that has just appeared

        rig.timers.advance(0.8)  # held until the prompt is armed
        assert rig.controller.armed()
        rig.controller.input_ended(LEFT)
        assert not rig.controller.choose(card.prompt, ALLOW_TASK)  # ... and released: no click

        assert click(rig, card, ALLOW_TASK)  # the user's own, on a prompt that takes answers

    @pytest.mark.parametrize("other", [LETTER, InputId("key", 50), SPACE, ENTER])
    def test_a_key_during_the_hold_does_not_end_it(self, rig: Rig, other: InputId) -> None:
        """Whatever key is pressed once armed ("hold and type the word"), Enter and Space included,
        a press held from before must not make its click count."""
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(LEFT)
        rig.timers.advance(0.8)

        tap(rig, other)
        rig.controller.input_ended(LEFT)

        assert not rig.controller.choose(card.prompt, ALLOW_TASK)
        assert rig.answers.given == []

    def test_enter_on_the_focused_deny_during_a_held_press_is_a_deny_and_not_the_held_allow(
        self, rig: Rig
    ) -> None:
        """Round 4: the Deny the user meant was refused and its refusal freed the held press, which
        then answered Allow. Now the Deny is taken, and the prompt is gone."""
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(LEFT)  # held on Allow, from before the prompt was armed
        rig.timers.advance(0.8)

        rig.controller.input_began(ENTER)  # the user presses Enter: the focus is on Deny
        assert rig.controller.choose(card.prompt, DENY)
        rig.controller.input_ended(ENTER)
        rig.controller.input_ended(LEFT)

        assert [d for _, d in rig.answers.given] == [DENY]
        assert not rig.controller.pending  # nothing is left for the held press to click

    def test_a_second_button_during_the_hold_does_not_end_it(self, rig: Rig) -> None:
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(LEFT)
        rig.timers.advance(0.8)

        rig.controller.input_began(RIGHT)  # another button, with the first still down
        rig.controller.input_ended(RIGHT)
        rig.controller.input_ended(LEFT)

        assert not rig.controller.choose(card.prompt, ALLOW_TASK)

    def test_a_second_press_of_the_same_button_does_not_free_a_held_one(self, rig: Rig) -> None:
        """Lost releases and a second touch look the same: what was recorded as tainted stays."""
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(LEFT)
        rig.timers.advance(0.8)

        rig.controller.input_began(LEFT)
        rig.controller.input_ended(LEFT)

        assert not rig.controller.choose(card.prompt, ALLOW_TASK)

    def test_a_pointer_press_during_a_held_key_does_not_end_it_either(self, rig: Rig) -> None:
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(ENTER)  # pressed while the prompt arrived, still down
        rig.timers.advance(0.8)

        rig.controller.input_began(LEFT)  # armed, a pointer of its own
        rig.controller.input_ended(LEFT)

        assert not rig.controller.choose(card.prompt, ALLOW_TASK)  # the key is still held
        rig.controller.input_ended(ENTER)
        assert click(rig, card, ALLOW_TASK)  # and once it is up, the next click is free

    def test_key_auto_repeat_is_not_a_new_press(self, rig: Rig) -> None:
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(ENTER)  # held from before it took answers
        rig.timers.advance(0.8)

        for _ in range(5):
            rig.controller.input_began(ENTER)  # the keyboard repeats it

        assert not rig.controller.choose(card.prompt, ALLOW_TASK)  # still the same, tainted press

    def test_a_held_press_does_not_open_the_picker_either(self, rig: Rig) -> None:
        card = rig.offer(pick_card())
        rig.timers.advance(0.1)
        rig.controller.input_began(LEFT)
        rig.timers.advance(0.8)
        rig.controller.input_ended(LEFT)

        assert not rig.controller.can_act(card.prompt)
        rig.controller.input_began(LEFT)
        rig.controller.input_ended(LEFT)
        assert rig.controller.can_act(card.prompt)

    # -- nothing is refused for good ---------------------------------------------------------

    def test_a_press_that_ended_in_no_click_does_not_cost_the_next_deliberate_click(
        self, rig: Rig
    ) -> None:
        card = rig.offer()
        rig.timers.advance(0.1)
        tap(rig, LEFT)  # pressed on something else while disarmed, released: no click
        rig.arm()

        assert click(rig, card, ALLOW_TASK)  # the next press is the user's own

    @pytest.mark.parametrize("first", [LETTER, ENTER, SPACE])
    def test_typing_as_the_prompt_appears_does_not_cost_the_first_click(
        self, rig: Rig, first: InputId
    ) -> None:
        """Sonnet, round 4: the chat's typing is the normal case; it must not eat a click."""
        card = rig.offer()
        rig.timers.advance(0.1)
        tap(rig, first)
        rig.arm()

        assert click(rig, card, ALLOW_TASK)

    @pytest.mark.parametrize("key", [ENTER, SPACE])
    def test_a_click_as_the_prompt_appears_does_not_cost_the_first_key(
        self, rig: Rig, key: InputId
    ) -> None:
        card = rig.offer()
        rig.timers.advance(0.1)
        tap(rig, LEFT)  # clicked in the window as the prompt came up
        rig.arm()

        tap(rig, key)  # Enter or Space on the focused button, deliberately
        assert rig.controller.choose(card.prompt, ALLOW_TASK)

    @pytest.mark.parametrize("key", [ENTER, SPACE])
    def test_a_window_activation_does_not_cost_the_first_key_or_click(
        self, rig: Rig, key: InputId
    ) -> None:
        """Sonnet, round 4: reactivate, wait, then Enter (or a click) must be taken."""
        card = rig.offer()
        rig.arm()
        rig.controller.reactivated()  # Alt+Tab back
        rig.arm()

        tap(rig, key)
        assert rig.controller.choose(card.prompt, ALLOW_TASK)

    def test_a_click_after_a_window_activation_and_a_wait_is_taken(self, rig: Rig) -> None:
        card = rig.offer()
        rig.arm()
        rig.controller.reactivated()
        rig.arm()

        assert click(rig, card, ALLOW_TASK)

    def test_losing_the_focus_cancels_what_was_held_so_a_lost_release_cannot_wedge_it(
        self, rig: Rig
    ) -> None:
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(LEFT)  # held, and its release goes to another window
        rig.controller.deactivated()
        rig.controller.reactivated()
        rig.arm()

        assert click(rig, card, ALLOW_TASK)

    def test_a_press_that_is_cancelled_makes_no_click_and_is_forgotten(self, rig: Rig) -> None:
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(TOUCH)
        rig.controller.input_ended(TOUCH, cancelled=True)  # a touch cancelled
        rig.arm()

        assert click(rig, card, ALLOW_TASK)

    def test_a_cancelled_press_leaves_no_click_to_refuse(self, rig: Rig) -> None:
        """It made no click, so an activation that comes without a new input of its own (a held
        Enter's) is not refused for it."""
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(TOUCH)
        rig.controller.input_ended(TOUCH, cancelled=True)
        rig.arm()

        assert rig.controller.choose(card.prompt, ALLOW_TASK)

    def test_a_broken_grab_cancels_every_pointer_press(self, rig: Rig) -> None:
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(LEFT)
        rig.controller.input_began(RIGHT)
        rig.controller.pointers_cancelled()
        rig.arm()

        assert click(rig, card, ALLOW_TASK)

    # -- activation and the first frame --------------------------------------------------------

    def test_a_press_before_the_window_activation_that_is_held_ends_in_no_click(
        self, rig: Rig
    ) -> None:
        """The click that brings the window up may be delivered before the activation is: armed
        when it comes, tainted a moment later, and held past the delay."""
        card = rig.offer()
        rig.arm()
        rig.controller.input_began(LEFT)  # (armed: it looks like the user's own)
        rig.controller.reactivated()  # ... but this was the click that brought the window up
        rig.timers.advance(0.8)
        rig.controller.input_ended(LEFT)

        assert not rig.controller.choose(card.prompt, ALLOW_TASK)

    def test_a_second_button_after_a_reactivation_does_not_free_a_press_held_across_it(
        self, rig: Rig
    ) -> None:
        card = rig.offer()
        rig.controller.input_began(LEFT)  # held on Allow, from before
        rig.controller.reactivated()
        rig.arm()

        rig.controller.input_began(RIGHT)
        rig.controller.input_ended(RIGHT)
        rig.controller.input_ended(LEFT)

        assert not rig.controller.choose(card.prompt, ALLOW_TASK)

    def test_a_held_press_across_a_prompt_swap_does_not_click_the_next_prompt(
        self, rig: Rig
    ) -> None:
        first = rig.offer()
        second = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(LEFT)
        rig.timers.advance(0.8)
        rig.controller.withdraw(first.prompt)  # the next prompt takes its place
        rig.arm()
        rig.controller.input_ended(LEFT)

        assert not rig.controller.choose(second.prompt, ALLOW_TASK)

    def test_a_deny_is_never_refused_for_what_is_held_once_the_prompt_is_armed(
        self, rig: Rig
    ) -> None:
        card = rig.offer()
        rig.timers.advance(0.1)
        rig.controller.input_began(LEFT)
        rig.controller.input_began(ENTER)
        rig.timers.advance(0.8)

        assert rig.controller.choose(card.prompt, DENY)

    def test_a_deny_is_ignored_in_the_first_750_ms_all_the_same(self, rig: Rig) -> None:
        card = rig.offer()
        rig.timers.advance(0.1)

        assert click(rig, card, DENY) is False

    def test_the_window_coming_to_the_front_starts_the_delay_again(self, rig: Rig) -> None:
        card = rig.offer()
        rig.arm()
        assert rig.controller.armed()

        rig.controller.reactivated()  # a click that brought the window up may land on a button

        assert not rig.controller.armed()
        assert rig.view.armed[-1:] == [False]
        assert not rig.controller.choose(card.prompt, ALLOW_TASK)
        rig.arm()
        assert rig.view.armed[-1:] == [True]
        assert click(rig, card, ALLOW_TASK)

    def test_an_input_that_began_with_no_prompt_is_tainted_for_the_next_one(self, rig: Rig) -> None:
        """A key held while typing as the prompt arrives (it did not exist when it went down)."""
        rig.controller.input_began(SPACE)  # no prompt yet
        card = rig.offer()
        rig.arm()

        assert not click(rig, card, ALLOW_TASK)  # the space is still down
        rig.controller.input_ended(SPACE)
        assert click(rig, card, ALLOW_TASK)

    def test_the_delay_counts_from_the_first_frame_not_from_the_queueing(self, rig: Rig) -> None:
        card = rig.offer()
        rig.timers.advance(ARM_SECONDS - 0.05)  # painting was slow

        rig.controller.mark_drawn(card.prompt)
        rig.timers.advance(0.1)

        assert not rig.controller.armed()
        assert not rig.controller.choose(card.prompt, DENY)
        rig.timers.advance(ARM_SECONDS)
        assert rig.controller.choose(card.prompt, DENY)

    def test_a_frame_of_another_prompt_changes_nothing(self, rig: Rig) -> None:
        card = rig.offer()
        rig.arm()

        rig.controller.mark_drawn(PromptId.new())

        assert rig.controller.choose(card.prompt, DENY)

    def test_a_timer_that_runs_before_the_clock_has_got_there_looks_again(self) -> None:
        """The scheduler's clock and the controller's are not the same clock: if the timer runs
        early the buttons must not stay dimmed for good."""
        clock = Clock()
        pending: list[tuple[float, Callable[[], None]]] = []

        class Lagging:
            def call_later(
                self, seconds: float, callback: Callable[[], None]
            ) -> Callable[[], None]:
                pending.append((seconds, callback))
                return lambda: None

        view = FakeView()
        controller = PromptController(view, Answers(), Lagging(), clock)
        controller.offer(read_card(), 60.0)
        clock.now += 0.5  # the controller's clock is behind the timer's
        _seconds, arm = next((s, c) for s, c in pending if s == ARM_SECONDS)

        arm()  # fires early

        assert view.armed == []  # not armed, and not claimed to be
        again = pending[-1]
        assert 0 < again[0] <= ARM_SECONDS - 0.5 + 0.01  # it looks again for what is left
        clock.now += again[0]
        again[1]()
        assert view.armed == [True]


def test_the_categories_a_path_spells_out_include_those_a_workspace_path_refuses() -> None:
    """One set of characters is unsafe to show: if `core.types` learns another, so must this."""
    from tumbleweed.core.types import (
        _FORBIDDEN_CATEGORIES,
    )
    from tumbleweed.shell.viewmodel.prompts import _SPELLED_OUT

    assert _FORBIDDEN_CATEGORIES <= _SPELLED_OUT


class TestOnlyOneAnswerCounts:
    def test_a_prompt_cannot_be_answered_twice(self, rig: Rig) -> None:
        card = rig.offer()
        rig.arm()
        assert rig.controller.choose(card.prompt, ALLOW_TASK)

        assert not rig.controller.choose(card.prompt, ALLOW_TASK)
        assert len(rig.answers.given) == 1

    def test_an_id_that_is_not_the_prompts_is_ignored(self, rig: Rig) -> None:
        rig.offer()
        rig.arm()

        assert not rig.controller.choose(PromptId.new(), ALLOW_TASK)
        assert rig.answers.given == []

    def test_a_scope_the_prompt_did_not_offer_is_ignored(self, rig: Rig) -> None:
        card = rig.offer(read_card(scopes=("once",)))
        rig.arm()

        assert not rig.controller.choose(card.prompt, ALLOW_TASK)
        assert rig.answers.given == []

    def test_a_choice_answers_a_picker_and_nothing_else(self, rig: Rig) -> None:
        reading = rig.offer()
        rig.arm()
        chosen = AllowDecision(kind="allow", scope="task", picked="/home/u/Documents")
        assert not rig.controller.choose(reading.prompt, chosen)  # no path for a named one
        assert rig.controller.choose(reading.prompt, DENY)
        picker = rig.offer(pick_card())
        rig.arm()

        assert not rig.controller.choose(picker.prompt, ALLOW_TASK)  # a picker wants a choice
        assert rig.controller.choose(picker.prompt, chosen)

    def test_a_sink_that_fails_still_brings_the_prompt_down(self, rig: Rig) -> None:
        card, next_card = rig.offer(), rig.offer()
        rig.arm()
        rig.answers.explode = True

        assert rig.controller.choose(card.prompt, DENY)

        assert rig.controller.current == next_card


class TestNoAnswerIsADenial:
    def test_the_deadline_denies_and_takes_the_prompt_down(self, rig: Rig) -> None:
        card = rig.offer(expires_in=30)

        rig.timers.advance(29)
        assert rig.controller.current == card
        rig.timers.advance(2)

        assert rig.answers.given == [
            (card.prompt, DenyDecision(kind="deny", reason="prompt_timeout"))
        ]
        assert not rig.controller.pending
        assert rig.view.cleared == 1

    def test_a_prompt_that_expires_while_waiting_is_denied_without_ever_being_shown(
        self, rig: Rig
    ) -> None:
        first = rig.offer(expires_in=100)
        waiting = rig.offer(expires_in=10)

        rig.timers.advance(11)

        assert rig.answers.given == [
            (waiting.prompt, DenyDecision(kind="deny", reason="prompt_timeout"))
        ]
        assert rig.view.shown == [first]
        assert rig.controller.queued == 0

    def test_the_next_prompt_is_shown_when_one_times_out(self, rig: Rig) -> None:
        first = rig.offer(expires_in=10)
        second = rig.offer(expires_in=100)

        rig.timers.advance(10)

        assert rig.view.shown == [first, second]
        assert rig.controller.current == second

    def test_an_answered_prompt_is_not_denied_later_by_its_deadline(self, rig: Rig) -> None:
        card = rig.offer(expires_in=5)
        rig.arm()
        rig.controller.choose(card.prompt, ALLOW_TASK)

        rig.timers.advance(60)

        assert len(rig.answers.given) == 1
        assert rig.timers.waiting == 0


class TestTheBrokerTakesAPromptDown:
    def test_a_withdrawn_prompt_comes_down_without_an_answer(self, rig: Rig) -> None:
        card, after = rig.offer(), rig.offer()

        rig.controller.withdraw(card.prompt)

        assert rig.answers.given == []
        assert rig.controller.current == after
        assert rig.timers.waiting == 2  # only `after`'s deadline and arming are left

    def test_a_waiting_prompt_can_be_withdrawn(self, rig: Rig) -> None:
        first, waiting = rig.offer(), rig.offer()

        rig.controller.withdraw(waiting.prompt)

        assert rig.controller.queued == 0
        assert rig.controller.current == first
        rig.timers.advance(3600)
        assert [p for p, _ in rig.answers.given] == [first.prompt]  # only the first timed out

    def test_withdrawing_what_is_not_there_does_nothing(self, rig: Rig) -> None:
        rig.offer()

        rig.controller.withdraw(PromptId.new())

        assert rig.controller.pending

    def test_closing_takes_everything_down_and_answers_nothing(self, rig: Rig) -> None:
        rig.offer()
        rig.offer()

        rig.controller.close()

        assert rig.answers.given == []
        assert not rig.controller.pending
        assert rig.timers.waiting == 0
        assert rig.view.cleared == 1


class TestWhatIsShown:
    def test_the_capability_is_read_from_the_structure(self) -> None:
        reading = card_of(show_message(access=ReadAccess(kind="fs.read", path="/home/u/Photos")))
        writing = card_of(show_message(access=WriteAccess(kind="fs.write", path="/home/u/x")))

        assert (reading.kind, reading.path) == ("read", "/home/u/Photos")
        assert (writing.kind, writing.path) == ("write", "/home/u/x")
        assert (network_card().kind, network_card().path) == ("network", None)
        assert (pick_card("file").kind, pick_card("folder").kind) == ("pick_file", "pick_folder")
        assert pick_card().picks
        assert not reading.picks

    def test_a_path_is_shown_whole_with_what_cannot_be_shown_spelled_out(self) -> None:
        card = card_of(show_message(access=ReadAccess(kind="fs.read", path="/home/u/a‮b\x1b[2Jc​d")))

        assert card.path == "/home/u/a\\u{202E}b\\u{001B}[2Jc\\u{200B}d"

    @pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
    def test_a_line_or_paragraph_separator_in_a_path_is_spelled_out_not_a_line_break(
        self, separator: str
    ) -> None:
        card = card_of(
            show_message(access=ReadAccess(kind="fs.read", path=f"/home/u/Photos{separator}x"))
        )

        assert card.path == f"/home/u/Photos\\u{{{ord(separator):04X}}}x"

    def test_the_reason_is_one_plain_line(self) -> None:
        separators = chr(0x2028) + chr(0x200B) + chr(0x202E) + "\x00\x1b"
        text = f"line one\nline two\r\n{separators} reversed [31m red {separators} zero"

        shown, cut = plain_reason(text)

        assert shown == "line one line two reversed [31m red zero"
        assert not cut

    def test_the_reason_is_capped_and_says_so(self) -> None:
        card = card_of(show_message(reason="word " * 90))

        assert len(card.reason) <= MAX_REASON_SHOWN
        assert card.reason.endswith("…")
        assert card.reason_cut

    def test_a_short_reason_is_as_it_was_said(self) -> None:
        card = card_of(show_message(reason="I want to look at the photos"))

        assert card.reason == "I want to look at the photos"
        assert not card.reason_cut

    def test_the_reason_cannot_pose_as_the_prompts_own_words(self) -> None:
        card = card_of(show_message(reason="x\n\nPermission request\nAllow everything?"))

        assert "\n" not in card.reason
        assert card.reason == "x Permission request Allow everything?"  # one quoted line, labelled

    def test_every_access_of_the_wire_has_a_card(self) -> None:
        for access in (
            ReadAccess(kind="fs.read", path="/a"),
            WriteAccess(kind="fs.write", path="/a"),
            NetworkAccess(kind="net.full"),
            PickAccess(kind="fs.pick", choose="file"),
        ):
            assert card_of(show_message(access=access)).kind
