"""A reply must not be able to say the wake word through the speakers (S08, D23 (7), #126): before
synthesis the configured phrase is replaced, whatever the case and punctuation, as whole words."""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.voice.support import ManualDispatcher
from tumbleweed.voice.config import TtsConfig
from tumbleweed.voice.fake import FakeAudioPlayer, FakeTts
from tumbleweed.voice.output import SILENCED_PHRASE, SpeechOutput, _fold, _phrase_pattern


def matches(spoken: str, avoid: tuple[str, ...]) -> bool:
    """Whether a spoken entry, folded as the scrub folds, still holds the phrase."""
    pattern = _phrase_pattern(avoid)
    assert pattern is not None
    return pattern.search(_fold(spoken)[0]) is not None


def speak(text: str, *, avoid: tuple[str, ...], config: TtsConfig | None = None) -> list[str]:
    dispatcher = ManualDispatcher()
    tts = FakeTts()
    speech = SpeechOutput(
        backend=tts,
        player=FakeAudioPlayer,
        dispatcher=dispatcher,
        config=config or TtsConfig(),
        avoid=avoid,
    )
    try:
        speech.begin_reply()
        speech.feed(text)
        speech.end_reply()
        dispatcher.pump_until(lambda: speech.phase.value == "idle")
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
        "Sure thing, hey Jar-vis is what you say to me.",
        "Sure thing, hey Jarvis2 is what you say to me.",
        "Sure thing, hey J\u00e0rvis is what you say to me.",
        "Sure thing, hey Ja\u0301rvis is what you say to me.",
        "Sure thing, \uff28\uff45\uff59 \uff2a\uff41\uff52\uff56\uff49\uff53 is what you say.",
        "Sure thing, hey j a r v i s is what you say to me.",
    ],
)
def test_the_phrase_is_not_spoken_in_any_spelling(text: str) -> None:
    sentences = speak(text, avoid=("hey_jarvis",))

    spoken = " ".join(sentences)
    assert "jarvis" not in spoken.lower()
    assert SILENCED_PHRASE in spoken
    assert " is what you say" in spoken


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


def test_a_look_alike_letter_of_another_script_is_not_folded_and_can_pass() -> None:
    """The known limit, said by a test: a Cyrillic \u0430 is not a Latin a. The speaking
    threshold, or headphones, are the control for respellings that fool the pattern."""
    sentences = speak("Sure thing, hey J\u0430rvis is what you say to me.", avoid=("hey_jarvis",))

    assert SILENCED_PHRASE not in " ".join(sentences)


@pytest.mark.parametrize(
    "text",
    [
        "The area is 10² = 100 square metres here.",
        "Müller ist schön und wohnt hier schon lange.",
        "Привет, й is a letter we speak.",
        "ありがとう、がぎぐ を言いました。",
        "नमस्ते हिन्दी भाषा है yes.",
        "We went to the café near the old station today.",
        "Add ½ cup of sugar and ™ marks the brand.",
        "Look: " + "ﷺ" * 30 + " is an ordinary run of text.",
    ],
)
def test_text_without_the_phrase_is_spoken_exactly_as_it_was(text: str) -> None:
    """The scrub must not change normal output: folding is only for finding the phrase."""
    sentences = speak(text, avoid=("hey_jarvis",))

    assert " ".join(sentences) == text


def test_the_phrase_is_cut_from_the_original_and_what_is_around_it_is_kept() -> None:
    sentences = speak("Café. Hey Jarvís, schön und gut.", avoid=("hey_jarvis",))

    assert " ".join(sentences) == f"Café. {SILENCED_PHRASE}, schön und gut."


@pytest.mark.parametrize(
    "text",
    ["Ｈｅｙ Ｊａｒｖíｓ now, please.", "Hey, Jar-vis now, please."],  # noqa: RUF001  (the point)
)
def test_accented_and_full_width_spellings_are_still_the_phrase(text: str) -> None:
    sentences = speak(text, avoid=("hey_jarvis",))

    assert " ".join(sentences) == f"{SILENCED_PHRASE} now, please."


def test_a_phrase_across_a_sentence_end_with_accents_is_still_handled() -> None:
    sentences = speak("Sure thing, héy. Jàrvis is the one you ask.", avoid=("hey_jarvis",))

    assert sentences[0] == "Sure thing, héy."  # untouched, and already on its way
    assert "rvis" not in sentences[1]
    assert sentences[1].startswith("is the one")


def test_the_scrubbed_sentence_never_exceeds_the_sentence_bound() -> None:
    sentences = speak("alexa " * 60, avoid=("alexa",))  # 5 letters become 13: it would triple

    assert all(len(sentence) <= 400 for sentence in sentences)
    assert sentences


def test_ordinary_text_that_sounds_like_the_phrase_is_also_replaced() -> None:
    """Defensible and pinned: letters apart from each other are the phrase said slowly."""
    sentences = speak("Please ask Alex a question about it today.", avoid=("alexa",))

    assert SILENCED_PHRASE in " ".join(sentences)


def test_the_cut_across_a_sentence_end_counts_original_characters_not_folded_ones() -> None:
    # "\ufb00" (a ligature) is one character that folds to two: the cut must be in the original,
    # or it would eat the "2" that follows the phrase
    sentences = speak(
        "Sure thing, hey. Je\ufb002 apples are all we have left.", avoid=("hey_jeff",)
    )

    assert sentences[1] == "2 apples are all we have left."


