"""A reply must not be able to say the wake word through the speakers (S08, D23 (7), #126): before
synthesis the configured phrase is replaced, whatever the case and punctuation, as whole words."""

import pytest

from tests.voice.support import ManualDispatcher
from tumbleweed.voice.config import TtsConfig
from tumbleweed.voice.fake import FakeAudioPlayer, FakeTts
from tumbleweed.voice.output import SILENCED_PHRASE, SpeechOutput


def speak(text: str, *, avoid: tuple[str, ...]) -> list[str]:
    dispatcher = ManualDispatcher()
    tts = FakeTts()
    speech = SpeechOutput(
        backend=tts,
        player=FakeAudioPlayer,
        dispatcher=dispatcher,
        config=TtsConfig(),
        avoid=avoid,
    )
    try:
        speech.begin_reply()
        speech.feed(text)
        speech.end_reply()
        dispatcher.pump_until(lambda: tts.spoken != [] and speech.phase.value == "idle")
        dispatcher.settle(0.1)
    finally:
        speech.close()
    return tts.spoken


@pytest.mark.parametrize(
    "text",
    [
        "Sure thing, hey jarvis is what you say to me.",
        "Sure thing, Hey Jarvis is what you say to me.",
        "Sure thing, HEY, JARVIS! is what you say to me.",
        "Sure thing, hey-jarvis is what you say to me.",
        "Sure thing, hey_jarvis is what you say to me.",
        "Sure thing, heyjarvis is what you say to me.",
    ],
)
def test_the_phrase_is_not_spoken_in_any_spelling(text: str) -> None:
    sentences = speak(text, avoid=("hey_jarvis",))

    spoken = " ".join(sentences)
    assert "jarvis" not in spoken.lower()
    assert SILENCED_PHRASE in spoken
    assert spoken.endswith("is what you say to me.")


def test_nor_across_the_end_of_a_sentence() -> None:
    sentences = speak("Sure thing, hey. Jarvis is the one you ask for it.", avoid=("hey_jarvis",))

    assert len(sentences) == 2  # the first is already on its way, ending in "hey"
    assert sentences[0].lower().endswith("hey.")
    assert "jarvis" not in sentences[1].lower()
    assert sentences[1].startswith("is the one")


def test_a_sentence_that_only_follows_a_hey_is_left_alone() -> None:
    sentences = speak("Sure thing, hey. Look at this other thing now.", avoid=("hey_jarvis",))

    assert sentences[-1] == "Look at this other thing now."


def test_other_words_that_merely_contain_it_are_spoken_as_they_are() -> None:
    (sentence,) = speak(
        "Alexander and Alexandra met at the alexandrite mine today.", avoid=("alexa",)
    )

    assert sentence == "Alexander and Alexandra met at the alexandrite mine today."


def test_a_single_word_phrase_is_replaced_as_a_word() -> None:
    (sentence,) = speak("You could call out to Alexa, and it would wake.", avoid=("alexa",))

    assert "Alexa" not in sentence
    assert SILENCED_PHRASE in sentence


def test_without_a_wake_word_nothing_is_changed() -> None:
    (sentence,) = speak("You could say hey jarvis to somebody else entirely.", avoid=())

    assert "hey jarvis" in sentence


def test_the_phrase_in_the_text_that_is_not_the_configured_one_is_left() -> None:
    (sentence,) = speak("Say hey mycroft if that is the word you set up.", avoid=("hey_jarvis",))

    assert "hey mycroft" in sentence
