"""Hostile text cannot wreck the layout or freeze the main loop (I5, I9).

Two kinds of text pass every size cap and still broke the window before `display_text`:

- one grapheme cluster tens of thousands of pixels wide (5000 Hangul jamo, a long ZWJ chain, a
  base with thousands of spacing marks): it set the window's minimum width, pushed the chrome off
  the screen and, in the container, crashed Cairo (surface too big);
- 16 KiB of hex, with no break opportunity: measuring the wrapped label was quadratic, seconds for
  16 KiB and minutes for 64 KiB, on the main thread.

Each text is sent as the user's message (the echo then repeats it as the assistant's) and
streamed as an assistant reply, and the window's size, the chrome region and the status strip
must not move. The time budgets are generous multiples of what a correct layout takes; the
quadratic one took 8 to 332 seconds.
"""

import time
from collections.abc import Callable, Iterator

import pytest

from tests.shell.fakes import ManualResponder
from tests.shell.gtk_support import (
    Bounds,
    Shell,
    ancestor_of,
    body_laid_out,
    bounds,
    click_send,
    pump,
    pump_until,
    rows,
    show,
    solid,
    type_text,
)
from tumbleweed.agent.responder import ReplyDelta, ReplyFinished, Responder
from tumbleweed.shell.viewmodel.display import ZERO_WIDTH_SPACE, display_text
from tumbleweed.shell.viewmodel.transcript import MAX_ASSISTANT_BYTES, MAX_USER_BYTES

pytestmark = pytest.mark.gtk

type ShellBuilder = Callable[[Responder], Shell]

JAMO = "\u1100"
ZWJ = "\u200d"
WOMAN = "\U0001f469"

