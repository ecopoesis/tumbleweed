"""`SpeechFeed` (S07): the transcript's streaming reply becomes speech, and nothing else does.

GTK-free: a real `Transcript` and a real `SpeechOutput` over the voice fakes, with the test's own
dispatcher. The window-level behaviour (Esc, push-to-talk, the mute button) is in
`test_speech_in_shell.py`.
"""

import threading
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from tests.voice.support import ManualDispatcher
from tumbleweed.shell.speech_feed import SpeechFeed
from tumbleweed.shell.viewmodel.transcript import Transcript
from tumbleweed.voice.config import TtsConfig
from tumbleweed.voice.fake import FakeAudioPlayer, FakeTts
from tumbleweed.voice.output import SpeechOutput, SpeechPhase

FIRST = "The first sentence is here."
SECOND = "The second sentence follows it."


@dataclass
class Rig:
    transcript: Transcript
    speech: SpeechOutput
    tts: FakeTts
    player: FakeAudioPlayer
    dispatcher: ManualDispatcher

    def settle(self) -> None:
        self.dispatcher.settle(0.25)


@pytest.fixture
def rig() -> Iterator[Rig]:
    dispatcher = ManualDispatcher()
    tts, player = FakeTts(), FakeAudioPlayer()
    speech = SpeechOutput(
        backend=tts, player=lambda: player, dispatcher=dispatcher, config=TtsConfig()
    )
    transcript = Transcript()
    transcript.subscribe(SpeechFeed(speech))
    yield Rig(transcript, speech, tts, player, dispatcher)
    speech.close()


def test_a_reply_is_spoken_as_its_text_arrives(rig: Rig) -> None:
    message = rig.transcript.begin_assistant()

    rig.transcript.append(message.id, "The first sent")
    rig.transcript.append(message.id, "ence is here. The second")
    assert rig.dispatcher.pump_until(lambda: rig.tts.spoken == [FIRST])
    rig.transcript.append(message.id, " sentence follows it.")
    rig.transcript.finish(message.id)

    assert rig.dispatcher.pump_until(lambda: rig.tts.spoken == [FIRST, SECOND])
    assert rig.dispatcher.pump_until(lambda: rig.speech.phase is SpeechPhase.IDLE)


def test_only_the_new_text_of_each_change_is_fed_never_the_whole_message_again(rig: Rig) -> None:
    message = rig.transcript.begin_assistant()
    for piece in ("Hello there, ", "this is a reply. ", "And another one. "):
        rig.transcript.append(message.id, piece)
    rig.transcript.finish(message.id)

    assert rig.dispatcher.pump_until(lambda: len(rig.tts.spoken) == 2)

    assert rig.tts.spoken == ["Hello there, this is a reply.", "And another one."]


def test_a_failed_reply_is_not_read_out(rig: Rig) -> None:
    message = rig.transcript.begin_assistant()
    rig.transcript.append(message.id, f"{FIRST} {SECOND[:5]}")
    assert rig.dispatcher.pump_until(lambda: rig.tts.spoken == [FIRST])

    rig.transcript.fail(message.id, "Cancelled.")
    rig.settle()

    assert rig.tts.spoken == [FIRST]  # not the half sentence, which `finish` would have spoken
    assert rig.speech.phase is SpeechPhase.IDLE


def test_a_user_message_stops_the_speech_of_the_reply_before(rig: Rig) -> None:
    message = rig.transcript.begin_assistant()
    rig.transcript.append(message.id, f"{FIRST} ")
    rig.transcript.finish(message.id)

    def phase() -> SpeechPhase:  # a call, so the checker does not take one answer for good
        return rig.speech.phase

    assert phase() is SpeechPhase.SPEAKING

    rig.transcript.add_user("another question")

    assert phase() is SpeechPhase.IDLE


def test_a_user_message_is_never_spoken(rig: Rig) -> None:
    rig.transcript.add_user("this is what the user typed.")
    rig.settle()

    assert rig.tts.spoken == []


def test_replacing_the_transcript_stops_the_speech_and_reads_nothing(rig: Rig) -> None:
    old = rig.transcript.begin_assistant()
    rig.transcript.append(old.id, f"{FIRST} ")
    rig.transcript.finish(old.id)
    assert rig.dispatcher.pump_until(lambda: rig.tts.spoken == [FIRST])
    messages = rig.transcript.messages

    rig.transcript.replace(messages)  # restoring a session
    rig.settle()

    assert rig.speech.phase is SpeechPhase.IDLE
    assert rig.tts.spoken == [FIRST]


def test_replacing_the_transcript_cuts_off_what_is_being_said_now() -> None:
    dispatcher = ManualDispatcher()
    hold = threading.Event()
    player = FakeAudioPlayer(hold=hold)
    speech = SpeechOutput(
        backend=FakeTts(), player=lambda: player, dispatcher=dispatcher, config=TtsConfig()
    )
    transcript = Transcript()
    transcript.subscribe(SpeechFeed(speech))
    message = transcript.begin_assistant()
    transcript.append(message.id, f"{FIRST} ")
    assert player.playing.wait(timeout=5)
    transcript.finish(message.id)  # a transcript is not replaced while a reply streams

    transcript.replace(transcript.messages)

    assert speech.phase is SpeechPhase.IDLE
    assert dispatcher.pump_until(lambda: player.interrupted == 1)
    hold.set()
    speech.close()


def test_a_reply_that_was_stopped_stays_stopped_as_the_rest_of_it_streams(rig: Rig) -> None:
    message = rig.transcript.begin_assistant()
    rig.transcript.append(message.id, f"{FIRST} ")
    assert rig.dispatcher.pump_until(lambda: rig.tts.spoken == [FIRST])

    rig.speech.stop()  # Esc
    rig.transcript.append(message.id, f"{SECOND} ")
    rig.transcript.finish(message.id)
    rig.settle()

    assert rig.tts.spoken == [FIRST]


def test_the_next_reply_is_spoken_after_one_was_stopped(rig: Rig) -> None:
    first = rig.transcript.begin_assistant()
    rig.transcript.append(first.id, f"{FIRST} ")
    rig.speech.stop()
    rig.transcript.finish(first.id)
    rig.transcript.add_user("again")

    second = rig.transcript.begin_assistant()
    rig.transcript.append(second.id, f"{SECOND} ")
    rig.transcript.finish(second.id)

    assert rig.dispatcher.pump_until(lambda: SECOND in rig.tts.spoken)


def test_an_error_in_speech_never_reaches_the_chat(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.speech.close()

    # A closed speech output ignores everything; make it raise instead to prove the guard.
    def boom() -> None:
        raise RuntimeError("speech broke")

    rig.speech.begin_reply = boom  # type: ignore[method-assign]  # a test double for a failing call

    with caplog.at_level("ERROR"):
        message = rig.transcript.begin_assistant()  # must not raise
        rig.transcript.append(message.id, "still works. ")
        rig.transcript.finish(message.id)

    assert rig.transcript.message(message.id).text == "still works. "
    assert "speaking a reply failed" in caplog.text