def test_a_combining_mark_on_the_last_letter_of_the_phrase_goes_with_it() -> None:
    sentences = speak("Hey Jarviś now, please.", avoid=("hey_jarvis",))

    assert " ".join(sentences) == f"{SILENCED_PHRASE} now, please."


STOCK = [("hey_jarvis", "heyjarvis"), ("hey_mycroft", "heymycroft"), ("alexa", "alexa")]


def lead(length: int) -> str:
    """A sentence of exactly `length` characters that is no phrase and ends a sentence."""
    return "a" * (length - 1) + "."


@pytest.mark.parametrize(("model", "compact"), STOCK)
def test_the_reply_cap_never_cuts_a_word_into_the_phrase(model: str, compact: str) -> None:
    # "<phrase>x." cut where the room ends right after the phrase would be the phrase
    config = TtsConfig(max_reply_chars=100)
    first = lead(100 - len(compact))
    assert len(first) == 100 - len(compact)

    spoken = speak(f"{first} {compact}xyz.", avoid=(model,), config=config)

    assert spoken[0] == first
    assert not any(matches(entry, (model,)) for entry in spoken)
    assert " ".join(spoken) == first  # the cut word is not spoken at all


def test_the_reproduction_with_punctuation_in_the_phrase() -> None:
    config = TtsConfig(max_reply_chars=100)
    first = lead(90)

    spoken = speak(f"{first} Hey,Jarvisson.", avoid=("hey_jarvis",), config=config)

    assert not any(matches(entry, ("hey_jarvis",)) for entry in spoken)
    assert " ".join(spoken) == first


def test_the_sentence_bound_cut_does_not_cut_into_the_phrase_either() -> None:
    config = TtsConfig(max_sentence_chars=40)
    text = "w" * 31 + "heyjarvisx and then it goes on"  # the 40th character ends the phrase

    spoken = speak(text, avoid=("hey_jarvis",), config=config)

    assert not any(matches(entry, ("hey_jarvis",)) for entry in spoken)


def test_a_cut_at_a_space_still_speaks_the_words_that_fit() -> None:
    config = TtsConfig(max_reply_chars=100)
    first = lead(90)

    spoken = speak(f"{first} one two three four.", avoid=("hey_jarvis",), config=config)

    assert " ".join(spoken) == f"{first} one two"


def test_a_ligature_before_a_match_is_cut_where_it_really_is() -> None:
    spoken = speak("\ufb01ne \ufb01ne, hey jarvis is it.", avoid=("hey_jarvis",))

    assert spoken == [f"\ufb01ne \ufb01ne, {SILENCED_PHRASE} is it."]


def test_the_text_after_a_straddling_cut_is_scrubbed_in_its_own_coordinates() -> None:
    spoken = speak("Sure thing, hey. Jarvis, then hey jarvis again.", avoid=("hey_jarvis",))

    assert spoken[0] == "Sure thing, hey."
    assert spoken[1] == f"then {SILENCED_PHRASE} again."


def test_the_previous_sentence_is_remembered_as_it_was_spoken() -> None:
    spoken = speak("I said hey jarvis and hey. Jarvis is next here.", avoid=("hey_jarvis",))

    assert spoken == [f"I said {SILENCED_PHRASE} and hey.", "is next here."]


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    pieces=st.lists(
        st.sampled_from(
            [
                "hey",
                "jarvis",
                "Jarvis",
                "jar",
                "vis",
                " ",
                " ",
                "-",
                ",",
                ".",
                "!",
                "x",
                "mycroft",
                "alexa",
                "a",
                "é",
            ]
        ),
        min_size=1,
        max_size=60,
    ),
    model=st.sampled_from(["hey_jarvis", "hey_mycroft", "alexa"]),
    reply_cap=st.integers(min_value=100, max_value=160),
    sentence_cap=st.integers(min_value=40, max_value=70),
)
def test_no_spoken_entry_ever_holds_the_phrase_whatever_the_text_and_the_caps(
    pieces: list[str], model: str, reply_cap: int, sentence_cap: int
) -> None:
    config = TtsConfig(max_reply_chars=reply_cap, max_sentence_chars=sentence_cap)
    text = "".join(pieces) * 4

    spoken = speak(text, avoid=(model,), config=config)

    assert not any(matches(entry, (model,)) for entry in spoken)
    assert all(len(entry) <= sentence_cap for entry in spoken)
    assert sum(len(entry) for entry in spoken) <= reply_cap


def test_the_last_check_drops_from_the_phrase_on_and_never_grows_the_sentence() -> None:
    speech = SpeechOutput(
        backend=FakeTts(),
        player=FakeAudioPlayer,
        dispatcher=ManualDispatcher(),
        config=TtsConfig(),
        avoid=("hey_jarvis",),
    )
    pattern = _phrase_pattern(("hey_jarvis",))
    assert pattern is not None
    try:
        checked = speech._final_check("Good déjà vu, Hey Jarvis and more.", pattern)  # noqa: SLF001

        assert checked == "Good déjà vu,"
        assert speech._previous == "Good deja vu,"  # noqa: SLF001  (what the next sentence follows)
        assert speech._final_check("Nothing to see here.", pattern) == "Nothing to see here."  # noqa: SLF001
    finally:
        speech.close()