# Each is under the user cap (16 KiB) in UTF-8, so it can be sent as it is.
CLUSTERS = {
    "5000 hangul jamo": JAMO * 5000,
    "a ZWJ chain": WOMAN + (ZWJ + WOMAN) * 1500,
    "devanagari with spacing marks": "\u0915" + "\u0903" * 4000,
    "devanagari conjuncts that never end": "\u0915\u094d" * 2500,
    "combining marks": "e" + "\u0301" * 5000,
    "prepend characters": "\u0600" * 4000,
    "emoji modifiers": "\U0001f44d" + "\U0001f3fd" * 3000,
}
HEX_USER = "ab" * (MAX_USER_BYTES // 2)  # 16 KiB, no break opportunity
HEX_ASSISTANT = "ab" * (MAX_ASSISTANT_BYTES // 2 - 4)  # just under 64 KiB

USER_BUDGET_SECONDS = 4.0
ASSISTANT_BUDGET_SECONDS = 8.0


def frame(shell: Shell) -> tuple[tuple[int, int], Bounds, Bounds, int]:
    """What must not move: the window's size, the chrome, the status strip, the panel's width."""
    window = shell.window
    return (
        (window.get_width(), window.get_height()),
        bounds(window.chrome, window),
        bounds(window.status_strip, window),
        window.chat_panel.get_width(),
    )


def settled(shell: Shell) -> None:
    pump(0.4)
    assert shell.window.get_mapped()


def chunks(text: str, size: int) -> Iterator[str]:
    for start in range(0, len(text), size):
        yield text[start : start + size]


@pytest.fixture
def shown(echo_shell: Shell) -> Shell:
    show(echo_shell)
    settled(echo_shell)
    return echo_shell


class TestHostileClusters:
    @pytest.mark.parametrize("text", CLUSTERS.values(), ids=list(CLUSTERS))
    def test_pasting_it_into_the_input_leaves_the_layout_alone(
        self, shown: Shell, text: str
    ) -> None:
        before = frame(shown)

        type_text(shown, text)
        settled(shown)

        assert frame(shown) == before

    @pytest.mark.parametrize("text", CLUSTERS.values(), ids=list(CLUSTERS))
    def test_sent_as_the_users_message_and_echoed_it_leaves_the_layout_alone(
        self, shown: Shell, text: str
    ) -> None:
        before = frame(shown)
        type_text(shown, text)

        click_send(shown)

        assert pump_until(lambda: len(rows(shown)) == 2)
        settled(shown)
        assert frame(shown) == before
        user, reply = shown.window.transcript.messages
        assert user.text == text  # the stored text is canonical
        assert reply.text == "Echo: " + text
        user_row = shown.window.chat_panel.row_for(user.id)
        assert user_row.shown_text == display_text(text)  # what is shown is the transform
        assert ZERO_WIDTH_SPACE in user_row.shown_text

    @pytest.mark.parametrize("text", CLUSTERS.values(), ids=list(CLUSTERS))
    def test_streamed_as_the_assistants_reply_it_leaves_the_layout_alone(
        self, build_shell: ShellBuilder, text: str
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        show(shell)
        settled(shell)
        before = frame(shell)
        type_text(shell, "go")
        click_send(shell)

        for piece in chunks(text, 500):
            responder.sink.emit(ReplyDelta(text=piece))
        responder.sink.emit(ReplyFinished())

        settled(shell)
        assert frame(shell) == before
        reply = shell.window.transcript.messages[1]
        assert reply.text == text
        row = shell.window.chat_panel.row_for(reply.id)
        assert row.shown_text == display_text(text)

    def test_the_transcript_never_passes_its_content_width_on_to_the_window(
        self, shown: Shell
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        window = shown.window
        before = window.measure(Gtk.Orientation.HORIZONTAL, -1)
        type_text(shown, JAMO * 5000)
        click_send(shown)
        assert pump_until(lambda: len(rows(shown)) == 2)
        settled(shown)

        after = window.measure(Gtk.Orientation.HORIZONTAL, -1)

        assert after[:2] == before[:2]  # minimum and natural width

    def test_without_the_transform_the_same_text_would_widen_the_row(self, shown: Shell) -> None:
        # The control: the cluster is really as wide as the review found, so the checks above
        # prove something. A label given the raw text is far wider than the whole window.
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.shell._widgets import plain_label

        raw = plain_label(JAMO * 3000, wrap=True)
        capped = plain_label(display_text(JAMO * 3000), wrap=True)

        raw_min = raw.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        capped_min = capped.measure(Gtk.Orientation.HORIZONTAL, -1)[0]

        assert raw_min > 10 * shown.window.get_width()
        assert capped_min < shown.window.get_width()


class TestTranscriptWidth:
    """The transcript never passes its content's width on, whatever the content is."""

    def test_a_row_that_asks_for_6000_pixels_does_not_widen_the_window(self, shown: Shell) -> None:
        from tumbleweed.shell._gi import Gtk

        window = shown.window
        before = frame(shown)
        measured = window.measure(Gtk.Orientation.HORIZONTAL, -1)

        window.chat_panel.transcript_list.append(solid((1.0, 0.0, 0.0), size=(6000, 50)))
        settled(shown)

        assert frame(shown) == before
        assert window.measure(Gtk.Orientation.HORIZONTAL, -1)[:2] == measured[:2]

    def test_the_transcript_scrolls_sideways_instead(self, shown: Shell) -> None:
        from tumbleweed.shell._gi import Gtk

        wide = solid((1.0, 0.0, 0.0), size=(6000, 50))
        shown.window.chat_panel.transcript_list.append(wide)
        settled(shown)

        scrolled = ancestor_of(shown.window.chat_panel.transcript_list, Gtk.ScrolledWindow)
        assert scrolled is not None
        assert scrolled.get_hscrollbar().get_mapped()  # the content is reachable, not squeezed
        assert wide.get_width() >= 6000


class TestTimeBudgets:
    def test_16_kib_of_hex_sent_by_the_user_lays_out_within_the_budget(self, shown: Shell) -> None:
        type_text(shown, HEX_USER)
        started = time.monotonic()

        click_send(shown)
        assert pump_until(lambda: len(rows(shown)) == 2)
        row = shown.window.chat_panel.row_for(shown.window.transcript.messages[0].id)
        assert pump_until(lambda: body_laid_out(row), timeout=USER_BUDGET_SECONDS + 10)
        elapsed = time.monotonic() - started

        assert elapsed < USER_BUDGET_SECONDS, f"{elapsed:.1f} s for 16 KiB of hex"

    def test_64_kib_of_hex_streamed_by_the_assistant_lays_out_within_the_budget(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        show(shell)
        settled(shell)
        type_text(shell, "go")
        click_send(shell)
        started = time.monotonic()

        for piece in chunks(HEX_ASSISTANT, 1024):
            responder.sink.emit(ReplyDelta(text=piece))
        responder.sink.emit(ReplyFinished())
        reply = shell.window.transcript.messages[1]
        row = shell.window.chat_panel.row_for(reply.id)
        assert pump_until(lambda: body_laid_out(row), timeout=ASSISTANT_BUDGET_SECONDS + 10)
        elapsed = time.monotonic() - started

        assert len(reply.text.encode()) > 60_000
        assert elapsed < ASSISTANT_BUDGET_SECONDS, f"{elapsed:.1f} s for 64 KiB of hex"

    def test_the_echo_of_16_kib_of_hex_adds_a_second_such_row_within_the_budget(
        self, shown: Shell
    ) -> None:
        type_text(shown, HEX_USER)
        started = time.monotonic()

        click_send(shown)
        assert pump_until(lambda: len(rows(shown)) == 2)
        reply_id = shown.window.transcript.messages[1].id
        row = shown.window.chat_panel.row_for(reply_id)
        assert pump_until(lambda: body_laid_out(row), timeout=USER_BUDGET_SECONDS + 10)
        settled(shown)
        elapsed = time.monotonic() - started

        assert elapsed < 2 * USER_BUDGET_SECONDS, f"{elapsed:.1f} s for two rows of 16 KiB"
